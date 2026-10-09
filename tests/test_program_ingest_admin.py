from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token
from portal.models import FLOOR_SOURCE_JITSI_BOT, FLOOR_SOURCE_PROGRAM_INGEST, Room


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


@pytest.fixture
def media_ops():
    """Stub every side effect on MediaMTX / floor-bot / workers triggered by the routes."""
    with (
        patch("portal.routers.program_ingest.kick_publisher", new=AsyncMock(return_value=True)) as kick,
        patch("portal.routers.program_ingest.remove_path_config", new=AsyncMock()) as remove,
        patch("portal.routers.program_ingest.request_floor_bot_stop", new=AsyncMock()) as bot_stop,
        patch("portal.routers.program_ingest.stop_floor_transcription_worker", new=AsyncMock()) as worker_stop,
    ):
        yield {"kick": kick, "remove": remove, "bot_stop": bot_stop, "worker_stop": worker_stop}


async def make_owner_and_room(slug: str = "ev1", role: str = "event_owner"):
    from portal.auth import hash_password
    from portal.database import create_event, create_room, create_user, get_session
    from portal.models import EventMembership, RoomMembership

    async with get_session() as s:
        user = await create_user(
            s, email=f"{slug}-{role}@test.com", display_name="T", password_hash=hash_password("pw")
        )
        ev = await create_event(s, slug=slug, display_name=slug)
        room = await create_room(s, event_id=ev.id, display_name="Main")
        room.floor_transcription_enabled = True
        room.floor_transcription_provider = "local"
        room.floor_transcription_model = "tiny"
        room.floor_language_code = "en"
        if role == "event_owner":
            s.add(EventMembership(user_id=user.id, event_id=ev.id, role="event_owner"))
        else:
            s.add(RoomMembership(user_id=user.id, room_id=room.id, role=role))
        await s.flush()
        token = create_user_token(user_id=user.id, email=user.email)
        return token, ev.id, room.id


def base(event_id: int, room_id: int, prefix: str = "/workspace") -> str:
    return f"{prefix}/events/{event_id}/rooms/{room_id}/program-ingest"


async def stored_room(room_id: int) -> Room:
    from portal.database import get_room_by_id, get_session

    async with get_session() as s:
        return await get_room_by_id(s, room_id)


@pytest.mark.anyio
async def test_status_defaults_to_disabled_bot_mode(client):
    token, ev_id, room_id = await make_owner_and_room()
    r = await client.get(f"{base(ev_id, room_id)}/status", cookies={"user_token": token})
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == FLOOR_SOURCE_JITSI_BOT
    assert data["state"] == "disabled"
    assert data["ingest_url"].endswith(f"/ev1/{room_id}/floor/whip")
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.anyio
async def test_enable_program_ingest_hands_floor_over(client, media_ops):
    token, ev_id, room_id = await make_owner_and_room()
    r = await client.post(
        f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies={"user_token": token}
    )
    assert r.status_code == 303
    assert (await stored_room(room_id)).floor_source_mode == FLOOR_SOURCE_PROGRAM_INGEST
    media_ops["bot_stop"].assert_awaited_once_with("ev1", room_id)
    media_ops["worker_stop"].assert_awaited_once_with("ev1", room_id)
    media_ops["remove"].assert_awaited_once_with(f"ev1/{room_id}/floor")

    status = (await client.get(f"{base(ev_id, room_id)}/status", cookies={"user_token": token})).json()
    assert status["enabled"] is True
    assert status["state"] == "waiting"
    assert any("No ingest credential" in w for w in status["warnings"])


@pytest.mark.anyio
async def test_switching_back_to_bot_releases_ingest(client, media_ops):
    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies=cookies)
    with patch("portal.routers.program_ingest.supervisor.release_room", new=AsyncMock()) as release:
        r = await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "jitsi_bot"}, cookies=cookies)
    assert r.status_code == 303
    release.assert_awaited_once_with(room_id)
    # Each switch disconnects before committing and re-checks afterwards.
    assert media_ops["kick"].await_count == 4
    assert (await stored_room(room_id)).floor_source_mode == FLOOR_SOURCE_JITSI_BOT


@pytest.mark.anyio
async def test_unknown_mode_rejected(client, media_ops):
    token, ev_id, room_id = await make_owner_and_room()
    r = await client.post(
        f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "youtube"}, cookies={"user_token": token}
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_secret_is_returned_once_and_never_again(client, media_ops):
    from portal.program_ingest.credentials import verify_room_ingest_secret

    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies=cookies)

    r = await client.post(f"{base(ev_id, room_id)}/credential", json={"expires_in_days": 7}, cookies=cookies)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    issued = r.json()
    secret = issued["secret"]
    assert secret.startswith("vbi_") and issued["expires_at"]
    assert issued["hint"] == secret[-4:]

    room = await stored_room(room_id)
    assert verify_room_ingest_secret(room, secret)
    assert secret not in repr(room.__dict__)

    status = await client.get(f"{base(ev_id, room_id)}/status", cookies=cookies)
    assert secret not in status.text
    assert room.program_ingest_secret_hash not in status.text
    assert status.json()["credential"]["configured"] is True

    page = await client.get(f"/workspace/events/{ev_id}/rooms/{room_id}/", cookies=cookies)
    assert page.status_code == 200
    assert secret not in page.text
    assert room.program_ingest_secret_hash not in page.text
    assert "Keep streaming directly to YouTube; add VoxBento as a second destination." in page.text
    assert "stream key" in page.text.lower()
    assert 'name="youtube' not in page.text.lower()


@pytest.mark.anyio
async def test_rotation_kicks_current_publisher_and_invalidates_old_secret(client, media_ops):
    from portal.program_ingest.credentials import verify_room_ingest_secret

    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies=cookies)
    first = (await client.post(f"{base(ev_id, room_id)}/credential", json={}, cookies=cookies)).json()["secret"]
    kicks_before = media_ops["kick"].await_count
    second = (await client.post(f"{base(ev_id, room_id)}/credential", json={}, cookies=cookies)).json()["secret"]

    assert media_ops["kick"].await_count == kicks_before + 2
    room = await stored_room(room_id)
    assert not verify_room_ingest_secret(room, first)
    assert verify_room_ingest_secret(room, second)


@pytest.mark.anyio
async def test_revoke_clears_secret_and_kicks(client, media_ops):
    from portal.program_ingest.credentials import verify_room_ingest_secret

    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies=cookies)
    secret = (await client.post(f"{base(ev_id, room_id)}/credential", json={}, cookies=cookies)).json()["secret"]

    r = await client.post(f"{base(ev_id, room_id)}/credential/revoke", cookies=cookies)
    assert r.status_code == 200
    room = await stored_room(room_id)
    assert room.program_ingest_secret_hash is None
    assert not verify_room_ingest_secret(room, secret)
    media_ops["kick"].assert_awaited_with(f"ev1/{room_id}/floor")


@pytest.mark.anyio
async def test_invalid_expiry_rejected(client, media_ops):
    token, ev_id, room_id = await make_owner_and_room()
    r = await client.post(
        f"{base(ev_id, room_id)}/credential", json={"expires_in_days": 5}, cookies={"user_token": token}
    )
    assert r.status_code == 400


@pytest.mark.anyio
async def test_room_coordinator_cannot_manage_ingest(client, media_ops):
    token, ev_id, room_id = await make_owner_and_room(role="room_coordinator")
    cookies = {"user_token": token}
    assert (await client.post(f"{base(ev_id, room_id)}/credential", json={}, cookies=cookies)).status_code == 403
    assert (await client.get(f"{base(ev_id, room_id)}/status", cookies=cookies)).status_code == 403


@pytest.mark.anyio
async def test_anonymous_cannot_manage_ingest(client, media_ops):
    _token, ev_id, room_id = await make_owner_and_room()
    assert (await client.post(f"{base(ev_id, room_id)}/credential", json={})).status_code == 403


@pytest.mark.anyio
async def test_owner_of_other_event_cannot_reach_room(client, media_ops):
    token_a, ev_a, _room_a = await make_owner_and_room("ev-a")
    _token_b, ev_b, room_b = await make_owner_and_room("ev-b")
    cookies = {"user_token": token_a}
    # Own event id with a foreign room → 404; foreign event id → 403.
    assert (await client.get(f"{base(ev_a, room_b)}/status", cookies=cookies)).status_code == 404
    assert (await client.get(f"{base(ev_b, room_b)}/status", cookies=cookies)).status_code == 403


@pytest.mark.anyio
async def test_sync_offset_saved_and_validated(client, media_ops):
    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    r = await client.post(
        f"{base(ev_id, room_id)}/sync-offset", data={"program_sync_offset_ms": "4500"}, cookies=cookies
    )
    assert r.status_code == 303
    assert (await stored_room(room_id)).program_sync_offset_ms == 4500
    for bad in ("-1", "30001", "abc"):
        r = await client.post(
            f"{base(ev_id, room_id)}/sync-offset", data={"program_sync_offset_ms": bad}, cookies=cookies
        )
        assert r.status_code == 400


@pytest.mark.anyio
async def test_workspace_redirects_and_admin_namespace_for_super_admin(client, media_ops):
    from portal.auth import create_admin_token

    token, ev_id, room_id = await make_owner_and_room()
    r = await client.post(
        f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies={"user_token": token}
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/workspace/events/{ev_id}/rooms/{room_id}/"

    # Organizers using the legacy /admin URL are redirected into /workspace.
    legacy = await client.get(f"{base(ev_id, room_id, '/admin')}/status", cookies={"user_token": token})
    assert legacy.status_code == 307
    assert legacy.headers["location"].startswith("/workspace/")

    admin = {"admin_token": create_admin_token()}
    assert (await client.get(f"{base(ev_id, room_id, '/admin')}/status", cookies=admin)).status_code == 200


@pytest.mark.anyio
async def test_floor_bot_refused_while_program_ingest_owns_room(client, media_ops):
    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies=cookies)
    with (
        patch("portal.routers.admin.get_http_client") as http,
        patch("portal.routers.admin.start_transcription_worker") as start_worker,
    ):
        r = await client.post(f"/api/rooms/{room_id}/floor-transcription/start", cookies=cookies)
    assert r.status_code == 409
    assert "Program Stream Ingest" in r.json()["detail"]
    http.assert_not_called()
    start_worker.assert_not_called()


@pytest.mark.anyio
async def test_room_page_hides_bot_controls_in_program_mode(client, media_ops):
    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    page = await client.get(f"/workspace/events/{ev_id}/rooms/{room_id}/", cookies=cookies)
    assert 'id="start-bot-btn"' in page.text
    await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies=cookies)
    page = await client.get(f"/workspace/events/{ev_id}/rooms/{room_id}/", cookies=cookies)
    assert 'id="start-bot-btn"' not in page.text
    assert "program-ingest.js" in page.text


@pytest.mark.anyio
async def test_timestamps_are_reported_as_utc(client, media_ops):
    from datetime import datetime

    from portal.database import get_session

    token, ev_id, room_id = await make_owner_and_room()
    async with get_session() as s:
        (await s.get(Room, room_id)).program_ingest_last_connected_at = datetime(2026, 10, 8, 9, 30)
    data = (await client.get(f"{base(ev_id, room_id)}/status", cookies={"user_token": token})).json()
    assert data["last_connected_at"] == "2026-10-08T09:30:00+00:00"


@pytest.mark.anyio
async def test_mode_switch_aborts_when_publisher_cannot_be_disconnected(client, media_ops):
    from portal.program_ingest.mediamtx import PublisherKickError

    token, ev_id, room_id = await make_owner_and_room()
    media_ops["kick"].side_effect = PublisherKickError("mediamtx down")
    r = await client.post(
        f"{base(ev_id, room_id)}/mode",
        data={"floor_source_mode": "program_ingest"},
        cookies={"user_token": token},
        headers={"Accept": "application/json"},
    )
    assert r.status_code == 503
    assert "nothing was changed" in r.json()["detail"]
    assert (await stored_room(room_id)).floor_source_mode == FLOOR_SOURCE_JITSI_BOT
    media_ops["bot_stop"].assert_not_awaited()
    media_ops["worker_stop"].assert_not_awaited()


@pytest.mark.anyio
async def test_rotation_aborts_when_publisher_cannot_be_disconnected(client, media_ops):
    from portal.program_ingest.credentials import verify_room_ingest_secret
    from portal.program_ingest.mediamtx import PublisherKickError

    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies=cookies)
    first = (await client.post(f"{base(ev_id, room_id)}/credential", json={}, cookies=cookies)).json()["secret"]

    media_ops["kick"].side_effect = PublisherKickError("mediamtx down")
    r = await client.post(f"{base(ev_id, room_id)}/credential", json={}, cookies=cookies)
    assert r.status_code == 503
    assert "secret" not in r.json()
    assert verify_room_ingest_secret(await stored_room(room_id), first)

    r = await client.post(f"{base(ev_id, room_id)}/credential/revoke", cookies=cookies)
    assert r.status_code == 503
    assert verify_room_ingest_secret(await stored_room(room_id), first)


@pytest.mark.anyio
async def test_first_secret_needs_no_disconnect(client, media_ops):
    from portal.program_ingest.mediamtx import PublisherKickError

    token, ev_id, room_id = await make_owner_and_room()
    cookies = {"user_token": token}
    await client.post(f"{base(ev_id, room_id)}/mode", data={"floor_source_mode": "program_ingest"}, cookies=cookies)
    media_ops["kick"].side_effect = PublisherKickError("mediamtx down")
    r = await client.post(f"{base(ev_id, room_id)}/credential", json={}, cookies=cookies)
    assert r.status_code == 200
    assert r.json()["secret"].startswith("vbi_")
