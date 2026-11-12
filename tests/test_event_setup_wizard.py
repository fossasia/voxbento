"""Tests for Event Setup Wizard navigation, event hydration/editing, and draft lifecycle."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token, generate_admin_csrf_token
from portal.database import (
    configure,
    create_event,
    create_user,
    dispose,
    get_event_by_id,
    get_session,
    init_db,
    set_event_membership,
)
from portal.models import Event, User
from portal.roles import EVENT_OWNER


@pytest.fixture(autouse=True)
async def setup_db():
    """Set up an isolated in-memory database for each test."""
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _raw_client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _client():
    class CSRFAsyncClient:
        def __init__(self, client):
            self._client = client

        async def __aenter__(self):
            await self._client.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self._client.__aexit__(*args)

        def __getattr__(self, name):
            return getattr(self._client, name)

        async def get(self, url, **kwargs):
            return await self.request("GET", url, **kwargs)

        async def post(self, url, **kwargs):
            return await self.request("POST", url, **kwargs)

        async def request(self, method, url, **kwargs):
            path = urlsplit(str(url)).path
            is_admin_path = path.startswith("/admin/") or path.startswith("/workspace/")
            is_mutation = method.upper() in {"POST", "PUT", "PATCH", "DELETE"}

            if is_admin_path and is_mutation:
                original_cookies = kwargs.get("cookies")
                bootstrap = await self._client.get("/admin/events/", cookies=original_cookies)
                csrf_token = bootstrap.cookies.get("admin_csrf") or self._client.cookies.get("admin_csrf")
                if csrf_token:
                    cookies = dict(self._client.cookies)
                    if original_cookies:
                        cookies.update(dict(original_cookies))
                    cookies["admin_csrf"] = csrf_token
                    kwargs["cookies"] = cookies

                    headers = dict(kwargs.get("headers") or {})
                    headers["X-CSRF-Token"] = csrf_token
                    kwargs["headers"] = headers

            return await self._client.request(method, url, **kwargs)

    return CSRFAsyncClient(_raw_client())


@pytest.fixture
async def seeded_admin():
    async with get_session() as s:
        user = await create_user(
            s,
            email="admin@example.com",
            display_name="Site Admin",
            is_admin=True,
            can_host=True,
            email_verified=True,
        )
    return user


@pytest.fixture
async def admin_cookie(seeded_admin):
    token = create_user_token(user_id=seeded_admin.id, email=seeded_admin.email, is_admin=True)
    return {"user_token": token}


@pytest.fixture
async def organizer():
    async with get_session() as s:
        user = await create_user(
            s,
            email="organizer@example.com",
            display_name="Event Organizer",
            is_admin=False,
            can_host=True,
            email_verified=True,
        )
        event = await create_event(s, slug="org-event", display_name="Organizer Event", owner_user_id=user.id)
        user_id = user.id
        event_id = event.id
        email = user.email

    token = create_user_token(user_id=user_id, email=email, is_admin=False)
    return {
        "user_id": user_id,
        "event_id": event_id,
        "cookies": {"user_token": token},
    }


@pytest.fixture
async def other_user():
    async with get_session() as s:
        user = await create_user(
            s,
            email="other@example.com",
            display_name="Other User",
            is_admin=False,
            can_host=True,
            email_verified=True,
        )
        user_id = user.id
        email = user.email
    token = create_user_token(user_id=user_id, email=email, is_admin=False)
    return {"user_id": user_id, "cookies": {"user_token": token}}


@pytest.mark.anyio
async def test_setup_start_without_event_id_renders_create_form(admin_cookie):
    async with _client() as client:
        resp = await client.get("/admin/setup", cookies=admin_cookie)
    assert resp.status_code == 200
    assert "Create your event" in resp.text
    assert 'value=""' in resp.text or "value=" not in resp.text


@pytest.mark.anyio
async def test_setup_start_with_unauthorized_user_returns_403(organizer, other_user):
    async with _client() as client:
        resp = await client.get(
            f"/admin/setup?eventId={organizer['event_id']}",
            cookies=other_user["cookies"],
        )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_setup_start_with_nonexistent_event_id_returns_404(admin_cookie):
    async with _client() as client:
        resp = await client.get("/admin/setup?eventId=999999", cookies=admin_cookie)
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_setup_start_with_authorized_owner_hydrates_form(organizer):
    async with _client() as client:
        resp = await client.get(
            f"/workspace/setup?eventId={organizer['event_id']}&return_to=/workspace/events/{organizer['event_id']}/setup/rooms",
            cookies=organizer["cookies"],
        )
    assert resp.status_code == 200
    assert "Edit your event" in resp.text
    assert 'value="Organizer Event"' in resp.text
    assert 'value="org-event"' in resp.text
    assert f'name="event_id" value="{organizer["event_id"]}"' in resp.text
    assert f'value="/workspace/events/{organizer["event_id"]}/setup/rooms"' in resp.text


@pytest.mark.anyio
async def test_setup_stepper_preserves_event_id_and_return_to(organizer):
    async with _client() as client:
        resp = await client.get(
            f"/workspace/events/{organizer['event_id']}/setup/rooms",
            cookies=organizer["cookies"],
        )
    assert resp.status_code == 200
    # Step 1 bubble link must contain ?eventId=... and return_to
    assert f"/setup?eventId={organizer['event_id']}" in resp.text
    assert f"return_to=/workspace/events/{organizer['event_id']}/setup/rooms" in resp.text


@pytest.mark.anyio
async def test_setup_edit_event_preserves_id_and_updates_fields(organizer):
    event_id = organizer["event_id"]
    return_target = f"/workspace/events/{event_id}/rooms/new?from=setup"
    async with _client() as client:
        resp = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "Renamed Event",
                "slug": "renamed-event",
                "return_to": return_target,
            },
            cookies=organizer["cookies"],
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == return_target

    async with get_session() as s:
        updated = await get_event_by_id(s, event_id)
        assert updated is not None
        assert updated.display_name == "Renamed Event"
        assert updated.slug == "renamed-event"
        assert updated.id == event_id


@pytest.mark.anyio
async def test_setup_edit_event_permits_keeping_same_slug(organizer):
    event_id = organizer["event_id"]
    async with _client() as client:
        resp = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "Updated Title Only",
                "slug": "org-event",
            },
            cookies=organizer["cookies"],
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"/events/{event_id}/setup/rooms")

    async with get_session() as s:
        updated = await get_event_by_id(s, event_id)
        assert updated is not None
        assert updated.display_name == "Updated Title Only"
        assert updated.slug == "org-event"


@pytest.mark.anyio
async def test_setup_edit_event_rejects_slug_taken_by_another_event(admin_cookie):
    async with get_session() as s:
        await create_event(s, slug="slug-one", display_name="First")
        ev2 = await create_event(s, slug="slug-two", display_name="Second")
        ev2_id = ev2.id

    async with _client() as client:
        resp = await client.post(
            f"/admin/setup?eventId={ev2_id}",
            data={
                "event_id": str(ev2_id),
                "display_name": "Second Updated",
                "slug": "slug-one",  # conflict with ev1
            },
            cookies=admin_cookie,
        )
    assert resp.status_code == 400
    assert "already exists" in resp.text


@pytest.mark.anyio
async def test_setup_edit_event_rejects_empty_name_or_invalid_slug(organizer):
    event_id = organizer["event_id"]
    async with _client() as client:
        # Empty name
        resp1 = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={"event_id": str(event_id), "display_name": "", "slug": "valid-slug"},
            cookies=organizer["cookies"],
        )
        assert resp1.status_code == 400
        assert "Please enter an event name" in resp1.text

        # Invalid slug
        resp2 = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={"event_id": str(event_id), "display_name": "Valid Name", "slug": "BAD SLUG!!!"},
            cookies=organizer["cookies"],
        )
        assert resp2.status_code == 400
        assert "That URL slug isn't valid" in resp2.text


@pytest.mark.anyio
async def test_organizer_redirect_to_workspace_preserves_query(organizer):
    event_id = organizer["event_id"]
    async with _client() as client:
        resp = await client.get(
            f"/admin/setup?eventId={event_id}&return_to=/admin/events/{event_id}/setup/rooms",
            cookies=organizer["cookies"],
            follow_redirects=False,
        )
    assert resp.status_code == 303 or resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("/workspace/setup")
    assert f"eventId={event_id}" in location


@pytest.mark.anyio
async def test_successful_room_creation_sets_cleanup_cookie(admin_cookie):
    async with get_session() as s:
        ev = await create_event(s, slug="room-clean-test", display_name="Room Clean Test")
        event_id = ev.id

    data = {
        "from": "setup",
        "display_name": "Test Room",
        "spoken_language": "en",
        "languages": ["es"],
        "delivery_es": "interpreter",
    }
    async with _client() as client:
        resp = await client.post(
            f"/admin/events/{event_id}/rooms/new",
            data=data,
            headers={"origin": "http://test"},
            cookies=admin_cookie,
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"/events/{event_id}/setup/rooms")
    # Cookie room_created is set to the event ID
    assert resp.cookies.get("room_created") == str(event_id)

    # When visiting /admin/events/{event_id}/setup/rooms with the cookie, page renders data-clear-room-draft
    async with _client() as client:
        rooms_page = await client.get(
            f"/admin/events/{event_id}/setup/rooms",
            cookies={**admin_cookie, "room_created": str(event_id)},
        )
    assert f'data-clear-room-draft="{event_id}"' in rooms_page.text
