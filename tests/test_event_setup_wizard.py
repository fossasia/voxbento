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
    list_rooms_for_event,
    set_event_membership,
)
from portal.models import Event, User, utc_now
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
                "slug": "org-event",
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
        assert updated.slug == "org-event"
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
async def test_setup_edit_event_rejects_slug_changes_for_existing_event(admin_cookie):
    async with get_session() as s:
        ev2 = await create_event(s, slug="slug-two", display_name="Second")
        ev2_id = ev2.id

    async with _client() as client:
        resp = await client.post(
            f"/admin/setup?eventId={ev2_id}",
            data={
                "event_id": str(ev2_id),
                "display_name": "Second Updated",
                "slug": "new-slug",  # change slug attempt
            },
            cookies=admin_cookie,
        )
    assert resp.status_code == 400
    assert "The URL slug cannot be changed once the event is created" in resp.text


@pytest.mark.anyio
async def test_setup_create_event_rejects_taken_or_soft_deleted_slug(admin_cookie):
    async with get_session() as s:
        # Create an active event and a soft-deleted event
        await create_event(s, slug="active-slug", display_name="Active")
        del_ev = Event(slug="deleted-slug", display_name="Deleted", deleted_at=utc_now())
        s.add(del_ev)
        await s.flush()

    async with _client() as client:
        # Taken active slug
        resp1 = await client.post(
            "/admin/setup",
            data={"display_name": "New Active", "slug": "active-slug"},
            cookies=admin_cookie,
        )
        assert resp1.status_code == 400
        assert "That URL slug is already taken" in resp1.text

        # Reserved soft-deleted slug
        resp2 = await client.post(
            "/admin/setup",
            data={"display_name": "New Deleted", "slug": "deleted-slug"},
            cookies=admin_cookie,
        )
        assert resp2.status_code == 400
        assert "That URL slug is already taken" in resp2.text


@pytest.mark.anyio
async def test_setup_edit_event_rejects_stale_concurrent_edit(organizer):
    event_id = organizer["event_id"]
    async with _client() as client:
        resp = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "My New Name",
                "original_display_name": "Stale Outdated Name",  # Mismatch with actual "Organizer Event"
                "slug": "org-event",
            },
            cookies=organizer["cookies"],
        )
    assert resp.status_code == 409
    assert "This event was modified by another organizer" in resp.text


@pytest.mark.anyio
async def test_update_event_atomic_conditional_update(organizer):
    from portal.database import update_event

    event_id = organizer["event_id"]

    async with get_session() as s:
        # Atomic update with matching expected_display_name succeeds
        updated = await update_event(
            s,
            event_id=event_id,
            display_name="First Atomic Update",
            expected_display_name="Organizer Event",
        )
        assert updated is not None
        assert updated.display_name == "First Atomic Update"

    async with get_session() as s:
        # Atomic update with mismatched expected_display_name raises ValueError
        with pytest.raises(ValueError, match="modified by another organizer"):
            await update_event(
                s,
                event_id=event_id,
                display_name="Second Atomic Update",
                expected_display_name="Organizer Event",  # Outdated expected name
            )


@pytest.mark.anyio
async def test_untrusted_return_to_falls_back_to_safe_destination(admin_cookie):
    async with get_session() as s:
        ev = await create_event(s, slug="return-test", display_name="Return Test")
        event_id = ev.id

    async with _client() as client:
        # 1. Javascript URI in return_to
        resp_js = await client.get(
            f"/admin/setup?eventId={event_id}&return_to=javascript:alert(1)",
            cookies=admin_cookie,
        )
        assert resp_js.status_code == 200
        assert "javascript:alert(1)" not in resp_js.text

        # 2. External URL in POST return_to
        resp_post = await client.post(
            f"/admin/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "Updated Name",
                "slug": "return-test",
                "return_to": "https://attacker.example.com",
            },
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp_post.status_code == 303
        assert resp_post.headers["location"].endswith(f"/events/{event_id}/setup/rooms")

        # 3. Malformed bracketed authority in return_to
        resp_malformed = await client.post(
            f"/admin/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "Updated Name 2",
                "slug": "return-test",
                "return_to": "http://[",
            },
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp_malformed.status_code == 303
        assert resp_malformed.headers["location"].endswith(f"/events/{event_id}/setup/rooms")

        # 4. Path traversal in return_to attempting cross-event escape
        resp_traversal = await client.post(
            f"/admin/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "Updated Name 3",
                "slug": "return-test",
                "return_to": f"/admin/events/{event_id}/../../events/999/setup/rooms",
            },
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp_traversal.status_code == 303
        assert resp_traversal.headers["location"].endswith(f"/events/{event_id}/setup/rooms")

        # 5. Encoded dot-dot path traversal in return_to
        resp_encoded_traversal = await client.post(
            f"/admin/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "Updated Name 4",
                "slug": "return-test",
                "return_to": f"/admin/events/{event_id}/%2e%2e/%2e%2e/events/999/setup/rooms",
            },
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp_encoded_traversal.status_code == 303
        assert resp_encoded_traversal.headers["location"].endswith(f"/events/{event_id}/setup/rooms")


@pytest.mark.anyio
async def test_revoked_hosting_owner_can_edit_owned_event():
    async with get_session() as s:
        user = await create_user(
            s,
            email="revoked@example.com",
            display_name="Revoked Host",
            is_admin=False,
            can_host=False,
            email_verified=True,
        )
        user.hosting_revoked_at = utc_now()
        s.add(user)
        await s.flush()
        ev = await create_event(s, slug="revoked-event", display_name="Revoked Event", owner_user_id=user.id)
        user_id = user.id
        event_id = ev.id
        email = user.email

    token = create_user_token(user_id=user_id, email=email, is_admin=False)
    cookies = {"user_token": token}

    async with _client() as client:
        # Revoked host editing their existing event is allowed (200 OK)
        page = await client.get(f"/workspace/setup?eventId={event_id}", cookies=cookies)
        assert page.status_code == 200
        assert "Edit your event" in page.text

        # Revoked host saving their existing event is allowed (303 Redirect)
        save_resp = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={"event_id": str(event_id), "display_name": "Renamed By Revoked", "slug": "revoked-event"},
            cookies=cookies,
            follow_redirects=False,
        )
        assert save_resp.status_code == 303

        # Revoked host trying to create a new event is blocked (redirects to /account?error=hosting_revoked)
        create_resp = await client.get("/workspace/setup", cookies=cookies, follow_redirects=False)
        assert create_resp.status_code == 303 or create_resp.status_code == 307
        assert "error=hosting_revoked" in create_resp.headers["location"]


@pytest.mark.anyio
async def test_duplicate_room_submission_is_prevented(admin_cookie):
    async with get_session() as s:
        ev = await create_event(s, slug="dup-room-test", display_name="Dup Room Test")
        event_id = ev.id

    data = {
        "from": "setup",
        "display_name": "Idempotent Room",
        "spoken_language": "en",
        "languages": ["es"],
        "delivery_es": "interpreter",
        "submission_id": "test-sub-uuid-12345",
    }

    async with _client() as client:
        # First submission
        resp1 = await client.post(
            f"/admin/events/{event_id}/rooms/new",
            data=data,
            headers={"origin": "http://test"},
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp1.status_code == 303

        # Second submission with the exact same submission_id
        resp2 = await client.post(
            f"/admin/events/{event_id}/rooms/new",
            data=data,
            headers={"origin": "http://test"},
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        assert resp2.cookies.get("room_created") == str(event_id)

    # Verify only ONE room was created in the database
    async with get_session() as s:
        rooms = await list_rooms_for_event(s, event_id)
        assert len(rooms) == 1
        assert rooms[0].display_name == "Idempotent Room"


@pytest.mark.anyio
async def test_concurrent_overlapping_room_submissions_create_only_one_room(admin_cookie):
    import asyncio

    async with get_session() as s:
        ev = await create_event(s, slug="concurrent-room-test", display_name="Concurrent Room Test")
        event_id = ev.id

    data = {
        "from": "setup",
        "display_name": "Concurrent Room",
        "spoken_language": "en",
        "languages": ["es"],
        "delivery_es": "interpreter",
        "submission_id": "concurrent-sub-99999",
    }

    async with _client() as client:
        # Launch two overlapping requests concurrently with asyncio.gather
        task1 = client.post(
            f"/admin/events/{event_id}/rooms/new",
            data=data,
            headers={"origin": "http://test"},
            cookies=admin_cookie,
            follow_redirects=False,
        )
        task2 = client.post(
            f"/admin/events/{event_id}/rooms/new",
            data=data,
            headers={"origin": "http://test"},
            cookies=admin_cookie,
            follow_redirects=False,
        )
        resp1, resp2 = await asyncio.gather(task1, task2)

        assert resp1.status_code == 303
        assert resp2.status_code == 303
        assert resp1.headers["location"] == resp2.headers["location"]

    async with get_session() as s:
        rooms = await list_rooms_for_event(s, event_id)
        assert len(rooms) == 1
        assert rooms[0].display_name == "Concurrent Room"


@pytest.mark.anyio
async def test_concurrent_room_submission_waiter_cancellation_preserves_shared_future(admin_cookie):
    """Test 1: Cancelling one waiter does not cancel the shared Future; remaining waiter succeeds."""
    import asyncio
    from unittest.mock import patch

    import portal.routers.admin as admin_module

    async with get_session() as s:
        ev = await create_event(s, slug="cancellation-room-test", display_name="Cancellation Test")
        event_id = ev.id

    data = {
        "from": "setup",
        "display_name": "Cancelled Waiter Room",
        "spoken_language": "en",
        "languages": ["es"],
        "delivery_es": "interpreter",
        "submission_id": "sub-cancel-12345",
    }

    real_apply_plan = admin_module.apply_plan

    async def delayed_apply_plan(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await real_apply_plan(*args, **kwargs)

    claim_key = (event_id, "sub-cancel-12345")

    async with _client() as client:
        with patch.object(admin_module, "apply_plan", side_effect=delayed_apply_plan):
            # 1. Start creator
            creator_task = asyncio.create_task(
                client.post(
                    f"/admin/events/{event_id}/rooms/new",
                    data=data,
                    headers={"origin": "http://test"},
                    cookies=admin_cookie,
                    follow_redirects=False,
                )
            )
            # Give creator time to acquire claim_key and register in _in_flight_room_creations
            for _ in range(100):
                if claim_key in admin_module._in_flight_room_creations:
                    break
                await asyncio.sleep(0.01)
            assert claim_key in admin_module._in_flight_room_creations
            shared_fut = admin_module._in_flight_room_creations[claim_key]

            # 2. Start two waiters
            waiter1_task = asyncio.create_task(
                client.post(
                    f"/admin/events/{event_id}/rooms/new",
                    data=data,
                    headers={"origin": "http://test"},
                    cookies=admin_cookie,
                    follow_redirects=False,
                )
            )
            waiter2_task = asyncio.create_task(
                client.post(
                    f"/admin/events/{event_id}/rooms/new",
                    data=data,
                    headers={"origin": "http://test"},
                    cookies=admin_cookie,
                    follow_redirects=False,
                )
            )
            # Give waiters time to bootstrap CSRF and begin awaiting the shared future
            await asyncio.sleep(0.05)

            # 3. Cancel waiter 1
            waiter1_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter1_task

            # Shared future must NOT be cancelled
            assert not shared_fut.cancelled()

            # Waiter 2 and Creator must both complete successfully
            resp_creator = await creator_task
            resp_waiter2 = await waiter2_task

            assert resp_creator.status_code == 303
            assert resp_waiter2.status_code == 303
            assert resp_creator.headers["location"] == resp_waiter2.headers["location"]

    # 4. Verify in-flight cleanup and database record
    assert claim_key not in admin_module._in_flight_room_creations
    async with get_session() as s:
        rooms = await list_rooms_for_event(s, event_id)
        assert len(rooms) == 1


@pytest.mark.anyio
async def test_concurrent_room_submission_validation_failure_shared_with_waiters(admin_cookie):
    """Test 2: Validation failure on creator returns HTTP 400 to both creator and concurrent waiters."""
    import asyncio

    import portal.routers.admin as admin_module

    async with get_session() as s:
        ev = await create_event(s, slug="val-fail-test", display_name="Validation Failure Test")
        event_id = ev.id

    # Invalid room data: missing display_name
    invalid_data = {
        "from": "setup",
        "display_name": "",  # Triggers validation error: "Please enter a room name."
        "spoken_language": "en",
        "submission_id": "val-fail-sub-999",
    }
    claim_key = (event_id, "val-fail-sub-999")

    async with _client() as client:
        # Launch overlapping creator and waiter
        task1 = client.post(
            f"/admin/events/{event_id}/rooms/new",
            data=invalid_data,
            headers={"origin": "http://test"},
            cookies=admin_cookie,
            follow_redirects=False,
        )
        task2 = client.post(
            f"/admin/events/{event_id}/rooms/new",
            data=invalid_data,
            headers={"origin": "http://test"},
            cookies=admin_cookie,
            follow_redirects=False,
        )
        resp1, resp2 = await asyncio.gather(task1, task2)

        # Both must return HTTP 400 validation error
        assert resp1.status_code == 400
        assert resp2.status_code == 400
        assert "Give the room a name." in resp1.text
        assert "Give the room a name." in resp2.text

    # In-flight map must be cleaned up
    assert claim_key not in admin_module._in_flight_room_creations


@pytest.mark.anyio
async def test_concurrent_room_submission_unexpected_exception_propagates_and_cleans_up(admin_cookie):
    """Test 4 & 5: Unexpected error in creator propagates to waiters and cleans up in-flight map."""
    import asyncio
    from unittest.mock import patch

    import portal.routers.admin as admin_module

    async with get_session() as s:
        ev = await create_event(s, slug="err-prop-test", display_name="Error Prop Test")
        event_id = ev.id

    data = {
        "from": "setup",
        "display_name": "Error Room",
        "spoken_language": "en",
        "languages": ["es"],
        "delivery_es": "interpreter",
        "submission_id": "err-sub-11111",
    }
    claim_key = (event_id, "err-sub-11111")

    async with _client() as client:
        with patch("portal.routers.admin.create_room", side_effect=RuntimeError("Simulated DB failure")):
            task1 = client.post(
                f"/admin/events/{event_id}/rooms/new",
                data=data,
                headers={"origin": "http://test"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
            task2 = client.post(
                f"/admin/events/{event_id}/rooms/new",
                data=data,
                headers={"origin": "http://test"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
            results = await asyncio.gather(task1, task2, return_exceptions=True)
            for res in results:
                assert isinstance(res, RuntimeError)
                assert "Simulated DB failure" in str(res)

    # In-flight map must be cleaned up, leaving no dangling future
    assert claim_key not in admin_module._in_flight_room_creations


@pytest.mark.anyio
async def test_setup_edit_event_rejects_empty_name_or_invalid_slug(organizer):
    event_id = organizer["event_id"]
    async with _client() as client:
        # Empty name
        resp1 = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={"event_id": str(event_id), "display_name": "", "slug": "org-event"},
            cookies=organizer["cookies"],
        )
        assert resp1.status_code == 400
        assert "Please enter an event name" in resp1.text

        # Invalid slug on new event creation
        resp2 = await client.post(
            "/workspace/setup",
            data={"display_name": "Valid Name", "slug": "BAD SLUG!!!"},
            cookies=organizer["cookies"],
        )
        assert resp2.status_code == 400
        assert "Use lowercase letters, numbers and hyphens" in resp2.text


@pytest.mark.anyio
async def test_setup_edit_event_corrects_rejected_name_and_resubmits_successfully(organizer):
    event_id = organizer["event_id"]
    async with _client() as client:
        # 1. Submit an invalid empty/whitespace name
        resp_reject = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={"event_id": str(event_id), "display_name": "   ", "slug": "org-event"},
            cookies=organizer["cookies"],
        )
        assert resp_reject.status_code == 400
        assert "Please enter an event name" in resp_reject.text

        # 2. Extract original_display_name from the returned form HTML
        match = re.search(r'name="original_display_name"\s+value="([^"]*)"', resp_reject.text)
        assert match is not None
        preserved_original = match.group(1)
        # Must preserve the saved event name as the concurrency baseline, NOT the empty attempted name
        assert preserved_original == "Organizer Event"

        # 3. Resubmit the form with a corrected valid name
        resp_resubmit = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "Properly Corrected Event",
                "slug": "org-event",
                "original_display_name": preserved_original,
            },
            cookies=organizer["cookies"],
            follow_redirects=False,
        )
        assert resp_resubmit.status_code == 303

    async with get_session() as s:
        updated = await get_event_by_id(s, event_id)
        assert updated is not None
        assert updated.display_name == "Properly Corrected Event"


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
    assert "return_to=" in location
    assert (
        f"/admin/events/{event_id}/setup/rooms" in location
        or f"%2Fadmin%2Fevents%2F{event_id}%2Fsetup%2Frooms" in location
    )


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
        "setup_draft_token": "token-12345",
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
    assert f"/events/{event_id}/setup/rooms" in resp.headers["location"]
    assert "clear_token=token-12345" in resp.headers["location"]
    assert resp.cookies.get("room_created") == str(event_id)
    assert resp.cookies.get("room_created_token") == f"{event_id}:token-12345"

    # When visiting /admin/events/{event_id}/setup/rooms with the query param clear_token
    async with _client() as client:
        rooms_page_query = await client.get(
            f"/admin/events/{event_id}/setup/rooms?clear_token=token-12345",
            cookies=admin_cookie,
        )
    assert f'data-clear-room-draft="{event_id}"' in rooms_page_query.text
    assert 'data-clear-room-draft-token="token-12345"' in rooms_page_query.text

    # When visiting /admin/events/{event_id}/setup/rooms with the cookie, page renders data-clear-room-draft
    async with _client() as client:
        rooms_page = await client.get(
            f"/admin/events/{event_id}/setup/rooms",
            cookies={**admin_cookie, "room_created_token": f"{event_id}:token-12345"},
        )
    assert f'data-clear-room-draft="{event_id}"' in rooms_page.text
    assert 'data-clear-room-draft-token="token-12345"' in rooms_page.text


@pytest.mark.anyio
async def test_setup_rooms_invalid_query_clear_token_does_not_clear_draft(admin_cookie):
    async with get_session() as s:
        ev = await create_event(s, slug="invalid-token-test", display_name="Invalid Token Test")
        event_id = ev.id

    async with _client() as client:
        # Malformed / rejected token (%21 = !)
        resp = await client.get(
            f"/admin/events/{event_id}/setup/rooms?clear_token=%21",
            cookies=admin_cookie,
        )
        assert resp.status_code == 200
        # Must not emit cleanup marker
        assert "data-clear-room-draft" not in resp.text


@pytest.mark.anyio
async def test_setup_stepper_preserves_multiple_query_params_in_return_to(organizer):
    event_id = organizer["event_id"]
    return_path_and_query = f"/workspace/events/{event_id}/setup/rooms?from=setup&step=2"

    async with _client() as client:
        rooms_page = await client.get(
            return_path_and_query,
            cookies=organizer["cookies"],
        )
    assert rooms_page.status_code == 200

    # Ensure return_to parameter is encoded as a single parameter preserving the inner & as %26
    expected_encoded = f"/workspace/events/{event_id}/setup/rooms%3Ffrom%3Dsetup%26step%3D2"
    assert f"return_to={expected_encoded}" in rooms_page.text

    # Extract the Step 1 link from the page
    match = re.search(r'href="([^"]*setup\?eventId=[^"]*)"', rooms_page.text)
    assert match is not None
    bubble_href = match.group(1).replace("&amp;", "&")

    # Navigate to the setup page via the bubble link
    async with _client() as client:
        setup_page = await client.get(bubble_href, cookies=organizer["cookies"])
    assert setup_page.status_code == 200

    # Verify the Cancel button target preserves both query parameters intact
    cancel_match = re.search(r'<a href="([^"]+)" class="btn">Cancel</a>', setup_page.text)
    assert cancel_match is not None
    cancel_href = cancel_match.group(1).replace("&amp;", "&")
    assert cancel_href == return_path_and_query

    # Verify submitting the form redirects to the return URL with both query parameters intact
    async with _client() as client:
        save_resp = await client.post(
            f"/workspace/setup?eventId={event_id}",
            data={
                "event_id": str(event_id),
                "display_name": "Organizer Event",
                "slug": "org-event",
                "return_to": return_path_and_query,
            },
            cookies=organizer["cookies"],
            follow_redirects=False,
        )
    assert save_resp.status_code == 303
    assert save_resp.headers["location"] == return_path_and_query
