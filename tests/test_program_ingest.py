from __future__ import annotations

import asyncio
import json
import struct
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from fastapi_app import app
from portal import program_ingest as ingest
from portal.auth import create_admin_token
from portal.config import settings
from portal.database import configure, dispose, get_session, init_db
from portal.models import Event, Room, RoomTranslationLanguage, TranscriptSegment
from portal.transcription.aggregator import CaptionAggregator
from portal.websockets.manager import tts_manager


@pytest.fixture
async def environment(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", "")
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "program_ingest_disconnect_grace_secs", 0)
    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    media = {}
    calls = []

    def control(request):
        calls.append((request.method, request.url.path))
        if "/paths/list" in request.url.path:
            return httpx.Response(200, json={"items": list(media.values())})
        if "/paths/get/" in request.url.path:
            name = request.url.path.split("/paths/get/")[1]
            return httpx.Response(200, json=media[name]) if name in media else httpx.Response(404)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(control)) as media_client:
        monkeypatch.setattr(ingest, "get_http_client", lambda: media_client)
        start, stop = AsyncMock(), AsyncMock()

        async def started(**kwargs):
            ingest.audio_progress[kwargs["booth_id"]] = asyncio.get_running_loop().time()

        start.side_effect = started
        monkeypatch.setattr(ingest, "start_transcription_worker", start)
        monkeypatch.setattr(ingest, "stop_transcription_worker", stop)
        async with get_session() as session:
            event = Event(slug="program-test", display_name="Program Test")
            session.add(event)
            await session.flush()
            rooms = [
                Room(
                    event_id=event.id,
                    display_name=f"Room {n}",
                    floor_transcription_enabled=True,
                    floor_language_code="en",
                )
                for n in range(2)
            ]
            session.add_all(rooms)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://test",
            cookies={"admin_token": create_admin_token()},
        ) as client:
            yield client, event, rooms, media, calls, start, stop
    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    ingest.audio_progress.clear()
    await dispose()


def admin_url(event, room):
    return f"/admin/events/{event.id}/rooms/{room.id}/program-ingest"


async def enable(client, event, room):
    response = await client.post(admin_url(event, room), json={"action": "enable"})
    assert response.status_code == 200, response.text
    return response.json()["secret"]


def credentials(room, token, **overrides):
    return {
        "action": "publish",
        "path": f"program-test/{room.id}/floor",
        "protocol": "webrtc",
        "token": token,
        "id": str(uuid4()),
        "ip": "198.51.100.7",
        **overrides,
    }


@pytest.mark.anyio
async def test_media_auth_requires_shared_hook_secret(environment, monkeypatch):
    client, event, rooms, *_ = environment
    room = rooms[0]
    token = await enable(client, event, room)
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", "control-plane-secret")
    payload = credentials(room, token)

    assert (await client.post("/internal/media-auth", json=payload)).status_code == 401
    assert (await client.post("/internal/media-auth?key=wrong", json=payload)).status_code == 401
    assert (await client.post("/internal/media-auth?key=control-plane-secret", json=payload)).status_code == 204


@pytest.mark.anyio
async def test_room_ownership_lock_does_not_block_other_room_authorization(environment):
    client, event, rooms, *_ = environment
    tokens = [await enable(client, event, room) for room in rooms]
    held_lock = ingest.room_lock(rooms[0].id)
    await held_lock.acquire()
    try:
        response = await asyncio.wait_for(
            client.post("/internal/media-auth", json=credentials(rooms[1], tokens[1])),
            timeout=1,
        )
    finally:
        held_lock.release()
    assert response.status_code == 204


@pytest.mark.anyio
async def test_enable_tolerates_absent_floor_bot_but_not_http_rejection(environment, monkeypatch):
    client, event, rooms, media, *_ = environment
    room = rooms[0]

    def absent_bot(request):
        if request.url.host == "floor-bot":
            raise httpx.ConnectError("not running", request=request)
        if "/paths/get/" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, json={"items": list(media.values())})

    async with httpx.AsyncClient(transport=httpx.MockTransport(absent_bot)) as media_client:
        monkeypatch.setattr(ingest, "get_http_client", lambda: media_client)
        response = await client.post(admin_url(event, room), json={"action": "enable"})
    assert response.status_code == 200
    assert response.json()["secret"]

    async with get_session() as session:
        saved = await session.get(Room, room.id)
        saved.floor_source = "jitsi_bot"

    def rejecting_bot(request):
        if request.url.host == "floor-bot":
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"items": []}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(rejecting_bot)) as media_client:
        monkeypatch.setattr(ingest, "get_http_client", lambda: media_client)
        response = await client.post(admin_url(event, room), json={"action": "enable"})
    assert response.status_code == 502


@pytest.mark.anyio
async def test_revoke_is_durable_when_kick_404_leaves_same_source_live(environment, monkeypatch):
    client, event, rooms, media, *_ = environment
    room = rooms[0]
    token = await enable(client, event, room)
    auth = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=auth)).status_code == 204
    media[auth["path"]] = {
        "name": auth["path"],
        "ready": True,
        "source": {"type": "webRTCSession", "id": auth["id"]},
        "tracks": ["Opus"],
        "bytesReceived": 10,
    }

    def missing_kick_endpoint(request):
        if "/kick/" in request.url.path:
            return httpx.Response(404, request=request)
        if "/paths/get/" in request.url.path:
            return httpx.Response(200, json=media[auth["path"]], request=request)
        return httpx.Response(200, json={}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(missing_kick_endpoint)) as media_client:
        monkeypatch.setattr(ingest, "get_http_client", lambda: media_client)
        response = await client.post(admin_url(event, room), json={"action": "revoke"})
    assert response.status_code == 202
    assert response.json() == {"secret": None, "cleanup_pending": True}
    async with get_session() as session:
        saved = await session.get(Room, room.id)
        assert not ingest.key_valid(saved, token)
    assert (await client.post("/internal/media-auth", json=auth)).status_code == 401


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["connection", "http", "json", "schema"])
async def test_media_control_failure_returns_503_without_replacing_reservation(environment, monkeypatch, failure):
    client, event, rooms, *_ = environment
    room = rooms[0]
    token = await enable(client, event, room)
    original = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=original)).status_code == 204
    ingest.health.clear()  # Force the persisted reservation's control-plane check.

    def unavailable(request):
        if failure == "connection":
            raise httpx.ConnectError("upstream details", request=request)
        if failure == "http":
            return httpx.Response(502, text="upstream details")
        if failure == "json":
            return httpx.Response(200, text="not JSON")
        return httpx.Response(200, json={"name": None})

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as media_client:
        monkeypatch.setattr(ingest, "get_http_client", lambda: media_client)
        response = await client.post("/internal/media-auth", json=credentials(room, token))
    assert response.status_code == 503
    assert response.json() == {"detail": "Media control unavailable"}
    async with get_session() as session:
        saved = await session.get(Room, room.id)
        assert saved.program_session_id == original["id"]


@pytest.mark.anyio
async def test_only_sync_actions_change_saved_offset(environment):
    client, event, rooms, *_ = environment
    room = rooms[0]
    url = admin_url(event, room)
    assert (await client.post(url, json={"action": "sync", "sync_offset_ms": 6500})).status_code == 200
    for action in ["enable", "rotate", "revoke", "disable"]:
        # Omitted/default and explicit offsets on credential actions are ignored.
        for offset in [{}, {"sync_offset_ms": 100}]:
            if action in {"rotate", "revoke"}:
                await enable(client, event, room)
            assert (await client.post(url, json={"action": action, **offset})).status_code == 200
            assert (await client.get(url)).json()["sync_offset_ms"] == 6500
    assert (await client.post(url, json={"action": "sync", "sync_offset_ms": 9000})).status_code == 200
    assert (await client.get(url)).json()["sync_offset_ms"] == 9000


@pytest.mark.anyio
async def test_credentials_scope_rotation_expiry_and_no_leaks(environment, caplog):
    client, event, rooms, _, _, _, _ = environment
    room, other = rooms
    token = await enable(client, event, room)
    url = admin_url(event, room)
    status = await client.get(url)
    assert token not in status.text and "program_key_hash" not in status.text
    assert status.headers["cache-control"] == "no-store"
    async with get_session() as session:
        saved = await session.get(Room, room.id)
        assert saved.program_key_hash == ingest.digest_program_secret(token)
    valid = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=valid)).status_code == 204
    for payload in [credentials(other, token), credentials(room, "wrong"), credentials(room, token, query="token=x")]:
        assert (await client.post("/internal/media-auth", json=payload)).status_code == 401
    for malformed_path in [f"program-test/{room.id}/FLOOR", f"program-test/{room.id}/floor/"]:
        assert (
            await client.post("/internal/media-auth", json=credentials(room, token, path=malformed_path))
        ).status_code == 403
    response = await client.post(url, json={"action": "rotate"})
    replacement = response.json()["secret"]
    assert replacement != token
    assert (await client.post("/internal/media-auth", json=valid)).status_code == 401
    assert (await client.post("/internal/media-auth", json=credentials(room, replacement))).status_code == 204
    await client.post(url, json={"action": "revoke"})
    assert (await client.post("/internal/media-auth", json=credentials(room, replacement))).status_code == 401
    token = (await client.post(url, json={"action": "rotate"})).json()["secret"]
    async with get_session() as session:
        saved = await session.get(Room, room.id)
        saved.program_key_expires_at = ingest.utc_now() - timedelta(seconds=1)
    assert (await client.post("/internal/media-auth", json=credentials(room, token))).status_code == 401
    assert token not in caplog.text and replacement not in caplog.text


@pytest.mark.anyio
async def test_auth_throttling_and_anonymous_floor_denial(environment):
    client, event, rooms, _, _, _, _ = environment
    await enable(client, event, rooms[0])
    for _ in range(10):
        assert (await client.post("/internal/media-auth", json=credentials(rooms[0], ""))).status_code == 401
    assert (await client.post("/internal/media-auth", json=credentials(rooms[0], ""))).status_code == 429
    assert (await client.post("/internal/media-auth", json={"token": "secret"})).status_code == 401
    assert (
        await client.post("/internal/media-auth", json=credentials(rooms[0], "", path="program-test/1/fr"))
    ).status_code == 204


@pytest.mark.anyio
async def test_owner_guard_csrf_and_cross_event(environment):
    client, event, rooms, *_ = environment
    url = admin_url(event, rooms[0])
    client.cookies.clear()
    assert (await client.post(url, json={"action": "enable"})).status_code == 403
    client.cookies.set("admin_token", create_admin_token())
    assert (
        await client.post(url, json={"action": "enable"}, headers={"Origin": "https://evil.example"})
    ).status_code == 403
    assert (
        await client.post(url.replace(f"events/{event.id}", "events/999"), json={"action": "enable"})
    ).status_code == 404


@pytest.mark.anyio
async def test_source_conflict_capacity_and_recovery(environment, monkeypatch):
    client, event, rooms, media, calls, start, stop = environment
    room = rooms[0]
    token = await enable(client, event, room)
    auth = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=auth)).status_code == 204
    assert (await client.post("/internal/media-auth", json=credentials(room, token))).status_code == 409
    assert (await client.post(f"/api/rooms/{room.id}/floor-transcription/start")).status_code == 409
    assert (
        await client.post("/internal/media-auth", json=credentials(room, "", protocol="rtsp", ip="172.18.0.2"))
    ).status_code == 401
    token2 = await enable(client, event, rooms[1])
    monkeypatch.setattr(settings, "program_ingest_max_rooms", 1)
    assert (await client.post("/internal/media-auth", json=credentials(rooms[1], token2))).status_code == 503
    media[auth["path"]] = {
        "name": auth["path"],
        "ready": True,
        "source": {"type": "webRTCSession", "id": auth["id"]},
        "tracks": ["H264", "Opus"],
        "bytesReceived": 1024,
    }
    calls.clear()
    await ingest.reconcile_once()
    assert calls == [("GET", "/v3/paths/list")]
    assert start.call_args.kwargs["room_id"] == room.id
    assert not any(path.endswith("/start") for _, path in calls)
    assert (await client.get(admin_url(event, room))).json()["state"] == "processing"
    # Reconstruct volatile state as on portal restart. Persistent ownership survives.
    ingest.health.clear()
    await ingest.reconcile_once()
    assert (await client.get(admin_url(event, room))).json()["state"] == "processing"
    media.clear()
    await ingest.reconcile_once()
    stop.assert_any_await(f"program-test-{room.id}-floor")
    assert (await client.get(admin_url(event, room))).json()["state"] == "disconnected"
    assert (await client.post("/internal/media-auth", json=credentials(room, token))).status_code == 204


@pytest.mark.anyio
async def test_reconnect_reuses_room_worker_without_duplicate_start(environment):
    client, event, rooms, media, _, start, stop = environment
    room = rooms[0]
    token = await enable(client, event, room)
    stop_before = stop.await_count
    auth = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=auth)).status_code == 204
    path = auth["path"]
    media[path] = {
        "name": path,
        "ready": True,
        "source": {"type": "webRTCSession", "id": auth["id"]},
        "tracks": ["Opus"],
        "bytesReceived": 10,
    }
    await ingest.reconcile_once()
    first_count = start.await_count
    media.clear()
    await ingest.reconcile_once()
    assert stop.await_count == stop_before + 1
    reconnect = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=reconnect)).status_code == 204
    media[path] = {
        "name": path,
        "ready": True,
        "source": {"type": "webRTCSession", "id": reconnect["id"]},
        "tracks": ["Opus"],
        "bytesReceived": 20,
    }
    await ingest.reconcile_once()
    assert start.await_count == first_count + 1


@pytest.mark.anyio
async def test_reconnect_gets_fresh_negotiation_grace_after_previous_grace_elapsed(environment, monkeypatch):
    client, event, rooms, *_ = environment
    room = rooms[0]
    monkeypatch.setattr(settings, "program_ingest_disconnect_grace_secs", 30)
    token = await enable(client, event, room)
    original = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=original)).status_code == 204

    # Reproduce a reservation whose publisher never completed negotiation and
    # whose grace period elapsed. Reconciliation clears the DB reservation but
    # deliberately keeps room health for the status surface.
    elapsed = ingest.time.monotonic() - 31
    ingest.health[room.id].reservation_started_at = elapsed
    ingest.health[room.id].missing_since = elapsed
    await ingest.reconcile_once()
    async with get_session() as session:
        saved = await session.get(Room, room.id)
        assert saved.program_session_id is None

    reconnect = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=reconnect)).status_code == 204
    await ingest.reconcile_once()

    async with get_session() as session:
        saved = await session.get(Room, room.id)
        assert saved.program_session_id == reconnect["id"]


@pytest.mark.anyio
async def test_video_only_and_two_room_isolation(environment):
    client, event, rooms, media, _, start, _ = environment
    for room in rooms:
        token = await enable(client, event, room)
        auth = credentials(room, token)
        assert (await client.post("/internal/media-auth", json=auth)).status_code == 204
        media[auth["path"]] = {
            "name": auth["path"],
            "ready": True,
            "source": {"type": "webRTCSession", "id": auth["id"]},
            "tracks": ["H264"] if room.id == rooms[0].id else ["Opus"],
            "bytesReceived": 50,
        }
    await ingest.reconcile_once()
    assert start.await_count == 1
    assert start.call_args.kwargs["room_id"] == rooms[1].id
    assert (await client.get(admin_url(event, rooms[0]))).json()["state"] == "degraded"
    assert "Opus" in (await client.get(admin_url(event, rooms[0]))).json()["reason"]


@pytest.mark.anyio
async def test_program_final_pipeline_preserves_timing_and_multiple_languages(environment, monkeypatch):
    client, event, rooms, *_ = environment
    room = rooms[0]
    await enable(client, event, room)
    async with get_session() as session:
        saved = await session.get(Room, room.id)
        saved.program_sync_offset_ms = 5000
        saved.floor_translation_enabled = True
        saved.floor_translation_provider = "local"
        saved.floor_translation_model = "test"
        for code, name in [("fr", "French"), ("es", "Spanish")]:
            session.add(RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=name, enabled=True))
    from portal.translations.worker import TranslationWorker

    monkeypatch.setattr(TranslationWorker, "_call_llm", AsyncMock(return_value="translated"))
    synth = AsyncMock(return_value=b"\x00\x01")
    monkeypatch.setattr("portal.tts.worker.synthesize", synth)
    frames = []

    class Listener:
        async def send_bytes(self, data):
            length = struct.unpack(">I", data[1:5])[0]
            frames.append((json.loads(data[5 : 5 + length]), data[5 + length :]))

    booth = f"program-test-{room.id}-floor"
    listener = Listener()
    for lang in ["fr", "es"]:
        tts_manager._rooms[tts_manager._get_key(room.id, lang, booth)] = {listener}
    try:
        callback = AsyncMock()
        aggregator = CaptionAggregator(callback, room.id)
        await aggregator.handle_partial(booth, "hello")
        assert not synth.called
        await aggregator.handle_final(booth, "hello world")
        for _ in range(100):
            if len(frames) == 4:
                break
            await asyncio.sleep(0.01)
        assert len(frames) == 4
        final = [call.args[1] for call in callback.call_args_list if call.args[1].get("status") == "final"][0]
        assert all(
            header["segment_id"] == final["segment_id"] and header["seq"] == final["seq"] for header, _ in frames
        )
        assert all(header["sync_offset_ms"] == 5000 for header, _ in frames)
        assert frames[0][1] == b""
        assert synth.await_count == 2
        async with get_session() as session:
            from sqlalchemy import select

            assert len(list(await session.scalars(select(TranscriptSegment)))) == 1
        # A fresh aggregator (provider reconnect) must not reset the room sequence.
        second = CaptionAggregator(callback, room.id)
        await second.handle_final(booth, "next")
        finals = [call.args[1] for call in callback.call_args_list if call.args[1].get("status") == "final"]
        assert finals[-1]["seq"] == final["seq"] + 1
        await asyncio.sleep(0.05)
    finally:
        for lang in ["fr", "es"]:
            tts_manager._rooms.pop(tts_manager._get_key(room.id, lang, booth), None)
