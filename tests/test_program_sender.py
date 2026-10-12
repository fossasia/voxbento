from __future__ import annotations

from datetime import timedelta
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
async def test_start_rejected_when_another_source_is_live(environment):
    client, event, room, media = environment
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
    await start_session(client, event, room)
    stop = await client.delete(sender_url(event, room))
    assert stop.status_code == 200
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_session_secret_hash is None
        assert stored.floor_source == "program_ingest"  # Stopping does not switch the floor back.


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
