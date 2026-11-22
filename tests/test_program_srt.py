"""SRT contribution ingest tests (#750)."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from fastapi_app import app
from portal import program_ingest as ingest
from portal import program_relay as relay
from portal.auth import create_user_token
from portal.config import settings
from portal.crypto import decrypt_val, encrypt_val
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import Event, Room

# Requests carry the token a real page would send.
pytestmark = pytest.mark.usefixtures("auto_csrf")


@pytest.fixture
async def srt_env(monkeypatch):
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
    calls: list[tuple[str, str, dict]] = []

    def control(request: httpx.Request):
        data = json.loads(request.read()) if request.content else {}
        calls.append((request.method, str(request.url), data))
        if "/v3/config/paths/get/" in request.url.path:
            name = request.url.path.split("/v3/config/paths/get/")[1]
            return httpx.Response(200, json={"name": name, "srtPublishPassphrase": "pass"})
        if "/v3/config/paths/list" in request.url.path:
            return httpx.Response(200, json={"items": [{"name": k, "srtPublishPassphrase": "pass"} for k in media]})
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
                email="srt-admin@example.com",
                display_name="SRT Admin",
                email_verified=True,
                is_admin=True,
            )
            event = Event(slug="srt-test", display_name="SRT Test")
            session.add(event)
            await session.flush()
            rooms = [
                Room(
                    event_id=event.id,
                    display_name=f"Room {n}",
                    floor_transcription_enabled=True,
                    floor_language_code="en",
                    floor_source="program_ingest",
                    program_ingest_mode=relay.SRT_MODE,
                )
                for n in range(2)
            ]
            session.add_all(rooms)
            await session.flush()
            for r in rooms:
                await ingest.generate_srt_passphrase(r)
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
                "rooms": rooms,
                "media": media,
                "calls": calls,
                "start": start,
            }

    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    ingest.audio_progress.clear()
    relay.relay_health.clear()
    await dispose()


def srt_publish(event: Event, room: Room, **overrides) -> dict:
    passphrase = decrypt_val(room.program_srt_passphrase_enc) if room.program_srt_passphrase_enc else "test-pass"
    return {
        "action": "publish",
        "path": f"{event.slug}/{room.id}/floor",
        "protocol": "srt",
        "id": str(uuid4()),
        "ip": "198.51.100.1",
        "password": passphrase,
        **overrides,
    }


@pytest.mark.anyio
async def test_srt_auth_hook_accepts_valid_room(srt_env):
    client = srt_env["client"]
    event = srt_env["event"]
    room = srt_env["rooms"][0]

    payload = srt_publish(event, room)
    response = await client.post("/internal/media-auth", json=payload)
    assert response.status_code == 204


@pytest.mark.anyio
async def test_srt_auth_hook_accepts_passphrase_in_query(srt_env):
    client = srt_env["client"]
    event = srt_env["event"]
    room = srt_env["rooms"][0]

    passphrase = decrypt_val(room.program_srt_passphrase_enc)
    payload = srt_publish(event, room, password="", query=f"passphrase={passphrase}&pkt_size=1316")
    response = await client.post("/internal/media-auth", json=payload)
    assert response.status_code == 204


@pytest.mark.anyio
async def test_srt_auth_hook_rejects_missing_or_empty_passphrase(srt_env):
    client = srt_env["client"]
    event = srt_env["event"]
    room = srt_env["rooms"][0]

    # Empty password rejected
    payload = srt_publish(event, room, password="")
    response = await client.post("/internal/media-auth", json=payload)
    assert response.status_code == 401

    # Wrong password rejected
    payload2 = srt_publish(event, room, password="wrong-passphrase")
    response2 = await client.post("/internal/media-auth", json=payload2)
    assert response2.status_code == 401


@pytest.mark.anyio
async def test_srt_auth_hook_rejects_when_srt_disabled(srt_env, monkeypatch):
    client = srt_env["client"]
    event = srt_env["event"]
    room = srt_env["rooms"][0]

    monkeypatch.setattr(settings, "srt_ingest_enabled", False)
    payload = srt_publish(event, room)
    response = await client.post("/internal/media-auth", json=payload)
    assert response.status_code in (401, 403)


@pytest.mark.anyio
async def test_srt_passphrase_stored_encrypted_never_logged(srt_env, caplog):
    import logging

    room = srt_env["rooms"][0]
    caplog.set_level(logging.DEBUG)

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        passphrase = await ingest.generate_srt_passphrase(stored)
        await session.commit()

        encrypted = stored.program_srt_passphrase_enc
        assert encrypted is not None

        plaintext = decrypt_val(encrypted)
        assert plaintext is not None
        assert plaintext == passphrase
        assert len(plaintext) >= 16
        # Ciphertext must differ from plaintext
        assert encrypted != plaintext
        # Plaintext must not appear in plaintext in ciphertext, room repr, or captured logs
        assert plaintext not in encrypted
        assert plaintext not in repr(stored)
        assert plaintext not in caplog.text


@pytest.mark.anyio
async def test_transport_isolation(srt_env, monkeypatch):
    client = srt_env["client"]
    event = srt_env["event"]
    srt_room = srt_env["rooms"][0]
    rtmps_room = srt_env["rooms"][1]

    # Configure rtmps_room for RTMPS mode
    async with get_session() as session:
        stored = await session.get(Room, rtmps_room.id)
        assert stored is not None
        stored.program_ingest_mode = relay.RTMPS_MODE
        stored.program_relay_key_enc = encrypt_val("rtmps-stream-key")
        await session.commit()

    monkeypatch.setattr(settings, "program_rtmps_enabled", True)

    # 1. SRT room rejects RTMP protocol
    rtmp_payload = {
        "action": "publish",
        "path": f"{event.slug}/{srt_room.id}/floor",
        "protocol": "rtmp",
        "query": "key=test-key",
        "id": str(uuid4()),
        "ip": "198.51.100.1",
    }
    resp1 = await client.post("/internal/media-auth", json=rtmp_payload)
    assert resp1.status_code in (401, 403)

    # 2. RTMPS room rejects SRT protocol
    srt_payload = srt_publish(event, rtmps_room)
    resp2 = await client.post("/internal/media-auth", json=srt_payload)
    assert resp2.status_code in (401, 403)


@pytest.mark.anyio
async def test_generate_and_apply_srt_passphrase(srt_env):
    event = srt_env["event"]
    room = srt_env["rooms"][0]
    calls = srt_env["calls"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        passphrase = await ingest.generate_srt_passphrase(stored)
        assert len(passphrase) >= 16
        await session.commit()

    path = f"{event.slug}/{room.id}/floor"
    await ingest.apply_srt_passphrase_to_mediamtx(path, passphrase)

    # Verify MediaMTX control client was invoked with srtPublishPassphrase and NOT publishPass
    matching_calls = [
        (method, url, data)
        for method, url, data in calls
        if f"/v3/config/paths/add/{path}" in url or f"/v3/config/paths/patch/{path}" in url
    ]
    assert len(matching_calls) >= 1
    assert matching_calls[0][2]["srtPublishPassphrase"] == passphrase
    assert "publishPass" not in matching_calls[0][2]
    assert matching_calls[0][2]["overridePublisher"] is False


@pytest.mark.anyio
async def test_srt_codec_validation_accepts_aac_audio_and_tolerates_data_tracks(srt_env):
    event = srt_env["event"]
    room = srt_env["rooms"][0]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_session_id = "test-session-id"
        await session.commit()

    # Helper to test codec validation
    async def evaluate_tracks(tracks: list[str]) -> str:
        health_entry = ingest.IngestHealth()
        ingest.health[room.id] = health_entry
        path = ingest.MediaPath(
            name=f"{event.slug}/{room.id}/floor",
            ready=True,
            source=ingest.MediaSource(id="test-session-id", type="srtConn"),
            tracks=tracks,
            bytesReceived=1024,
        )
        await ingest.reconcile_room(stored, event, path)
        return health_entry.state

    # 1. Video only -> rejected (no audio)
    assert await evaluate_tracks(["H264"]) == "rejected_codec"

    # 2. Audio only (AAC) -> accepted
    assert await evaluate_tracks(["AAC"]) != "rejected_codec"

    # 3. Empty tracks -> rejected
    assert await evaluate_tracks([]) == "rejected_codec"

    # 4. Incompatible extra track (HEVC video) -> rejected
    assert await evaluate_tracks(["H264", "AAC", "HEVC"]) == "rejected_codec"

    # 5. Non-AAC MPEG-4 -> rejected
    assert await evaluate_tracks(["H264", "MPEG-4 Visual"]) == "rejected_codec"

    # 6. Valid H.264 + AAC -> accepted
    state_valid1 = await evaluate_tracks(["H264", "AAC"])
    assert state_valid1 != "rejected_codec"

    # 7. Valid H.264 + MPEG-4 Audio -> accepted
    state_valid2 = await evaluate_tracks(["H.264", "MPEG-4 Audio"])
    assert state_valid2 != "rejected_codec"

    # 8. Valid H.264 + AAC + KLV data track -> accepted
    state_valid3 = await evaluate_tracks(["H264", "AAC", "KLV"])
    assert state_valid3 != "rejected_codec"
    state_valid2 = await evaluate_tracks(["H.264", "MPEG-4 Audio"])
    assert state_valid2 != "rejected_codec"


@pytest.mark.anyio
async def test_srt_aac_audio_track_recognized_for_transcription(srt_env):
    import time

    event = srt_env["event"]
    room = srt_env["rooms"][0]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.floor_transcription_provider = "local"
        stored.floor_transcription_model = "tiny"
        await session.commit()

    path = ingest.MediaPath(
        name=f"{event.slug}/{room.id}/floor",
        ready=True,
        source=ingest.MediaSource(id="test-session-id", type="srtConn"),
        tracks=["H264", "AAC"],
        bytesReceived=1024,
    )
    health_entry = ingest.IngestHealth()
    health_entry.codecs = ["H264", "AAC"]
    booth_id = f"{event.slug}-{room.id}-floor"
    now = time.monotonic()

    # Reconcile processing with AAC track: must start transcription, not degrade
    await ingest.reconcile_processing(stored, event, path, health_entry, booth_id, now)
    assert health_entry.processing_state != "degraded"
    assert health_entry.state in ("receiving", "processing")
    assert health_entry.ingest_state == "connected"

    # Verify status API returns ingest_continuity=True for healthy connected feed
    ingest.health[room.id] = health_entry
    client = srt_env["client"]
    resp = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/program-ingest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["metrics"]["ingest_continuity"] is True
    assert data["metrics"]["audio_present"] is True
    assert health_entry.processing_state in ("starting", "processing")


@pytest.mark.anyio
async def test_destination_relay_failed_state_triggers_operator_alert(srt_env, monkeypatch):
    import portal.program_relay as program_relay

    event = srt_env["event"]
    room = srt_env["rooms"][0]
    client = srt_env["client"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_ingest_mode = program_relay.RELAY_MODE
        stored.program_relay_server = "rtmps://a.rtmps.youtube.com/live2"
        await session.commit()

    monkeypatch.setattr(settings, "program_relay_enabled", True)
    program_relay.relay_health[room.id] = program_relay.RelayHealth(
        "failed", "YouTube rejected the stream key; check credentials."
    )

    resp = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/program-ingest")
    assert resp.status_code == 200
    data = resp.json()
    alerts = data.get("alerts", [])
    youtube_alerts = [a for a in alerts if a.get("target") == "youtube"]
    assert len(youtube_alerts) == 1
    assert youtube_alerts[0]["level"] == "error"
    assert "YouTube rejected the stream key" in youtube_alerts[0]["message"]
    assert "Verify YouTube stream key" in youtube_alerts[0]["action"]


@pytest.mark.anyio
async def test_srt_urls_bracket_ipv6_addresses(srt_env, monkeypatch):
    event = srt_env["event"]
    room = srt_env["rooms"][0]
    client = srt_env["client"]

    # Test 1: IPv6 in public_base_url
    monkeypatch.setattr(settings, "program_srt_public_host", "")
    monkeypatch.setattr(settings, "public_base_url", "https://[2001:db8::1]:8000")
    resp = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/program-ingest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["srt_server"].startswith(f"srt://[2001:db8::1]:{settings.program_srt_public_port}?streamid=")
    assert data["srt_backup_server"].startswith(f"srt://[2001:db8::1]:{settings.program_srt_public_port}?streamid=")

    # Test 2: Unbracketed IPv6 in program_srt_public_host
    monkeypatch.setattr(settings, "program_srt_public_host", "2001:db8::2")
    resp = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/program-ingest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["srt_server"].startswith(f"srt://[2001:db8::2]:{settings.program_srt_public_port}?streamid=")
    assert data["srt_backup_server"].startswith(f"srt://[2001:db8::2]:{settings.program_srt_public_port}?streamid=")

    # Test 3: Standard hostname remains unbracketed
    monkeypatch.setattr(settings, "program_srt_public_host", "ingest.example.com")
    resp = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/program-ingest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["srt_server"].startswith(f"srt://ingest.example.com:{settings.program_srt_public_port}?streamid=")


@pytest.mark.anyio
async def test_egress_continuity_only_reported_when_relay_state_is_live(srt_env, monkeypatch):
    import portal.program_relay as program_relay

    event = srt_env["event"]
    room = srt_env["rooms"][0]
    client = srt_env["client"]

    async with get_session() as session:
        stored = await session.get(Room, room.id)
        assert stored is not None
        stored.program_ingest_mode = program_relay.RELAY_MODE
        await session.commit()

    monkeypatch.setattr(settings, "program_relay_enabled", True)

    # When relay state is "live", egress_continuity must report True
    program_relay.relay_health[room.id] = program_relay.RelayHealth("live", "Relaying program feed.")
    resp = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/program-ingest")
    assert resp.status_code == 200
    assert resp.json()["metrics"]["egress_continuity"] is True

    # When relay state is "retrying", "failed", or "stopped", egress_continuity must report False
    for non_live_state in ("retrying", "failed", "stalled", "stopped", "idle"):
        program_relay.relay_health[room.id] = program_relay.RelayHealth(non_live_state, "Not live.")
        resp = await client.get(f"/admin/events/{event.id}/rooms/{room.id}/program-ingest")
        assert resp.status_code == 200
        assert resp.json()["metrics"]["egress_continuity"] is False
