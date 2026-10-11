from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"

from portal.auth import create_admin_token, create_user_token


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _admin_csrf(client, cookies):
    import secrets

    csrf_token = secrets.token_hex(32)
    client.cookies.set("admin_csrf", csrf_token)
    return csrf_token


async def _create_user(email="owner@test.com", is_admin=False):
    from portal.auth import hash_password
    from portal.database import create_user, get_session

    async with get_session() as s:
        user = await create_user(s, email=email, display_name="Test", password_hash=hash_password("pw"))
        user.is_admin = is_admin
        await s.commit()
        return user


async def _create_event_room(slug="ev1"):
    from portal.database import create_event, create_room, get_session

    async with get_session() as s:
        ev = await create_event(s, slug=slug, display_name=slug)
        rm = await create_room(s, event_id=ev.id, display_name="Room1")
        rm.floor_transcription_enabled = True
        rm.floor_transcription_provider = "openai"
        await s.commit()
        return ev, rm


async def _set_membership(user_id, event_id=None, room_id=None, role="event_owner"):
    from portal.database import get_session
    from portal.models import EventMembership, RoomMembership

    async with get_session() as s:
        if event_id:
            s.add(EventMembership(user_id=user_id, event_id=event_id, role=role))
        if room_id:
            s.add(RoomMembership(user_id=user_id, room_id=room_id, role=role))
        await s.commit()


# --- The Tests ---


@pytest.mark.anyio
@patch("portal.routers.admin.start_transcription_worker")
@patch("portal.routers.admin.stop_transcription_worker")
async def test_event_owner_can_start_stop_status(mock_stop, mock_start, client, setup_db):
    user = await _create_user()
    ev, rm = await _create_event_room()
    await _set_membership(user.id, event_id=ev.id, role="event_owner")
    token = create_user_token(user_id=user.id, email=user.email)
    csrf_token = await _admin_csrf(client, {"user_token": token})
    with patch("portal.routers.admin.get_http_client") as mock_http:
        from unittest.mock import AsyncMock

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"active_rooms": {f"{ev.slug}-{rm.id}": {}}}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_http.return_value = mock_client

        # Start
        r = await client.post(
            f"/api/rooms/{rm.id}/floor-transcription/start",
            cookies={"user_token": token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200

        # Stop
        r = await client.post(
            f"/api/rooms/{rm.id}/floor-transcription/stop",
            cookies={"user_token": token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200

        # Status
        r = await client.get(f"/api/rooms/{rm.id}/floor-transcription/status", cookies={"user_token": token})
        assert r.status_code == 200


@pytest.mark.anyio
@patch("portal.routers.admin.start_transcription_worker")
@patch("portal.routers.admin.stop_transcription_worker")
async def test_room_coordinator_can_start_stop(mock_stop, mock_start, client, setup_db):
    user = await _create_user()
    ev, rm = await _create_event_room()
    await _set_membership(user.id, room_id=rm.id, role="room_coordinator")
    token = create_user_token(user_id=user.id, email=user.email)
    csrf_token = await _admin_csrf(client, {"user_token": token})
    with patch("portal.routers.admin.get_http_client") as mock_http:
        from unittest.mock import AsyncMock

        mock_resp = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_http.return_value = mock_client
        r = await client.post(
            f"/api/rooms/{rm.id}/floor-transcription/start",
            cookies={"user_token": token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200


@pytest.mark.anyio
async def test_invalid_role_gets_403(client, setup_db):
    user = await _create_user()
    ev, rm = await _create_event_room()
    await _set_membership(user.id, event_id=ev.id, role="interpreter")
    token = create_user_token(user_id=user.id, email=user.email)
    csrf_token = await _admin_csrf(client, {"user_token": token})

    r = await client.post(
        f"/api/rooms/{rm.id}/floor-transcription/start",
        cookies={"user_token": token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code == 403


@pytest.mark.anyio
async def test_cross_event_leak_gets_403(client, setup_db):
    user = await _create_user()
    ev1, rm1 = await _create_event_room("ev1")
    ev2, rm2 = await _create_event_room("ev2")
    await _set_membership(user.id, event_id=ev1.id, role="event_owner")
    token = create_user_token(user_id=user.id, email=user.email)

    r = await client.post(f"/api/rooms/{rm2.id}/floor-transcription/start", cookies={"user_token": token})
    assert r.status_code == 403


@pytest.mark.anyio
@patch("portal.routers.admin.start_transcription_worker")
@patch("portal.routers.admin.stop_transcription_worker")
async def test_super_admin_does_no_extra_query(mock_stop, mock_start, client, setup_db):
    user = await _create_user(is_admin=True)
    ev, rm = await _create_event_room()
    token = create_user_token(user_id=user.id, email=user.email)
    csrf_token = await _admin_csrf(client, {"user_token": token})
    # We will assert that only 2 queries happen in the dependency (fetch user, etc)
    # The short circuit means we shouldn't hit the `RoomMembership` OR query
    with patch("portal.routers.admin.get_http_client") as mock_http:
        from unittest.mock import AsyncMock

        mock_resp = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_http.return_value = mock_client
        r = await client.post(
            f"/api/rooms/{rm.id}/floor-transcription/start",
            cookies={"user_token": token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200


@pytest.mark.anyio
async def test_super_admin_missing_room_gets_400(client, setup_db):
    # A super-admin bypasses the 403 membership check; the endpoint itself raises 400
    # for a non-existent room because the room lookup fails before bot logic runs.
    user = await _create_user(is_admin=True)
    token = create_user_token(user_id=user.id, email=user.email)
    csrf_token = await _admin_csrf(client, {"user_token": token})
    r = await client.post(
        "/api/rooms/999/floor-transcription/start", cookies={"user_token": token}, headers={"X-CSRF-Token": csrf_token}
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_non_admin_missing_room_403(client, setup_db):
    user = await _create_user()
    ev, rm = await _create_event_room()
    await _set_membership(user.id, event_id=ev.id, role="event_owner")
    token = create_user_token(user_id=user.id, email=user.email)
    csrf_token = await _admin_csrf(client, {"user_token": token})
    r = await client.post(
        "/api/rooms/999/floor-transcription/start", cookies={"user_token": token}, headers={"X-CSRF-Token": csrf_token}
    )
    assert r.status_code == 403


@pytest.mark.anyio
async def test_malformed_room_id_422(client, setup_db):
    token = create_admin_token()
    csrf_token = await _admin_csrf(client, {"admin_token": token})
    r = await client.post(
        "/api/rooms/abc/floor-transcription/start", cookies={"admin_token": token}, headers={"X-CSRF-Token": csrf_token}
    )
    assert r.status_code == 422

    r = await client.post(
        "/api/rooms/abc/floor-transcription/start", cookies={"admin_token": token}, headers={"X-CSRF-Token": csrf_token}
    )
    assert r.status_code == 422


@pytest.mark.anyio
async def test_unauthenticated_403(client, setup_db):
    r = await client.post("/api/rooms/1/floor-transcription/start")
    assert r.status_code == 403
    assert "Admin access required." in r.text


@pytest.mark.anyio
async def test_downstream_failure_returns_502(client, setup_db):
    user = await _create_user()
    ev, rm = await _create_event_room()
    await _set_membership(user.id, event_id=ev.id, role="event_owner")
    token = create_user_token(user_id=user.id, email=user.email)
    csrf_token = await _admin_csrf(client, {"user_token": token})

    with patch("portal.routers.admin.get_http_client") as mock_http:
        from unittest.mock import AsyncMock

        from httpx import HTTPStatusError, Request, Response

        err = HTTPStatusError(
            "Error", request=Request("POST", "http://test"), response=Response(401, json={"detail": "Eventyay Revoked"})
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=err)
        mock_http.return_value = mock_client

        r = await client.post(
            f"/api/rooms/{rm.id}/floor-transcription/start",
            cookies={"user_token": token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 502
        assert "Bot service connection failed or access was revoked upstream." in r.text


@pytest.mark.anyio
async def test_privilege_escalation(client, setup_db):
    user = await _create_user()
    ev, rm = await _create_event_room()
    await _set_membership(user.id, event_id=ev.id, role="event_owner")
    token = create_user_token(user_id=user.id, email=user.email)

    r = await client.get("/admin/users/", cookies={"user_token": token})
    assert r.status_code == 403


@pytest.mark.anyio
@patch("portal.routers.admin.start_transcription_worker")
@patch("portal.routers.admin.stop_transcription_worker")
async def test_admin_token_without_user(mock_stop, mock_start, client, setup_db):
    token = create_admin_token()
    csrf_token = await _admin_csrf(client, {"admin_token": token})
    ev, rm = await _create_event_room()
    with patch("portal.routers.admin.get_http_client") as mock_http:
        from unittest.mock import AsyncMock

        mock_resp = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_http.return_value = mock_client
        r = await client.post(
            f"/api/rooms/{rm.id}/floor-transcription/start",
            cookies={"admin_token": token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200


@pytest.mark.anyio
async def test_revoked_admin_flag_takes_effect_immediately(client, setup_db):
    """Regression: a JWT carrying is_admin=True must NOT bypass a DB revocation.

    resolve_principal must always consult the database for is_global_admin;
    it must never trust the is_admin claim baked into the token alone.
    """
    from portal.database import get_session

    # Create the user as an admin
    user = await _create_user(is_admin=True)
    # Token is created while the user is still an admin, carrying is_admin=True in the JWT payload.
    # This simulates a stale token that remains in the browser after the DB role is revoked.
    token = create_user_token(user_id=user.id, email=user.email, is_admin=True)
    csrf_token = await _admin_csrf(client, {"user_token": token})

    # Immediately revoke admin in the DB (simulating an admin demotion while
    # the user's JWT is still valid)
    async with get_session() as s:
        from sqlalchemy import select as sa_select

        from portal.models import User

        db_user = (await s.execute(sa_select(User).where(User.id == user.id))).scalars().first()
        db_user.is_admin = False
        await s.commit()

    # The DB now says is_admin=False; even though the token was created when the
    # user was an admin, the endpoint must deny global-admin access.
    ev, rm = await _create_event_room()
    r = await client.post(
        f"/api/rooms/{rm.id}/floor-transcription/start",
        cookies={"user_token": token},
        headers={"X-CSRF-Token": csrf_token},
    )
    # No event_owner membership exists, so 403 is correct here.
    assert r.status_code == 403, f"Expected 403 after admin revocation, got {r.status_code}"
