"""Regression tests for organizer workspace routing."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_admin_token, create_user_token
from portal.database import (
    configure,
    create_event,
    create_room,
    create_user,
    dispose,
    get_event_by_slug,
    get_session,
    init_db,
    list_memberships_for_event,
    set_event_membership,
    set_room_membership,
)
from portal.workspace_routing import legacy_admin_to_workspace_path, workspace_to_admin_path

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize(
    ("workspace_path", "handler_path"),
    [
        ("/workspace", "/admin"),
        ("/workspace/events", "/admin/events"),
        ("/workspace/setup", "/admin/setup"),
        ("/workspace/api/events/1/api-keys", "/admin/api/events/1/api-keys"),
        ("/workspace/models/trigger_download", "/admin/models/trigger_download"),
        ("/workspace/models/download_progress", "/admin/models/download_progress"),
        (
            "/workspace/models/supertonic/trigger_download",
            "/admin/models/supertonic/trigger_download",
        ),
        (
            "/workspace/models/supertonic/download_progress",
            "/admin/models/supertonic/download_progress",
        ),
        (
            "/api/workspace/events/1/rooms/2/transcripts/en",
            "/api/admin/events/1/rooms/2/transcripts/en",
        ),
        ("/api/workspace/providers/translation/models", "/api/admin/providers/translation/models"),
    ],
)
@pytest.mark.anyio
async def test_workspace_route_inventory_maps_only_known_route_families(workspace_path, handler_path):
    assert workspace_to_admin_path(workspace_path) == handler_path
    assert legacy_admin_to_workspace_path(handler_path) == workspace_path


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/eventspoof",
        "/workspace/events/1/not-a-route",
        "/workspace/setup/unknown",
        "/workspace/api/events/1/not-a-route",
        "/workspace/models",
        "/workspace/models/future-route",
        "/workspace/models/supertonic/future-route",
        "/api/workspace/events/1/not-a-route",
        "/api/workspace/providers/future-route",
    ],
)
@pytest.mark.anyio
async def test_workspace_route_inventory_rejects_unknown_and_deep_paths(path):
    assert workspace_to_admin_path(path) is None


@pytest.fixture
async def organizer():
    async with get_session() as session:
        event = await create_event(session, slug="shared-event", display_name="Shared Event")
        user = await create_user(session, email="owner@example.com", display_name="Event Owner")
        await set_event_membership(session, user_id=user.id, event_id=event.id, role="event_owner")
        event_id = event.id
        user_id = user.id
        email = user.email

    return {
        "event_id": event_id,
        "user_id": user_id,
        "cookies": {"user_token": create_user_token(user_id=user_id, email=email)},
    }


@pytest.mark.anyio
async def test_event_owner_manages_event_from_workspace_namespace(organizer):
    async with client() as http:
        response = await http.get(
            f"/workspace/events/{organizer['event_id']}/",
            cookies=organizer["cookies"],
        )

    assert response.status_code == 200
    assert "Shared Event" in response.text
    assert f"/workspace/events/{organizer['event_id']}/rooms/" in response.text
    assert f"/admin/events/{organizer['event_id']}/rooms/" not in response.text


@pytest.mark.anyio
async def test_legacy_admin_event_url_redirects_owner_and_preserves_query(organizer):
    async with client() as http:
        response = await http.get(
            f"/admin/events/{organizer['event_id']}/rooms/?search=main%20hall",
            cookies=organizer["cookies"],
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == (f"/workspace/events/{organizer['event_id']}/rooms/?search=main%20hall")


@pytest.mark.anyio
async def test_workspace_form_submission_stays_in_workspace_namespace(organizer):
    async with client() as http:
        response = await http.post(
            f"/workspace/events/{organizer['event_id']}/rooms/",
            data={"display_name": "Main Hall"},
            cookies=organizer["cookies"],
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/workspace/events/{organizer['event_id']}/rooms/"


@pytest.mark.anyio
async def test_super_admin_retains_admin_namespace(organizer):
    async with client() as http:
        response = await http.get(
            f"/admin/events/{organizer['event_id']}/",
            cookies={"admin_token": create_admin_token()},
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert f"/admin/events/{organizer['event_id']}/rooms/" in response.text
    assert f"/workspace/events/{organizer['event_id']}/rooms/" not in response.text


@pytest.mark.anyio
async def test_workspace_api_uses_workspace_namespace(organizer):
    async with client() as http:
        response = await http.get(
            f"/workspace/api/events/{organizer['event_id']}/api-keys",
            cookies=organizer["cookies"],
        )
        legacy = await http.get(
            f"/admin/api/events/{organizer['event_id']}/api-keys",
            cookies=organizer["cookies"],
            follow_redirects=False,
        )

    assert response.status_code == 200
    assert response.json() == []
    assert legacy.status_code == 307
    assert legacy.headers["location"] == f"/workspace/api/events/{organizer['event_id']}/api-keys"


@pytest.mark.anyio
async def test_workspace_does_not_expose_system_admin_routes(organizer):
    async with client() as http:
        response = await http.get("/workspace/users/", cookies=organizer["cookies"])

    assert response.status_code == 404


@pytest.mark.anyio
async def test_event_owner_cannot_use_system_admin_actions(organizer):
    async with client() as http:
        response = await http.post("/admin/demo/regenerate", cookies=organizer["cookies"])

    assert response.status_code == 403


@pytest.mark.anyio
async def test_workspace_trailing_slash_redirect_stays_in_workspace(organizer):
    async with client() as http:
        response = await http.get(
            "/workspace/events",
            cookies=organizer["cookies"],
            follow_redirects=False,
        )

    assert response.status_code == 307
    assert response.headers["location"] == "http://test/workspace/events/"


@pytest.mark.anyio
async def test_organizer_navigation_points_to_workspace(organizer):
    async with client() as http:
        home = await http.get("/", cookies=organizer["cookies"])
        account = await http.get("/account", cookies=organizer["cookies"])

    assert home.status_code == 200
    assert 'href="/workspace/"' in home.text
    assert account.status_code == 200
    assert 'href="/workspace/">Organizer Workspace</a>' in account.text
    assert f'href="/workspace/events/{organizer["event_id"]}/"' in account.text


@pytest.mark.anyio
async def test_unrelated_user_cannot_access_workspace_event(organizer):
    async with get_session() as session:
        user = await create_user(session, email="outsider@example.com", display_name="Outsider")
        outsider_cookie = {"user_token": create_user_token(user_id=user.id, email=user.email)}

    async with client() as http:
        response = await http.get(
            f"/workspace/events/{organizer['event_id']}/",
            cookies=outsider_cookie,
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_event_owner_can_access_general_workspace_routes(organizer):
    async with client() as http:
        dashboard = await http.get(
            "/workspace/",
            cookies=organizer["cookies"],
            follow_redirects=False,
        )
        setup = await http.get("/workspace/setup", cookies=organizer["cookies"])

    assert dashboard.status_code == 303
    assert dashboard.headers["location"] == f"/workspace/events/{organizer['event_id']}/"
    assert setup.status_code == 200


@pytest.mark.anyio
async def test_event_owner_cannot_manage_an_event_they_do_not_own(organizer):
    async with get_session() as session:
        other_event = await create_event(session, slug="other-event", display_name="Other Event")
        other_event_id = other_event.id

    async with client() as http:
        response = await http.get(
            f"/workspace/events/{other_event_id}/",
            cookies=organizer["cookies"],
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_room_coordinator_cannot_access_event_owner_workspace(organizer):
    async with get_session() as session:
        room = await create_room(
            session,
            event_id=organizer["event_id"],
            display_name="Coordinator Room",
        )
        user = await create_user(session, email="coordinator@example.com", display_name="Coordinator")
        await set_room_membership(session, user_id=user.id, room_id=room.id, role="room_coordinator")
        coordinator_cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}

    async with client() as http:
        workspace_response = await http.get(
            f"/workspace/events/{organizer['event_id']}/",
            cookies=coordinator_cookies,
        )
        legacy_response = await http.get(
            f"/admin/events/{organizer['event_id']}/rooms/{room.id}/",
            cookies=coordinator_cookies,
            follow_redirects=False,
        )

    assert workspace_response.status_code == 403
    assert legacy_response.status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "slug", "display_name"),
    [
        ("/workspace/events/", "quick-created", "Quick Created"),
        ("/workspace/setup", "wizard-created", "Wizard Created"),
    ],
)
async def test_workspace_event_creation_assigns_creator_ownership(organizer, path, slug, display_name):
    async with client() as http:
        response = await http.post(
            path,
            data={"slug": slug, "display_name": display_name},
            cookies=organizer["cookies"],
            follow_redirects=False,
        )

    assert response.status_code == 303
    async with get_session() as session:
        event = await get_event_by_slug(session, slug)
        assert event is not None
        memberships = await list_memberships_for_event(session, event.id)
        assert any(
            membership.user_id == organizer["user_id"] and membership.role == "event_owner"
            for membership in memberships
        )

    async with client() as http:
        event_page = await http.get(f"/workspace/events/{event.id}/", cookies=organizer["cookies"])

    assert event_page.status_code == 200


@pytest.mark.anyio
async def test_room_coordinator_navigation_uses_mission_control(organizer):
    async with get_session() as session:
        room = await create_room(
            session,
            event_id=organizer["event_id"],
            display_name="Navigation Room",
        )
        user = await create_user(session, email="nav-coordinator@example.com", display_name="Navigation Coordinator")
        await set_event_membership(
            session,
            user_id=user.id,
            event_id=organizer["event_id"],
            role="room_coordinator",
        )
        await set_room_membership(session, user_id=user.id, room_id=room.id, role="room_coordinator")
        coordinator_cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}

    async with client() as http:
        home = await http.get("/", cookies=coordinator_cookies)
        account = await http.get("/account", cookies=coordinator_cookies)

    assert home.status_code == 200
    assert 'href="/mission-control/"' in home.text
    assert 'href="/workspace/"' not in home.text
    assert account.status_code == 200
    assert 'href="/mission-control/">Mission Control</a>' in account.text
    assert 'href="/mission-control/shared-event/"' in account.text
    assert f'href="/workspace/events/{organizer["event_id"]}/"' not in account.text


@pytest.mark.anyio
async def test_workspace_event_list_omits_coordinator_only_events(organizer):
    async with get_session() as session:
        coordinator_event = await create_event(
            session,
            slug="coordinator-only",
            display_name="Coordinator Only Event",
        )
        room = await create_room(session, event_id=coordinator_event.id, display_name="Coordinator Room")
        await set_room_membership(
            session,
            user_id=organizer["user_id"],
            room_id=room.id,
            role="room_coordinator",
        )

    async with client() as http:
        response = await http.get("/workspace/events/", cookies=organizer["cookies"])

    assert response.status_code == 200
    assert "Shared Event" in response.text
    assert "Coordinator Only Event" not in response.text


@pytest.mark.anyio
async def test_regular_user_navigation_omits_management_dashboard():
    async with get_session() as session:
        user = await create_user(session, email="regular@example.com", display_name="Regular User")
        regular_cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}

    async with client() as http:
        home = await http.get("/", cookies=regular_cookies)
        account = await http.get("/account", cookies=regular_cookies)

    assert home.status_code == 200
    assert 'href="/workspace/"' not in home.text
    assert 'href="/admin/"' not in home.text
    assert 'href="/mission-control/"' not in home.text
    assert '<span class="max-sm:hidden">Dashboard</span>' not in home.text
    assert 'href="/account"' in home.text
    assert account.status_code == 200
    nav_html = account.text.split('<nav class="header-nav">')[1].split("</nav>")[0]
    assert nav_html.count("My Account") == 1


@pytest.mark.anyio
async def test_admin_token_navigation_points_to_admin():
    admin_cookies = {"admin_token": create_admin_token()}

    async with client() as http:
        home = await http.get("/", cookies=admin_cookies)

    assert home.status_code == 200
    assert 'href="/admin/"' in home.text
    assert '<span class="max-sm:hidden">Dashboard</span>' in home.text
