"""Backup contribution feed tests (#750)."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
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
from portal.crypto import decrypt_val
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
            ingest._configured_srt_paths.add(f"{event.slug}/{room.id}/floor")
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
    ingest._configured_srt_paths.clear()
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
        await ingest.generate_backup_srt_passphrase(stored, session)
        ingest._configured_srt_paths.add(f"{event.slug}/{room.id}/floor_backup")
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


@pytest.mark.anyio
async def test_backup_single_publisher_and_capacity_limits(backup_env, monkeypatch):
    client = backup_env["client"]
    event = backup_env["event"]
    room = backup_env["room"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        await ingest.generate_backup_srt_passphrase(stored, session)
        ingest._configured_srt_paths.add(f"{event.slug}/{room.id}/floor_backup")
        await session.commit()

    # 1. First backup publisher succeeds
    p1 = backup_publish(event, room)
    resp1 = await client.post("/internal/media-auth", json=p1)
    assert resp1.status_code == 204

    # 2. Concurrent second publisher gets 409
    p2 = backup_publish(event, room)
    resp2 = await client.post("/internal/media-auth", json=p2)
    assert resp2.status_code == 409
    assert "A backup floor publisher is already connected or connecting" in resp2.text

    # 3. Capacity limit 429 check
    monkeypatch.setattr(settings, "program_contribution_max", 1)
    async with get_session() as session:
        room2 = Room(
            event_id=event.id,
            display_name="Backup Room 2",
            floor_transcription_enabled=True,
            floor_language_code="en",
            floor_source="program_ingest",
            program_ingest_mode=relay.SRT_MODE,
            program_backup_enabled=True,
        )
        session.add(room2)
        await session.flush()
        await ingest.generate_srt_passphrase(room2, session)
        await ingest.generate_backup_srt_passphrase(room2, session)
        ingest._configured_srt_paths.add(f"{event.slug}/{room2.id}/floor_backup")
        await session.commit()

    p3 = backup_publish(event, room2)
    resp3 = await client.post("/internal/media-auth", json=p3)
    assert resp3.status_code == 429
    assert "Gateway at contribution capacity" in resp3.text


@pytest.mark.anyio
async def test_backup_credentials_revocation_kicks_backup(backup_env):
    client = backup_env["client"]
    event = backup_env["event"]
    room = backup_env["room"]
    calls = backup_env["calls"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        await ingest.generate_backup_srt_passphrase(stored, session)
        ingest._configured_srt_paths.add(f"{event.slug}/{room.id}/floor_backup")
        await session.commit()

    # Publish backup
    payload = backup_publish(event, room)
    resp = await client.post("/internal/media-auth", json=payload)
    assert resp.status_code == 204

    # Revoke credentials via POST
    revoke_resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "revoke"},
    )
    assert revoke_resp.status_code == 200

    # Verify both floor and floor_backup kick was attempted
    kick_calls = [url for method, url in calls if "kick" in url]
    assert any(payload["id"] in k for k in kick_calls)

    # Subsequent publish attempt is denied
    resp_after = await client.post("/internal/media-auth", json=payload)
    assert resp_after.status_code in (401, 403)


@pytest.mark.anyio
async def test_backup_srt_credentials_restored_on_restart(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    calls = backup_env["calls"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        await ingest.generate_srt_passphrase(stored, session)
        await ingest.generate_backup_srt_passphrase(stored, session)
        await session.commit()

    ingest._configured_srt_paths.clear()
    await ingest.reconcile_once()

    # Check that MediaMTX was called to configure both primary and backup paths
    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"
    configured_paths = [
        url for method, url in calls if "/v3/config/paths/add/" in url or "/v3/config/paths/patch/" in url
    ]
    assert any(primary_path in c for c in configured_paths)
    assert any(backup_path in c for c in configured_paths)


@pytest.mark.anyio
async def test_backup_ui_api_actions_toggle_and_promote(backup_env):
    client = backup_env["client"]
    event = backup_env["event"]
    room = backup_env["room"]

    # Toggle backup
    toggle_resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "toggle_backup", "backup_enabled": True},
    )
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["backup_enabled"] is True

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_backup_enabled is True
        stored.program_active_feed = "backup"
        await session.commit()

    # Promote primary
    promote_resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "promote_primary"},
    )
    assert promote_resp.status_code == 200
    assert promote_resp.json()["active_feed"] == "primary"

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "primary"


@pytest.mark.anyio
async def test_backup_captions_after_takeover_does_not_crash_language_validator(backup_env):
    import time

    event = backup_env["event"]
    room = backup_env["room"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        stored.program_active_feed = "backup"
        stored.floor_transcription_provider = "local"
        stored.floor_transcription_model = "tiny"
        await session.commit()

    path = ingest.MediaPath(
        name=f"{event.slug}/{room.id}/floor_backup",
        ready=True,
        source=ingest.MediaSource(id="sess-1", type="srtSource"),
        tracks=["H264", "AAC"],
        bytesReceived=1024,
    )
    health_entry = ingest.IngestHealth()
    booth_id = f"{event.slug}-{room.id}-floor"
    now = time.monotonic()

    # Must not raise ValueError: Invalid language code: 'floor_backup'
    await ingest.reconcile_processing(stored, event, path, health_entry, booth_id, now)


@pytest.mark.anyio
async def test_backup_separate_credentials_generated_and_isolated(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    client = backup_env["client"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        await ingest.configure_source(stored, event, "enable", 0, "srt")
        assert stored.program_backup_enabled is True
        assert stored.program_srt_passphrase_enc is not None
        assert stored.program_backup_key_enc is not None
        assert stored.program_srt_passphrase_enc != stored.program_backup_key_enc

        primary_pass = decrypt_val(stored.program_srt_passphrase_enc)
        backup_pass = decrypt_val(stored.program_backup_key_enc)
        assert primary_pass != backup_pass
        assert len(primary_pass) >= 16
        assert len(backup_pass) >= 16
        await session.commit()

    # Valid backup publish succeeds
    p_backup = backup_publish(event, room)
    resp = await client.post("/internal/media-auth", json=p_backup)
    assert resp.status_code == 204


@pytest.mark.anyio
async def test_stale_backup_active_feed_reset_on_source_change_or_disable(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    client = backup_env["client"]

    # 1. Start with room in backup active feed
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_backup_enabled = True
        stored.program_active_feed = "backup"
        await session.commit()

    # 2. Toggling backup off resets program_active_feed to primary
    toggle_resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "toggle_backup", "backup_enabled": False},
    )
    assert toggle_resp.status_code == 200
    assert toggle_resp.json()["backup_enabled"] is False

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "primary"

    # 3. Rotating or re-enabling source also resets active feed
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_active_feed = "backup"
        await session.commit()

    await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "rotate"},
    )
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "primary"


@pytest.mark.anyio
async def test_backup_first_startup_grace_period(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        stored.program_backup_connected_at = ingest.utc_now()
        await session.commit()

    backup_path = f"{event.slug}/{room.id}/floor_backup"
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "backup-sess", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 1024,
    }

    # 1. Immediately on cycle 0, primary is given a startup grace period, NOT promoted to backup
    await ingest.reconcile_once()
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "primary"

    # 2. After startup grace period elapsed (simulated by setting backup_connected_at 10s ago)
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_connected_at = ingest.utc_now() - ingest.timedelta(seconds=10)
        await session.commit()

    await ingest.reconcile_once()
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "backup"


@pytest.mark.anyio
async def test_primary_stall_triggers_backup_takeover(backup_env):
    import time

    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_connected_at = ingest.utc_now()
        stored.program_session_id = "primary-sess"
        stored.program_backup_enabled = True
        stored.program_backup_connected_at = ingest.utc_now()
        stored.program_active_feed = "primary"
        await session.commit()

    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"

    # Setup media: primary is stalled (not progressing), backup is receiving
    media[primary_path] = {
        "name": primary_path,
        "ready": True,
        "source": {"id": "primary-sess", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 50000,
    }
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "backup-sess", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 10000,
    }

    # Simulate primary health entry where last progress was 10s ago (stalled > TAKEOVER_STALL_SECS)
    now = time.monotonic()
    primary_health = ingest.IngestHealth()
    primary_health.last_bytes = 50000
    primary_health.last_progress = now - 10.0
    primary_health.backup_session_id = "backup-sess"
    primary_health.backup_connected = True
    ingest.health[room.id] = primary_health

    await ingest.reconcile_once()

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "backup"


@pytest.mark.anyio
async def test_takeover_preserves_single_consumer_guarantee(backup_env, monkeypatch):
    event = backup_env["event"]
    room = backup_env["room"]
    monkeypatch.setattr(settings, "program_relay_enabled", True)

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_ingest_mode = relay.RELAY_MODE
        stored.program_relay_server = "rtmps://a.rtmps.youtube.com/live2"
        from portal.crypto import encrypt_val

        stored.program_relay_key_enc = encrypt_val("test-key")
        stored.program_backup_enabled = True
        stored.program_active_feed = "primary"
        await session.commit()

    path = ingest.MediaPath(
        name=f"{event.slug}/{room.id}/floor",
        available=True,
        source=ingest.MediaSource(id="sess-1", type="rtmpConn"),
        tracks=["H264", "AAC"],
        bytesReceived=1024,
    )

    # 1. On primary, relay consumes floor path
    dest1 = ingest.relay_request(stored, event, path)
    assert dest1 is not None
    assert dest1[0] == f"{event.slug}/{room.id}/floor"
    assert "floor_backup" not in dest1[0]

    # 2. On backup failover, relay switches single-consumer consumption to floor_backup
    stored.program_active_feed = "backup"
    dest2 = ingest.relay_request(stored, event, path)
    assert dest2 is not None
    assert dest2[0] == f"{event.slug}/{room.id}/floor_backup"
    assert dest2[0] != f"{event.slug}/{room.id}/floor"


@pytest.mark.anyio
async def test_invalid_srt_passphrase_rejected(backup_env):
    client = backup_env["client"]
    event = backup_env["event"]
    room = backup_env["room"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        await ingest.generate_backup_srt_passphrase(stored, session)
        ingest._configured_srt_paths.add(f"{event.slug}/{room.id}/floor_backup")
        await session.commit()

    # Attempt to publish with wrong password
    bad_payload = backup_publish(event, room, password="wrong_passphrase_12345")
    response = await client.post("/internal/media-auth", json=bad_payload)
    # Media auth denies unauthenticated or invalid passphrases
    assert response.status_code in (401, 403)


@pytest.mark.anyio
async def test_unconfigured_srt_path_rejected_at_media_auth(backup_env):
    """Fail closed: if MediaMTX has not confirmed passphrase application, reject publish."""
    client = backup_env["client"]
    event = backup_env["event"]
    room = backup_env["room"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        await ingest.generate_backup_srt_passphrase(stored, session)
        await session.commit()

    backup_path = f"{event.slug}/{room.id}/floor_backup"
    ingest._configured_srt_paths.discard(backup_path)

    payload = backup_publish(event, room)
    response = await client.post("/internal/media-auth", json=payload)
    assert response.status_code in (401, 403)


@pytest.mark.anyio
async def test_backup_feed_allowed_when_max_rooms_limit_reached(backup_env, monkeypatch):
    """A redundant backup feed for an already-admitted room does not count as a new room."""
    client = backup_env["client"]
    event = backup_env["event"]
    room = backup_env["room"]

    monkeypatch.setattr(settings, "program_ingest_max_rooms", 1)

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_session_id = "primary-sess-uuid"
        stored.program_backup_enabled = True
        await ingest.generate_backup_srt_passphrase(stored, session)
        ingest._configured_srt_paths.add(f"{event.slug}/{room.id}/floor_backup")
        await session.commit()

    payload = backup_publish(event, room)
    response = await client.post("/internal/media-auth", json=payload)
    assert response.status_code == 204


@pytest.mark.anyio
async def test_backup_negotiation_grace_period_prevents_premature_disconnect(backup_env):
    """Pending backup reservation is preserved for bounded negotiation period."""
    room = backup_env["room"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        stored.program_backup_connected_at = ingest.utc_now()
        await session.commit()

    current = ingest.health.setdefault(room.id, ingest.IngestHealth())
    current.backup_session_id = "backup-sess-uuid"
    current.backup_reservation_started_at = time.monotonic()  # just reserved

    # Run reconcile_once when MediaMTX snapshot does not yet report backup ready
    await ingest.reconcile_once()

    async with get_session() as session:
        refreshed = await session.get(Room, room.id)
        # Must still be connected because negotiation grace period is active
        assert refreshed.program_backup_connected_at is not None
        assert current.backup_session_id == "backup-sess-uuid"


@pytest.mark.anyio
async def test_independent_backup_credential_validity(backup_env):
    """Expired primary credential does not invalidate or kick active backup feed."""
    room = backup_env["room"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        await ingest.generate_backup_srt_passphrase(stored, session)
        # Primary expired yesterday
        stored.program_srt_passphrase_expires_at = ingest.utc_now() - timedelta(days=1)
        # Backup valid for 30 days
        stored.program_backup_key_expires_at = ingest.utc_now() + timedelta(days=30)
        stored.program_backup_connected_at = ingest.utc_now()
        await session.commit()

    assert ingest.has_valid_credential(stored) is False
    assert ingest.has_valid_backup_credential(stored) is True

    # publish_permitted evaluates backup credential independently
    assert ingest.publish_permitted(stored, "srt", "", is_backup=True) is True
    assert ingest.publish_permitted(stored, "srt", "", is_backup=False) is False
