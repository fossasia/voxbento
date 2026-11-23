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
    class ConfiguredPaths(dict):
        def add(self, name: str, pass_: str = "pass"):
            self[name] = pass_

        def discard(self, name: str):
            self.pop(name, None)

    configured_paths = ConfiguredPaths()

    def control(request: httpx.Request):
        calls.append((request.method, str(request.url)))
        data = json.loads(request.read()) if request.content else {}
        if "/v3/config/paths/get/" in request.url.path:
            name = request.url.path.split("/v3/config/paths/get/")[1]
            if name in configured_paths:
                return httpx.Response(200, json={"name": name, "srtPublishPassphrase": configured_paths[name]})
            return httpx.Response(404, json={})
        if "/v3/config/paths/list" in request.url.path:
            return httpx.Response(
                200, json={"items": [{"name": k, "srtPublishPassphrase": v} for k, v in configured_paths.items()]}
            )
        if "/v3/config/paths/add/" in request.url.path or "/v3/config/paths/patch/" in request.url.path:
            name = (
                request.url.path.split("/v3/config/paths/add/")[1]
                if "/v3/config/paths/add/" in request.url.path
                else request.url.path.split("/v3/config/paths/patch/")[1]
            )
            configured_paths[name] = data.get("srtPublishPassphrase", "pass")
            return httpx.Response(200, json={})
        if "/v3/config/paths/delete/" in request.url.path:
            name = request.url.path.split("/v3/config/paths/delete/")[1]
            configured_paths.discard(name)
            return httpx.Response(200, json={})
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

        _backup_passphrases.clear()
        orig_gen_backup = ingest.generate_backup_srt_passphrase

        async def _tracking_gen_backup(r):
            p = await orig_gen_backup(r)
            _backup_passphrases[r.id] = p
            return p

        monkeypatch.setattr(ingest, "generate_backup_srt_passphrase", _tracking_gen_backup)

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
            primary_pass = await ingest.generate_srt_passphrase(room)
            configured_paths.add(f"{event.slug}/{room.id}/floor", primary_pass)
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
                "start": start,
                "stop": stop,
                "configured_paths": configured_paths,
            }

    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    ingest.audio_progress.clear()
    configured_paths.clear()
    relay.relay_health.clear()
    await dispose()


_backup_passphrases: dict[int, str] = {}


def backup_publish(event: Event, room: Room, **overrides) -> dict:
    if room.program_backup_key_enc:
        passphrase = decrypt_val(room.program_backup_key_enc)
    elif room.id in _backup_passphrases:
        passphrase = _backup_passphrases[room.id]
    else:
        passphrase = "test-pass"
    return {
        "action": "publish",
        "path": f"{event.slug}/{room.id}/floor_backup",
        "protocol": "srt",
        "id": str(uuid4()),
        "ip": "198.51.100.1",
        "password": passphrase,
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
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
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
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
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
        await ingest.generate_srt_passphrase(room2)
        pass_b2 = await ingest.generate_backup_srt_passphrase(room2)
        backup_env["configured_paths"].add(f"{event.slug}/{room2.id}/floor_backup", pass_b2)
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
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
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
        await ingest.generate_srt_passphrase(stored)
        await ingest.generate_backup_srt_passphrase(stored)
        await session.commit()

    backup_env["configured_paths"].clear()
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

    # 1. Promote primary when primary feed is not connected -> 409
    promote_resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "promote_primary"},
    )
    assert promote_resp.status_code == 409
    assert "not connected" in promote_resp.text

    # 2. Promote primary when primary feed has 0 bytes -> 409
    media = backup_env["media"]
    primary_path = f"{event.slug}/{room.id}/floor"
    media[primary_path] = {
        "name": primary_path,
        "ready": True,
        "source": {"id": "prim-sess", "type": "srtSource"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 0,
    }
    promote_resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "promote_primary"},
    )
    assert promote_resp.status_code == 409
    assert "not sending media" in promote_resp.text

    # 3. Promote primary when stalled -> 409
    media[primary_path]["bytesReceived"] = 2048
    ingest.health[room.id] = ingest.IngestHealth(
        primary_session_id="prim-sess",
        primary_last_bytes=2048,
        primary_last_progress=time.monotonic() - 10.0,
    )
    promote_resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "promote_primary"},
    )
    assert promote_resp.status_code == 409
    assert "stalled" in promote_resp.text

    # 4. Promote primary when healthy and progressing -> 200
    ingest.health[room.id].primary_last_progress = time.monotonic()
    promote_resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-ingest",
        json={"action": "promote_primary"},
    )
    assert promote_resp.status_code == 200
    assert promote_resp.json()["active_feed"] == "primary"

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "primary"

    # Regression test: verify promote_primary stops the worker so reconcile restarts on primary
    stop = backup_env["stop"]
    assert stop.called


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
    p_backup = backup_publish(event, stored)
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
    primary_health.primary_last_bytes = 50000
    primary_health.primary_last_progress = now - 10.0
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
async def test_active_feed_stall_detected_after_takeover_when_counters_unchanged(backup_env):
    import time

    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_connected_at = ingest.utc_now()
        stored.program_session_id = "primary-sess"
        stored.program_backup_enabled = True
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
        stored.program_backup_connected_at = ingest.utc_now()
        stored.program_active_feed = "backup"
        await session.commit()

    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"

    # Both feeds are present in media, but neither is advancing bytes
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

    now = time.monotonic()
    entry = ingest.IngestHealth()
    entry.primary_last_bytes = 50000
    entry.primary_last_progress = now - 20.0
    entry.backup_session_id = "backup-sess"
    entry.backup_connected = True
    # Active feed (backup) progress stopped longer than stall threshold
    entry.last_bytes = 10000
    entry.last_progress = now - (settings.program_ingest_stall_secs + 2.0)
    ingest.health[room.id] = entry

    await ingest.reconcile_once()

    current = ingest.health[room.id]
    assert current.state == "degraded"
    assert current.ingest_state == "degraded"
    assert "media has stalled" in current.reason.lower()


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
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
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
        await ingest.generate_backup_srt_passphrase(stored)
        await session.commit()

    backup_path = f"{event.slug}/{room.id}/floor_backup"
    backup_env["configured_paths"].discard(backup_path)

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
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
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
        await ingest.generate_backup_srt_passphrase(stored)
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


@pytest.mark.anyio
async def test_primary_counter_reset_on_reconnect_does_not_trigger_false_stall(backup_env):
    """When primary reconnects and its byte counter resets to a lower value, baseline is reset."""
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_connected_at = ingest.utc_now()
        stored.program_session_id = "primary-sess-2"
        stored.program_backup_enabled = True
        stored.program_active_feed = "primary"
        await session.commit()

    primary_path = f"{event.slug}/{room.id}/floor"
    media[primary_path] = {
        "name": primary_path,
        "ready": True,
        "source": {"id": "primary-sess-2", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 1000,  # Lower than previous 500,000 bytes
    }

    now = time.monotonic()
    entry = ingest.IngestHealth()
    entry.primary_session_id = "primary-sess-1"  # Old session
    entry.primary_last_bytes = 500000
    entry.primary_last_progress = now - 4.0
    ingest.health[room.id] = entry

    await ingest.reconcile_once()

    current = ingest.health[room.id]
    # Session change / counter decrease must reset primary_last_bytes to 1000 and refresh progress
    assert current.primary_last_bytes == 1000
    assert current.primary_session_id == "primary-sess-2"
    assert current.primary_last_progress >= now


@pytest.mark.anyio
async def test_primary_first_connect_with_older_backup_does_not_fail_startup(backup_env):
    """Primary connecting for the first time while backup is older than 5.0s is not failed."""
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_connected_at = None  # First time connect
        stored.program_session_id = "primary-sess-1"
        stored.program_backup_enabled = True
        # Backup connected 10 seconds ago
        stored.program_backup_connected_at = ingest.utc_now() - timedelta(seconds=10)
        stored.program_active_feed = "primary"
        await session.commit()

    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"
    media[primary_path] = {
        "name": primary_path,
        "ready": True,
        "source": {"id": "primary-sess-1", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 5000,
    }
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "backup-sess-1", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 15000,
    }

    await ingest.reconcile_once()

    async with get_session() as session:
        refreshed = await session.get(Room, room.id)
        # Primary is connected, so no takeover should happen despite backup age >= 5s
        assert refreshed.program_active_feed == "primary"


@pytest.mark.anyio
async def test_bitrate_sampling_reset_on_feed_takeover_prevents_false_limits_kick(backup_env):
    """When switching feeds, bitrate tracking is reset so large delta_bytes does not trigger a limits kick."""
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_connected_at = ingest.utc_now()
        stored.program_backup_enabled = True
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
        stored.program_backup_connected_at = ingest.utc_now()
        stored.program_active_feed = "primary"
        await session.commit()

    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"

    # Primary is stalled, backup is receiving with 200,000 bytes
    media[primary_path] = {
        "name": primary_path,
        "ready": True,
        "source": {"id": "primary-sess-1", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 1000,
    }
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "backup-sess-1", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 200000,
    }

    now = time.monotonic()
    entry = ingest.IngestHealth()
    entry.primary_last_bytes = 1000
    entry.primary_last_progress = now - 10.0  # Stalled
    entry.backup_session_id = "backup-sess-1"
    entry.backup_connected = True
    entry.last_active_path = primary_path
    # Simulate primary having high bitrate samples
    entry.high_bitrate_cycles = 4
    entry.last_cycle_bytes = 1000
    entry.last_cycle_time = now - 1.0
    ingest.health[room.id] = entry

    await ingest.reconcile_once()

    current = ingest.health[room.id]
    # Backup takeover occurred
    async with get_session() as session:
        refreshed = await session.get(Room, room.id)
        assert refreshed.program_active_feed == "backup"

    # Crucially, backup must NOT be kicked for limits
    assert current.state != "rejected_limits"
    assert current.high_bitrate_cycles == 0
    assert current.last_cycle_bytes == 200000


@pytest.mark.anyio
async def test_open_sender_session_rejects_srt_mode_and_active_backup(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    client = backup_env["client"]
    media = backup_env["media"]

    # 1. SRT mode rejected with 409
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.floor_source = "program_ingest"
        stored.program_ingest_mode = "srt"
        await session.commit()

    resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-sender",
        json={"sync_offset_ms": 0},
        headers={"Origin": settings.public_base_url},
    )
    assert resp.status_code == 409
    assert "An SRT encoder owns the floor" in resp.json()["detail"]

    # 2. WHIP mode with active backup publishing: rejected with 409 even if primary is absent
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_ingest_mode = "whip"
        stored.program_backup_enabled = True
        await session.commit()

    backup_path = f"{event.slug}/{room.id}/floor_backup"
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "backup-sess-1", "type": "srtConn"},
    }
    resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-sender",
        json={"sync_offset_ms": 0},
        headers={"Origin": settings.public_base_url},
    )
    assert resp.status_code == 409
    assert "Another source is publishing to the floor" in resp.json()["detail"]


@pytest.mark.anyio
async def test_open_sender_session_takeover_cleans_backup_state(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    client = backup_env["client"]
    media = backup_env["media"]

    # Jitsi room with backup enabled and active feed set to backup
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.floor_source = "jitsi"
        stored.program_backup_enabled = True
        stored.program_backup_connected_at = ingest.utc_now()
        stored.program_active_feed = "backup"
        await session.commit()

    backup_path = f"{event.slug}/{room.id}/floor_backup"
    media[backup_path] = {
        "name": backup_path,
        "ready": False,
        "source": None,
    }

    resp = await client.post(
        f"/admin/events/{event.id}/rooms/{room.id}/program-sender",
        json={"sync_offset_ms": 0},
        headers={"Origin": settings.public_base_url},
    )
    assert resp.status_code == 200

    async with get_session() as session:
        refreshed = await session.get(Room, room.id)
        assert refreshed.floor_source == "program_ingest"
        assert refreshed.program_ingest_mode == "whip"
        assert refreshed.program_backup_enabled is False
        assert refreshed.program_backup_connected_at is None
        assert refreshed.program_active_feed == "primary"


@pytest.mark.anyio
async def test_worker_omitted_stream_path_does_not_replace_running_backup_worker(monkeypatch):
    from unittest.mock import AsyncMock

    from portal.transcription.worker import (
        ProviderConfig,
        State,
        TranscriptionWorkerSession,
        active_workers,
        start_transcription_worker,
        stop_transcription_worker,
    )

    async def mock_run_loop(self):
        self.loop_entered = True
        try:
            await self.stop_event.wait()
        finally:
            self.state = State.STOPPED

    monkeypatch.setattr(TranscriptionWorkerSession, "_run_loop", mock_run_loop)

    monkeypatch.setattr(TranscriptionWorkerSession, "_run_loop", mock_run_loop)

    booth_id = "test-event-1-floor"
    await stop_transcription_worker(booth_id)

    # 1. Start worker on backup stream path
    backup_path = "test-event/1/floor_backup"
    started = await start_transcription_worker(
        event_slug="test-event",
        language_code="floor",
        booth_id=booth_id,
        broadcast_callback=AsyncMock(),
        provider="local",
        model_size="tiny",
        config=ProviderConfig(api_key=None),
        room_id=1,
        stream_path=backup_path,
    )
    assert started is True
    session = active_workers.get(booth_id)
    assert session is not None
    assert session.stream_path == backup_path
    first_session_id = session.session_id

    # 2. Call start_transcription_worker with omitted stream_path (None) on switchback
    # MUST stop the running backup worker session and switch back to primary (None)
    started_again = await start_transcription_worker(
        event_slug="test-event",
        language_code="floor",
        booth_id=booth_id,
        broadcast_callback=AsyncMock(),
        provider="local",
        model_size="tiny",
        config=ProviderConfig(api_key=None),
        room_id=1,
        stream_path=None,
    )
    assert started_again is True
    current_session = active_workers.get(booth_id)
    assert current_session is not None
    assert current_session.stream_path is None

    # 3. Call start_transcription_worker with an explicitly different stream_path
    # MUST stop old worker and start new one
    primary_path = "test-event/1/floor"
    started_new = await start_transcription_worker(
        event_slug="test-event",
        language_code="floor",
        booth_id=booth_id,
        broadcast_callback=AsyncMock(),
        provider="local",
        model_size="tiny",
        config=ProviderConfig(api_key=None),
        room_id=1,
        stream_path=primary_path,
    )
    assert started_new is True
    new_session = active_workers.get(booth_id)
    assert new_session is not None
    assert new_session.session_id != first_session_id
    assert new_session.stream_path == primary_path

    await stop_transcription_worker(booth_id)


@pytest.mark.anyio
async def test_api_transcription_start_passes_backup_path_when_backup_active(backup_env, monkeypatch):
    from portal.models import DBBooth
    from portal.routers import api as api_router_mod

    event = backup_env["event"]
    room = backup_env["room"]
    client = backup_env["client"]

    # Configure room with active backup
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_backup_enabled = True
        stored.program_active_feed = "backup"
        booth = DBBooth(
            event_id=event.id,
            room_id=room.id,
            language_code="floor",
            language_name="Floor",
            transcription_enabled=True,
            transcription_provider="local",
            transcription_model="tiny",
        )
        session.add(booth)
        await session.commit()

    passed_kwargs = {}

    async def mock_start_worker(*args, **kwargs):
        passed_kwargs.update(kwargs)
        return True

    monkeypatch.setattr(api_router_mod, "start_transcription_worker", mock_start_worker)

    resp = await client.post(
        f"/api/events/{event.slug}/rooms/{room.id}/booths/floor/transcription/start",
        headers={"Origin": "http://test"},
    )
    assert resp.status_code == 200
    assert passed_kwargs.get("stream_path") == f"{event.slug}/{room.id}/floor_backup"


@pytest.mark.anyio
async def test_backup_only_publisher_released_on_event_deletion(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]
    calls = backup_env["calls"]

    backup_path = f"{event.slug}/{room.id}/floor_backup"
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "backup-sess-del", "type": "srtConn"},
    }

    # Setup room as backup-only (program_session_id is None, program_backup_connected_at is set)
    async with get_session() as session:
        stored_room = await session.get(Room, room.id)
        assert stored_room is not None
        stored_room.program_session_id = None
        stored_room.program_backup_enabled = True
        stored_room.program_backup_connected_at = ingest.utc_now()
        stored_event = await session.get(Event, event.id)
        assert stored_event is not None
        stored_event.deleted_at = ingest.utc_now()
        await session.commit()

    entry = ingest.IngestHealth()
    entry.backup_connected = True
    entry.backup_session_id = "backup-sess-del"
    entry.backup_reservation_started_at = time.monotonic()
    ingest.health[room.id] = entry

    calls.clear()
    await ingest.release_deleted_event_sessions()

    # Verify backup publisher was kicked and DB/memory state cleaned up
    async with get_session() as session:
        refreshed_room = await session.get(Room, room.id)
        assert refreshed_room is not None
        assert refreshed_room.program_backup_connected_at is None

    assert entry.backup_session_id is None
    assert entry.backup_connected is False
    assert entry.backup_reservation_started_at is None

    # Verify kick API was called for the backup path
    assert any(backup_path in url for method, url in calls)


@pytest.mark.anyio
async def test_standby_backup_non_conforming_codecs_rejected_and_not_promoted(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]
    calls = backup_env["calls"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_ingest_mode = "srt"
        stored.program_backup_enabled = True
        stored.program_backup_connected_at = ingest.utc_now()
        stored.program_active_feed = "primary"
        await session.commit()

    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"

    # Primary is stalled (counter unchanged, old progress)
    media[primary_path] = {
        "name": primary_path,
        "ready": True,
        "source": {"id": "prim-1", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 50000,
    }
    # Standby backup has non-conforming codecs: VP8 video and Opus audio (or H264 without AAC)
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "bad-backup-1", "type": "srtConn"},
        "tracks": ["VP8", "opus"],
        "bytesReceived": 10000,
    }

    now = time.monotonic()
    entry = ingest.IngestHealth()
    entry.primary_last_bytes = 50000
    entry.primary_last_progress = now - 10.0  # primary stalled > TAKEOVER_STALL_SECS
    entry.backup_session_id = "bad-backup-1"
    entry.backup_connected = True
    ingest.health[room.id] = entry

    calls.clear()
    await ingest.reconcile_once()

    current = ingest.health[room.id]
    # Standby backup must be marked non-conforming and disconnected
    assert current.backup_conforming is False
    assert current.backup_connected is False

    # Room must NOT promote the non-conforming backup to active feed
    async with get_session() as session:
        refreshed = await session.get(Room, room.id)
        assert refreshed.program_active_feed == "primary"
        assert refreshed.program_backup_connected_at is None

    # Standby backup must have been kicked by session ID
    assert any("kick/bad-backup-1" in url for method, url in calls)


@pytest.mark.anyio
async def test_standby_backup_excessive_bitrate_rejected_and_not_promoted(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_ingest_mode = "srt"
        stored.program_backup_enabled = True
        stored.program_backup_connected_at = ingest.utc_now()
        stored.program_active_feed = "primary"
        await session.commit()

    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"

    media[primary_path] = {
        "name": primary_path,
        "ready": True,
        "source": {"id": "prim-1", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 50000,
    }
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "high-bitrate-backup", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 20_000_000,
    }

    now = time.monotonic()
    entry = ingest.IngestHealth()
    entry.primary_last_bytes = 50000
    entry.primary_last_progress = now - 10.0  # primary stalled
    entry.backup_session_id = "high-bitrate-backup"
    entry.backup_connected = True
    # Simulate standby having 4 high bitrate cycles already
    entry.backup_high_bitrate_cycles = 4
    entry.backup_last_cycle_bytes = 10_000_000
    entry.backup_last_cycle_time = now - 1.0  # 10MB in 1s = 80 Mbps > 12 Mbps
    ingest.health[room.id] = entry

    await ingest.reconcile_once()

    current = ingest.health[room.id]
    # At 5th cycle, standby backup must be kicked and marked non-conforming
    assert current.backup_conforming is False
    assert current.backup_connected is False

    # Must NOT have taken over
    async with get_session() as session:
        refreshed = await session.get(Room, room.id)
        assert refreshed.program_active_feed == "primary"
        assert refreshed.program_backup_connected_at is None


@pytest.mark.anyio
async def test_backup_reserved_session_kicked_when_path_has_no_source_on_event_deletion(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]
    calls = backup_env["calls"]

    backup_path = f"{event.slug}/{room.id}/floor_backup"
    # MediaMTX path does not expose a source (e.g. still negotiating or 404)
    media[backup_path] = {
        "name": backup_path,
        "ready": False,
        "source": None,
    }

    async with get_session() as session:
        stored_room = await session.get(Room, room.id)
        assert stored_room is not None
        stored_room.program_session_id = None
        stored_room.program_backup_enabled = True
        stored_room.program_backup_connected_at = ingest.utc_now()
        stored_event = await session.get(Event, event.id)
        assert stored_event is not None
        stored_event.deleted_at = ingest.utc_now()
        await session.commit()

    entry = ingest.IngestHealth()
    entry.backup_connected = False
    entry.backup_session_id = "backup-in-memory-sess-1"
    entry.backup_reservation_started_at = time.monotonic()
    ingest.health[room.id] = entry

    calls.clear()
    await ingest.release_deleted_event_sessions()

    # The in-memory reserved session ID must be kicked even without a live MediaMTX source
    assert any("kick/backup-in-memory-sess-1" in url for method, url in calls)
    assert entry.backup_session_id is None
    assert entry.backup_reservation_started_at is None
    async with get_session() as session:
        refreshed_room = await session.get(Room, room.id)
        assert refreshed_room is not None
        assert refreshed_room.program_backup_connected_at is None


@pytest.mark.anyio
async def test_backup_bitrate_counters_reset_on_new_publisher_reservation(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_ingest_mode = "srt"
        stored.program_backup_enabled = True
        stored.program_active_feed = "primary"
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
        await session.commit()

    # Suppose a previous backup publisher accumulated 4 high-bitrate cycles and 50MB
    entry = ingest.IngestHealth()
    entry.backup_session_id = "old-backup-session"
    entry.backup_high_bitrate_cycles = 4
    entry.backup_last_cycle_bytes = 50_000_000
    entry.backup_last_cycle_time = time.monotonic() - 1.0
    ingest.health[room.id] = entry

    # New backup publisher reserves via authorize_publish
    new_sess_id = str(uuid4())
    auth_data = ingest.MediaAuth(
        action="publish",
        protocol="srt",
        ip="127.0.0.1",
        path=f"{event.slug}/{room.id}/floor_backup",
        id=new_sess_id,
        password=decrypt_val(stored.program_backup_key_enc),
    )
    await ingest.authorize_publish(auth_data)

    # IngestHealth backup bitrate counters must be reset upon reservation
    assert entry.backup_session_id == new_sess_id
    assert entry.backup_high_bitrate_cycles == 0
    assert entry.backup_last_cycle_bytes == -1
    assert entry.backup_last_cycle_time == 0.0
    assert entry.backup_conforming is True

    # Now simulate a normal bitrate cycle for the new publisher
    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"
    media[primary_path] = {
        "name": primary_path,
        "ready": True,
        "source": {"id": "prim-1", "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 50000,
    }
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": new_sess_id, "type": "srtConn"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 1000,
    }

    await ingest.reconcile_once()

    # Because counters were reset, the previous 4 high-bitrate cycles must NOT carry over
    assert entry.backup_high_bitrate_cycles == 0
    assert entry.backup_last_cycle_bytes == 1000
    assert entry.backup_conforming is True
    assert entry.backup_connected is True


@pytest.mark.anyio
async def test_in_memory_backup_session_kicked_when_db_timestamp_is_null_on_event_deletion(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]
    calls = backup_env["calls"]

    backup_path = f"{event.slug}/{room.id}/floor_backup"
    media[backup_path] = {
        "name": backup_path,
        "ready": False,
        "source": None,
    }

    async with get_session() as session:
        stored_room = await session.get(Room, room.id)
        assert stored_room is not None
        stored_room.program_session_id = None
        stored_room.program_backup_enabled = True
        stored_room.program_backup_connected_at = None
        stored_event = await session.get(Event, event.id)
        assert stored_event is not None
        stored_event.deleted_at = ingest.utc_now()
        await session.commit()

    entry = ingest.IngestHealth()
    entry.backup_connected = False
    entry.backup_session_id = "mem-only-sess-42"
    entry.backup_reservation_started_at = time.monotonic()
    ingest.health[room.id] = entry

    calls.clear()
    await ingest.release_deleted_event_sessions()

    assert any("kick/mem-only-sess-42" in url for method, url in calls)
    assert entry.backup_session_id is None
    assert entry.backup_reservation_started_at is None


@pytest.mark.anyio
async def test_backup_takeover_blocked_when_backup_frozen(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_connected_at = ingest.utc_now()
        stored.program_session_id = "prim-sess"
        stored.program_backup_enabled = True
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
        stored.program_backup_connected_at = ingest.utc_now()
        stored.program_active_feed = "primary"
        await session.commit()

    primary_path = f"{event.slug}/{room.id}/floor"
    backup_path = f"{event.slug}/{room.id}/floor_backup"

    # Primary is disconnected / missing from media
    media.pop(primary_path, None)

    # Backup is connected, conforming codec, but frozen (0 bytes received or flat progress)
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": "backup-sess-frozen", "type": "srtSource"},
        "tracks": ["H264", "AAC"],
        "bytesReceived": 0,
    }

    entry = ingest.IngestHealth()
    entry.primary_session_id = "prim-sess"
    entry.primary_last_progress = 0.0
    entry.backup_session_id = "backup-sess-frozen"
    entry.backup_last_bytes = 0
    entry.backup_last_progress = time.monotonic() - 10.0  # stalled for 10s
    ingest.health[room.id] = entry

    # 1. Takeover must NOT happen when backup is not progressing
    await ingest.reconcile_once()

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "primary"

    # 2. When backup starts progressing (bytesReceived grows), takeover happens
    media[backup_path]["bytesReceived"] = 50_000
    await ingest.reconcile_once()

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored.program_active_feed == "backup"


@pytest.mark.anyio
async def test_backup_kick_failure_recorded_against_backup_client_ip(backup_env):
    event = backup_env["event"]
    room = backup_env["room"]
    media = backup_env["media"]
    client = backup_env["client"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_backup_enabled = True
        pass_b = await ingest.generate_backup_srt_passphrase(stored)
        backup_env["configured_paths"].add(f"{event.slug}/{room.id}/floor_backup", pass_b)
        await session.commit()

    # 1. Authorize primary from IP 198.51.100.10
    primary_payload = {
        "action": "publish",
        "path": f"{event.slug}/{room.id}/floor",
        "protocol": "srt",
        "id": str(uuid4()),
        "ip": "198.51.100.10",
        "password": decrypt_val(stored.program_srt_passphrase_enc),
    }
    resp1 = await client.post("/internal/media-auth", json=primary_payload)
    assert resp1.status_code == 204
    assert ingest.health[room.id].client_ip == "198.51.100.10"

    # 2. Authorize backup from IP 198.51.100.20
    backup_payload = backup_publish(event, stored, ip="198.51.100.20")
    resp2 = await client.post("/internal/media-auth", json=backup_payload)
    assert resp2.status_code == 204
    assert ingest.health[room.id].client_ip == "198.51.100.10"
    assert ingest.health[room.id].backup_client_ip == "198.51.100.20"

    # 3. Simulate backup sending invalid codec track -> kicked on reconcile
    backup_path = f"{event.slug}/{room.id}/floor_backup"
    media[backup_path] = {
        "name": backup_path,
        "ready": True,
        "source": {"id": backup_payload["id"], "type": "srtConn"},
        "tracks": ["VP9"],  # Non-conforming codec
        "bytesReceived": 1024,
    }
    await ingest.reconcile_once()

    # Verify failure recorded against backup_client_ip (198.51.100.20), NOT primary (198.51.100.10)
    assert ingest.failures["198.51.100.20"][1] >= 1
    assert "198.51.100.10" not in ingest.failures
