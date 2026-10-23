from __future__ import annotations

from datetime import timedelta
from unittest import mock
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from fastapi_app import app
from portal import program_ingest as ingest
from portal.auth import create_user_token
from portal.config import settings
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import Event, Room


@pytest.fixture
async def environment(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", "")
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "program_sender_session_ttl_secs", 150)
    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    media: dict = {}

    def control(request):
        if "/paths/list" in request.url.path:
            return httpx.Response(200, json={"items": list(media.values())})
        if "/paths/get/" in request.url.path:
            name = request.url.path.split("/paths/get/")[1]
            return httpx.Response(200, json=media[name]) if name in media else httpx.Response(404)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(control)) as media_client:
        monkeypatch.setattr(ingest, "get_http_client", lambda: media_client)
        monkeypatch.setattr(ingest, "start_transcription_worker", AsyncMock())
        monkeypatch.setattr(ingest, "stop_transcription_worker", AsyncMock())
        async with get_session() as session:
            admin = await create_user(
                session,
                email="sender-admin@example.com",
                display_name="Sender Admin",
                email_verified=True,
                is_admin=True,
            )
            event = Event(slug="sender-test", display_name="Sender Test")
            session.add(event)
            await session.flush()
            room = Room(event_id=event.id, display_name="Room", floor_transcription_enabled=True, floor_language_code="en")
            session.add(room)
        admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://test",
            cookies={"user_token": admin_token},
        ) as client:
            yield client, event, room, media
    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    await dispose()


def sender_url(event, room):
    return f"/admin/events/{event.id}/rooms/{room.id}/program-sender"


def whip_credentials(room, token, **overrides):
    return {
        "action": "publish",
        "path": f"sender-test/{room.id}/floor",
        "protocol": "webrtc",
        "token": token,
        "id": str(uuid4()),
        "ip": "198.51.100.7",
        **overrides,
    }


async def start_session(client, event, room):
    response = await client.post(sender_url(event, room), json={"sync_offset_ms": 0})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.anyio
async def test_start_mints_short_lived_session_and_claims_floor(environment):
    client, event, room, _ = environment
    data = await start_session(client, event, room)
    assert data["secret"] and data["endpoint"].endswith(f"/sender-test/{room.id}/floor/whip")
    assert data["expires_in"] == 150
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.floor_source == "program_ingest"
        assert stored.program_session_secret_hash is not None
        assert stored.program_key_hash is None  # The browser session never mints the long-lived encoder key.
        ttl = ingest.aware(stored.program_session_secret_expires_at) - ingest.utc_now()
        assert timedelta(seconds=60) < ttl <= timedelta(seconds=150)


@pytest.mark.anyio
async def test_session_secret_authorizes_whip_publish(environment):
    client, event, room, _ = environment
    data = await start_session(client, event, room)
    auth = await client.post("/internal/media-auth", json=whip_credentials(room, data["secret"]))
    assert auth.status_code == 204


@pytest.mark.anyio
async def test_start_rejected_when_program_ingest_source_is_live(environment):
    client, event, room, media = environment
    async with get_session() as session:
        (await session.get(Room, room.id)).floor_source = "program_ingest"
    media[f"sender-test/{room.id}/floor"] = {
        "name": f"sender-test/{room.id}/floor",
        "ready": True,
        "source": {"type": "webRTCSession", "id": str(uuid4())},
        "tracks2": [{"codec": "Opus"}],
        "bytesReceived": 100,
    }
    response = await client.post(sender_url(event, room), json={"sync_offset_ms": 0})
    assert response.status_code == 409


@pytest.mark.anyio
async def test_start_takes_over_idle_jitsi_bot(environment, monkeypatch):
    client, event, room, _ = environment
    stop_calls = []
    real_client = ingest.get_http_client()
    orig_post = real_client.post

    async def spy_post(url, **kwargs):
        if url.endswith("/stop"):
            stop_calls.append(url)
        return await orig_post(url, **kwargs)

    monkeypatch.setattr(real_client, "post", spy_post)
    data = await start_session(client, event, room)
    assert data["secret"]
    assert any("/stop" in url for url in stop_calls)  # Bot takeover stops the Jitsi bot.
    async with get_session() as session:
        assert (await session.get(Room, room.id)).floor_source == "program_ingest"


@pytest.mark.anyio
async def test_close_does_not_kick_an_encoder(environment):
    client, event, room, media = environment
    encoder_session = str(uuid4())
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.floor_source = "program_ingest"
        stored.program_key_hash = "encoderhash"
        stored.program_session_id = encoder_session  # Encoder owns the floor, no browser session.
    kicks = []
    orig_kick = ingest.kick

    async def spy_kick(path, source):
        if source is not None:
            kicks.append(source.id)
        return await orig_kick(path, source)

    with mock.patch.object(ingest, "kick", spy_kick):
        stop = await client.delete(sender_url(event, room))
    assert stop.status_code == 200
    assert encoder_session not in kicks  # The encoder was never kicked.
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_key_hash == "encoderhash"
        assert stored.program_session_id == encoder_session


@pytest.mark.anyio
async def test_duplicate_start_rejected_while_pending(environment):
    client, event, room, _ = environment
    await start_session(client, event, room)  # First start mints and marks the session pending.
    second = await client.post(sender_url(event, room), json={"sync_offset_ms": 0})
    assert second.status_code == 409


@pytest.mark.anyio
async def test_revoke_also_clears_sender_session_secret(environment):
    client, event, room, _ = environment
    await client.post(f"/admin/events/{event.id}/rooms/{room.id}/program-ingest", json={"action": "enable"})
    await start_session(client, event, room)  # Now both encoder key and session secret exist.
    async with get_session() as session:
        (await session.get(Room, room.id)).program_session_id = str(uuid4())  # Simulate a connected publisher.
    revoke = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest", json={"action": "revoke"}
    )
    assert revoke.status_code in {200, 202}
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_session_secret_hash is None
        assert stored.program_key_hash is None
        assert stored.program_session_id is None  # Reservation cleared so reconcile cannot keep it live.


@pytest.mark.anyio
async def test_reconcile_keeps_live_session_past_secret_expiry(environment):
    client, event, room, media = environment
    data = await start_session(client, event, room)
    session_id = str(uuid4())
    path_name = f"sender-test/{room.id}/floor"
    media[path_name] = {
        "name": path_name,
        "ready": True,
        "source": {"type": "webRTCSession", "id": session_id},
        "tracks2": [{"codec": "Opus"}],
        "bytesReceived": 500,
    }
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_session_id = session_id  # Publisher is connected.
        stored.program_session_secret_expires_at = ingest.utc_now() - timedelta(seconds=1)  # TTL elapsed.
        stored.program_connected_at = ingest.utc_now()
        stored.floor_transcription_enabled = False  # Force an early return without touching providers.
    assert data["secret"]
    kicks = []
    orig_kick = ingest.kick

    async def spy_kick(path, source):
        if source is not None:
            kicks.append(source.id)
        return await orig_kick(path, source)

    from portal.models import Event as EventModel

    with mock.patch.object(ingest, "kick", spy_kick):
        async with get_session() as session:
            stored = await session.get(Room, room.id)
            ev = await session.get(EventModel, event.id)
            await ingest.reconcile_room(stored, ev, ingest.MediaPath.model_validate(media[path_name]))
    assert session_id not in kicks  # The connected publisher survives TTL expiry.


@pytest.mark.anyio
async def test_start_preserves_existing_encoder_key(environment):
    client, event, room, _ = environment
    # Enable an external encoder first (mints the 30-day key), then open a browser session.
    enabled = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest", json={"action": "enable"}
    )
    assert enabled.status_code == 200
    async with get_session() as session:
        encoder_hash = (await session.get(Room, room.id)).program_key_hash
    await start_session(client, event, room)
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_key_hash == encoder_hash  # Encoder credential untouched.
        assert stored.program_session_secret_hash is not None


@pytest.mark.anyio
async def test_expired_session_secret_is_denied(environment):
    client, event, room, _ = environment
    data = await start_session(client, event, room)
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_session_secret_expires_at = ingest.utc_now() - timedelta(seconds=1)
    auth = await client.post("/internal/media-auth", json=whip_credentials(room, data["secret"]))
    assert auth.status_code == 401


@pytest.mark.anyio
async def test_stop_releases_only_the_sender_session(environment):
    client, event, room, _ = environment
    data = await start_session(client, event, room)
    stop = await client.delete(sender_url(event, room), headers={"X-Program-Session": data["secret"]})
    assert stop.status_code == 200
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_session_secret_hash is None
        assert stored.floor_source == "program_ingest"  # Stopping does not switch the floor back.


@pytest.mark.anyio
async def test_stop_without_matching_secret_is_a_noop(environment):
    client, event, room, _ = environment
    await start_session(client, event, room)
    # A stale or foreign DELETE (wrong/absent secret) must not release the live session.
    stop = await client.delete(sender_url(event, room), headers={"X-Program-Session": "not-the-secret"})
    assert stop.status_code == 200
    async with get_session() as session:
        assert (await session.get(Room, room.id)).program_session_secret_hash is not None


@pytest.mark.anyio
async def test_stop_without_secret_header_is_a_noop(environment):
    client, event, room, _ = environment
    await start_session(client, event, room)
    # No X-Program-Session header at all: the live session must be left intact.
    stop = await client.delete(sender_url(event, room))
    assert stop.status_code == 200
    async with get_session() as session:
        assert (await session.get(Room, room.id)).program_session_secret_hash is not None


@pytest.mark.anyio
async def test_stop_clears_reservation_so_restart_succeeds(environment):
    client, event, room, _ = environment
    data = await start_session(client, event, room)  # Mints a secret and a pending reservation.
    await client.delete(sender_url(event, room), headers={"X-Program-Session": data["secret"]})
    # Reservation cleared on release, so an immediate restart is not rejected as pending.
    restart = await client.post(sender_url(event, room), json={"sync_offset_ms": 0})
    assert restart.status_code == 200


@pytest.mark.anyio
async def test_start_requires_operator_enabled(environment, monkeypatch):
    client, event, room, _ = environment
    monkeypatch.setattr(settings, "program_ingest_enabled", False)
    response = await client.post(sender_url(event, room), json={"sync_offset_ms": 0})
    assert response.status_code == 409


@pytest.mark.anyio
async def test_start_rejects_room_event_mismatch(environment):
    client, event, room, _ = environment
    response = await client.post(f"/admin/events/{event.id + 999}/rooms/{room.id}/program-sender", json={})
    assert response.status_code in {403, 404}


@pytest.mark.anyio
async def test_start_without_offset_keeps_saved_sync_delay(environment):
    client, event, room, _ = environment
    async with get_session() as session:
        (await session.get(Room, room.id)).program_sync_offset_ms = 2500
    response = await client.post(sender_url(event, room), json={})
    assert response.status_code == 200, response.text
    async with get_session() as session:
        assert (await session.get(Room, room.id)).program_sync_offset_ms == 2500


@pytest.mark.anyio
async def test_standalone_sender_page_renders(environment):
    client, event, room, _ = environment
    response = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/sender/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert f"/rooms/{room.id}/program-sender" in response.text
    assert "/static/js/program-sender.js" in response.text


@pytest.mark.anyio
async def test_sender_page_rejects_room_event_mismatch(environment):
    client, event, room, _ = environment
    response = await client.get(f"/admin/events/{event.id + 999}/rooms/{room.id}/sender/")
    assert response.status_code in {403, 404}


def test_workspace_maps_sender_routes():
    from portal.workspace_routing import workspace_to_admin_path

    assert workspace_to_admin_path("/workspace/events/3/rooms/7/sender/") == "/admin/events/3/rooms/7/sender/"
    assert workspace_to_admin_path("/workspace/events/3/rooms/7/program-sender") == "/admin/events/3/rooms/7/program-sender"
