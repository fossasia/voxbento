"""Backup contribution feed tests (#750)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from fastapi_app import app
from portal import program_ingest as ingest
from portal import program_relay as relay
from portal.auth import create_user_token
from portal.booth_identity import make_backup_floor_path
from portal.config import settings
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import Event, Room

# Requests carry the token a real page would send.
pytestmark = pytest.mark.usefixtures("auto_csrf")


@pytest.fixture
async def backup_env(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "srt_ingest_enabled", True)
    monkeypatch.setattr(settings, "program_relay_enabled", False)
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", "")
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "program_ingest_disconnect_grace_secs", 0)

    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    relay.relay_health.clear()
    relay.list_outage.failures = 0

    media: dict[str, dict] = {}
    calls: list[tuple[str, str]] = []

    def control(request: httpx.Request):
        calls.append((request.method, str(request.url)))
        if "/v3/config/paths/" in request.url.path:
            return httpx.Response(200, json={})
        if "/paths/list" in request.url.path:
            return httpx.Response(200, json={"items": list(media.values())})
        if "/paths/get/" in request.url.path:
            name = request.url.path.split("/paths/get/")[1]
            return httpx.Response(200, json=media[name]) if name in media else httpx.Response(404)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(control)) as control_client:
        monkeypatch.setattr(ingest, "get_http_client", lambda: control_client)
        monkeypatch.setattr(relay, "get_http_client", lambda: control_client)
        start, stop = AsyncMock(), AsyncMock()

        async def started(**kwargs):
            ingest.audio_progress[kwargs["booth_id"]] = asyncio.get_running_loop().time()

        start.side_effect = started
        monkeypatch.setattr(ingest, "start_transcription_worker", start)
        monkeypatch.setattr(ingest, "stop_transcription_worker", stop)

        async with get_session() as session:
            admin = await create_user(
                session,
                email="backup-admin@example.com",
                display_name="Backup Admin",
                email_verified=True,
                is_admin=True,
            )
            event = Event(slug="backup-test", display_name="Backup Test")
            session.add(event)
            await session.flush()
            room = Room(
                event_id=event.id,
                display_name="Backup Room",
                floor_transcription_enabled=True,
                floor_language_code="en",
                floor_source="program_ingest",
                program_ingest_mode=relay.SRT_MODE,
                program_backup_enabled=False,
            )
            session.add(room)
            await session.flush()
            await ingest.generate_srt_passphrase(room, session)
            await session.commit()

        token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://test",
            cookies={"user_token": token},
        ) as client:
            yield {
                "client": client,
                "event": event,
                "room": room,
                "media": media,
                "calls": calls,
            }

    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    ingest.audio_progress.clear()
    relay.relay_health.clear()
    await dispose()


def backup_publish(event: Event, room: Room, **overrides) -> dict:
    return {
        "action": "publish",
        "path": f"{event.slug}/{room.id}/floor_backup",
        "protocol": "srt",
        "id": str(uuid4()),
        "ip": "198.51.100.1",
        **overrides,
    }


@pytest.mark.anyio
async def test_backup_path_rejected_when_disabled(backup_env):
    client = backup_env["client"]
    event = backup_env["event"]
    room = backup_env["room"]

    payload = backup_publish(event, room)
    response = await client.post("/internal/media-auth", json=payload)
    assert response.status_code == 403
    assert "Backup contribution feed is not enabled" in response.text


@pytest.mark.anyio
async def test_backup_path_accepted_when_enabled(backup_env):
    client = backup_env["client"]
    event = backup_env["event"]
    room = backup_env["room"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_backup_enabled = True
        await session.commit()

    payload = backup_publish(event, room)
    response = await client.post("/internal/media-auth", json=payload)
    assert response.status_code == 204

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        assert stored.program_backup_connected_at is not None
        connected_at = ingest.aware(stored.program_backup_connected_at)
        age = (datetime.now(timezone.utc) - connected_at).total_seconds()
        assert 0 <= age < 10


def test_floor_path_regex():
    # Valid floor paths
    m_floor = ingest.FLOOR_PATH.fullmatch("event-slug/12/floor")
    assert m_floor is not None
    assert m_floor.group(1) == "event-slug"
    assert m_floor.group(2) == "12"
    assert m_floor.group("backup") is None

    # Valid backup floor paths
    m_backup = ingest.FLOOR_PATH.fullmatch("event-slug/12/floor_backup")
    assert m_backup is not None
    assert m_backup.group(1) == "event-slug"
    assert m_backup.group(2) == "12"
    assert m_backup.group("backup") == "_backup"

    # Additional valid slug with multiple hyphens
    m_multi = ingest.FLOOR_PATH.fullmatch("pycon-us-2026/1/floor_backup")
    assert m_multi is not None
    assert m_multi.group(1) == "pycon-us-2026"
    assert m_multi.group(2) == "1"
    assert m_multi.group("backup") == "_backup"

    # Rejections
    assert ingest.FLOOR_PATH.fullmatch("event-slug/12/floor_backup_extra") is None
    assert ingest.FLOOR_PATH.fullmatch("event-slug/12/floor_other") is None
    assert ingest.FLOOR_PATH.fullmatch("event-slug/12/floor/") is None
    assert ingest.FLOOR_PATH.fullmatch("event-slug/0/floor") is None
    assert ingest.FLOOR_PATH.fullmatch("event-slug/floor") is None
    assert ingest.FLOOR_PATH.fullmatch("event-slug/12/backup") is None
    assert ingest.FLOOR_PATH.fullmatch("event_slug/12/floor") is None
    assert ingest.FLOOR_PATH.fullmatch("EVENT/12/floor") is None

    # Helper make_backup_floor_path
    constructed = make_backup_floor_path("demo-conf", 7)
    assert constructed == "demo-conf/7/floor_backup"
    m_constructed = ingest.FLOOR_PATH.fullmatch(constructed)
    assert m_constructed is not None
    assert m_constructed.group("backup") == "_backup"
