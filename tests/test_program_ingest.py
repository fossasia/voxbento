from __future__ import annotations

import asyncio
import contextlib
import json
import struct
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from fastapi_app import app
from portal import floor_language
from portal import program_ingest as ingest
from portal.auth import create_user_token, decode_token, generate_admin_csrf_token
from portal.config import settings
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import Event, Room, RoomTranslationLanguage, TranscriptSegment
from portal.transcription import aggregator as aggregator_module
from portal.transcription import worker as worker_module
from portal.transcription.aggregator import CLEAR_CAPTION, CaptionAggregator
from portal.transcription.providers.base import ProviderConfig, TranscriptionProvider
from portal.transcription.worker import active_workers
from portal.translations.worker import TranslationWorker
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
            admin = await create_user(
                session,
                email="program-admin@example.com",
                display_name="Program Admin",
                email_verified=True,
                is_admin=True,
            )
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
        admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)
        session_id = decode_token(admin_token)["jti"]
        csrf_token = generate_admin_csrf_token(str(session_id))

        session_id = decode_token(admin_token)["jti"]
        csrf_token = generate_admin_csrf_token(str(session_id))

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://test",
            cookies={"user_token": admin_token, "admin_csrf": csrf_token},
            headers={"X-CSRF-Token": csrf_token},
        ) as client:
            yield client, event, rooms, media, calls, start, stop, csrf_token
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
    client, event, rooms, *_ = environment
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
    client, event, rooms, *_ = environment
    await enable(client, event, rooms[0])
    for _ in range(10):
        assert (await client.post("/internal/media-auth", json=credentials(rooms[0], ""))).status_code == 401
    assert (await client.post("/internal/media-auth", json=credentials(rooms[0], ""))).status_code == 429
    assert (await client.post("/internal/media-auth", json={"token": "secret"})).status_code == 401
    # Interpreter booth paths share the throttle and no longer bypass authorization (#783).
    assert (
        await client.post("/internal/media-auth", json=credentials(rooms[0], "", path="program-test/1/fr"))
    ).status_code == 429


@pytest.mark.anyio
async def test_owner_guard_csrf_and_cross_event(environment):
    client, event, rooms, *_rest, csrf_token = environment
    url = admin_url(event, rooms[0])
    admin_token = client.cookies.get("user_token")
    client.cookies.clear()

    assert (await client.post(url, json={"action": "enable"})).status_code == 403

    client.cookies.set("user_token", admin_token)
    client.cookies.set("admin_csrf", csrf_token)

    assert (
        await client.post(
            url,
            json={"action": "enable"},
            headers={"Origin": "https://evil.example", "X-CSRF-Token": csrf_token},
        )
    ).status_code == 403

    assert (
        await client.post(
            url.replace(f"events/{event.id}", "events/999"),
            json={"action": "enable"},
            headers={"X-CSRF-Token": csrf_token},
        )
    ).status_code == 404


@pytest.mark.anyio
async def test_source_conflict_capacity_and_recovery(environment, monkeypatch):
    client, event, rooms, media, calls, start, stop, *_ = environment
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
    client, event, rooms, media, calls, start, stop, *_ = environment
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
async def test_reconnect_does_not_reuse_stale_worker_start_time(environment, monkeypatch):
    client, event, rooms, media, _, start, stop, *_ = environment
    room = rooms[0]
    monkeypatch.setattr(settings, "program_ingest_stall_secs", 10)
    token = await enable(client, event, room)
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
    ingest.health[room.id].worker_started_at = ingest.time.monotonic() - 11

    media.clear()
    ingest.audio_progress.pop(f"program-test-{room.id}-floor", None)
    await ingest.reconcile_once()
    stop_count = stop.await_count

    reconnect = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=reconnect)).status_code == 204
    start.side_effect = None  # Real ffmpeg does not report PCM in the same reconciliation tick.
    media[path] = {
        "name": path,
        "ready": True,
        "source": {"type": "webRTCSession", "id": reconnect["id"]},
        "tracks": ["Opus"],
        "bytesReceived": 20,
    }
    await ingest.reconcile_once()

    assert ingest.health[room.id].state == "receiving"
    assert stop.await_count == stop_count


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
    client, event, rooms, media, _, start, _, *_ = environment
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
    ingest.health.setdefault(rooms[0].id, ingest.IngestHealth()).worker_started_at = ingest.time.monotonic() - 30
    await ingest.reconcile_once()
    assert start.await_count == 1
    assert start.call_args.kwargs["room_id"] == rooms[1].id
    assert (await client.get(admin_url(event, rooms[0]))).json()["state"] == "degraded"
    assert "Opus" in (await client.get(admin_url(event, rooms[0]))).json()["reason"]
    assert ingest.health[rooms[0].id].worker_started_at is None


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


@pytest.mark.anyio
async def test_deleted_event_room_worker_is_stopped(environment, monkeypatch):
    client, event, rooms, media, _, start, stop, *_ = environment
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
    await ingest.reconcile_once()
    assert start.await_count == 1
    monkeypatch.setitem(active_workers, f"program-test-{room.id}-floor", object())  # the running worker

    async with get_session() as session:
        db_event = await session.get(Event, event.id)
        db_event.deleted_at = datetime.now(timezone.utc)

    start.reset_mock()
    stop.reset_mock()
    await ingest.reconcile_once()
    start.assert_not_awaited()
    stop.assert_awaited_once_with(f"program-test-{room.id}-floor")
    assert room.id not in ingest.health


@pytest.mark.anyio
async def test_deleted_event_worker_is_stopped_while_media_listing_fails(environment, monkeypatch):
    client, event, rooms, media, _, start, stop, *_ = environment
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
    await ingest.reconcile_once()
    assert start.await_count == 1
    monkeypatch.setitem(active_workers, f"program-test-{room.id}-floor", object())  # the running worker

    async with get_session() as session:
        other = Event(slug="other-event", display_name="Other Event")
        session.add(other)
        await session.flush()
        session.add(Room(event_id=other.id, display_name="Other Room", floor_source="program_ingest"))
        db_event = await session.get(Event, event.id)
        db_event.deleted_at = datetime.now(timezone.utc)

    # Another room is still active, so the media paths are requested, and that request fails.
    monkeypatch.setattr(ingest, "media_paths", AsyncMock(side_effect=httpx.ConnectError("media down")))
    stop.reset_mock()
    await ingest.reconcile_once()
    stop.assert_awaited_once_with(f"program-test-{room.id}-floor")


@pytest.mark.anyio
async def test_deleted_event_worker_is_stopped_after_its_health_record_is_dropped(environment, monkeypatch):
    client, event, rooms, media, _, start, stop, *_ = environment
    room = rooms[0]
    booth_id = f"program-test-{room.id}-floor"
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
    await ingest.reconcile_once()
    assert start.await_count == 1
    monkeypatch.setitem(active_workers, booth_id, object())  # the running worker
    # close_sender_session drops the room's health record without stopping its worker.
    ingest.health.pop(room.id, None)

    async with get_session() as session:
        db_event = await session.get(Event, event.id)
        db_event.deleted_at = datetime.now(timezone.utc)

    stop.reset_mock()
    await ingest.reconcile_once()
    stop.assert_awaited_once_with(booth_id)


async def soft_delete(event):
    async with get_session() as session:
        (await session.get(Event, event.id)).deleted_at = datetime.now(timezone.utc)


async def connect_whip(client, event, room, media):
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
    return token, auth


@pytest.mark.anyio
@pytest.mark.parametrize("delete_room_only", [True, False])
async def test_worker_of_a_room_deleted_outright_is_stopped(environment, monkeypatch, delete_room_only):
    """The admin pages delete the rows, so there is no deleted_at to find the room by (#1311)."""
    client, event, rooms, media, _, start, stop, *_ = environment
    room, other = rooms
    await connect_whip(client, event, room, media)
    await connect_whip(client, event, other, media)
    await ingest.reconcile_once()
    assert start.await_count == 2
    booth_id, other_booth_id = f"program-test-{room.id}-floor", f"program-test-{other.id}-floor"
    monkeypatch.setitem(active_workers, booth_id, object())  # the running worker
    monkeypatch.setitem(active_workers, other_booth_id, object())  # another room's worker

    delete_url = (
        f"/admin/events/{event.id}/rooms/{room.id}/delete" if delete_room_only else f"/admin/events/{event.id}/delete"
    )
    assert (await client.post(delete_url)).status_code == 303
    stop.reset_mock()
    await ingest.reconcile_once()
    stopped = [call.args[0] for call in stop.await_args_list]
    assert stopped == ([booth_id] if delete_room_only else sorted([booth_id, other_booth_id]))


@pytest.mark.anyio
async def test_worker_of_a_deleted_room_is_stopped_when_another_event_reuses_its_id(environment, monkeypatch):
    """SQLite can hand a deleted room's id to the next room created, here in another event."""
    client, event, rooms, media, _, start, stop, *_ = environment
    room = rooms[0]
    await connect_whip(client, event, room, media)
    await ingest.reconcile_once()
    booth_id = f"program-test-{room.id}-floor"
    monkeypatch.setitem(active_workers, booth_id, object())  # the running worker
    assert (await client.post(f"/admin/events/{event.id}/rooms/{room.id}/delete")).status_code == 303
    async with get_session() as session:
        other = Event(slug="other-event", display_name="Other")
        session.add(other)
        await session.flush()
        session.add(Room(id=room.id, event_id=other.id, display_name="Reused", floor_source="program_ingest"))
    # The new room's own worker shares the room id and must keep running.
    monkeypatch.setitem(active_workers, f"other-event-{room.id}-floor", object())
    # Its settings live in the shared health record, which stopping the old worker must not clear.
    replacement_config = ("local", "tiny", "en")
    ingest.health.setdefault(room.id, ingest.IngestHealth()).worker_config = replacement_config
    stop.reset_mock()
    await ingest.stop_deleted_event_workers()
    assert [call.args[0] for call in stop.await_args_list] == [booth_id]
    assert ingest.health[room.id].worker_config == replacement_config


@pytest.mark.anyio
@pytest.mark.parametrize(("floor_source", "stopped"), [("jitsi_bot", True), ("program_ingest", False)])
async def test_worker_of_a_deleted_room_when_the_same_event_reuses_its_id(
    environment, monkeypatch, floor_source, stopped
):
    """In the same event a reused id gives the same booth id, so the new room's settings decide."""
    client, event, rooms, media, _, start, stop, *_ = environment
    room = rooms[0]
    await connect_whip(client, event, room, media)
    await ingest.reconcile_once()
    booth_id = f"program-test-{room.id}-floor"
    monkeypatch.setitem(active_workers, booth_id, object())  # the running worker
    assert (await client.post(f"/admin/events/{event.id}/rooms/{room.id}/delete")).status_code == 303
    async with get_session() as session:
        session.add(Room(id=room.id, event_id=event.id, display_name="Replacement", floor_source=floor_source))
    stop.reset_mock()
    await ingest.stop_deleted_event_workers()
    assert [call.args[0] for call in stop.await_args_list] == ([booth_id] if stopped else [])


@pytest.mark.anyio
async def test_deleted_event_publisher_is_kicked_and_reservation_released(environment):
    client, event, rooms, media, calls, *_ = environment
    room = rooms[0]
    token, auth = await connect_whip(client, event, room, media)
    await ingest.reconcile_once()
    await soft_delete(event)

    calls.clear()
    await ingest.reconcile_once()
    assert ("POST", f"/v3/webrtcsessions/kick/{auth['id']}") in calls
    async with get_session() as session:
        assert (await session.get(Room, room.id)).program_session_id is None
    # The same encoder reconnecting is refused, so the reservation cannot be taken back.
    retry = credentials(room, token)
    assert (await client.post("/internal/media-auth", json=retry)).status_code == 401


@pytest.mark.anyio
async def test_deleted_event_reservation_is_kept_until_the_kick_succeeds(environment, monkeypatch):
    client, event, rooms, media, *_ = environment
    room = rooms[0]
    await connect_whip(client, event, room, media)
    await ingest.reconcile_once()
    await soft_delete(event)

    with monkeypatch.context() as patch:
        patch.setattr(ingest, "kick", AsyncMock(side_effect=httpx.ConnectError("media down")))
        await ingest.reconcile_once()
    async with get_session() as session:
        assert (await session.get(Room, room.id)).program_session_id is not None

    await ingest.reconcile_once()
    async with get_session() as session:
        assert (await session.get(Room, room.id)).program_session_id is None


@pytest.mark.anyio
async def test_deleted_event_feed_is_released_while_media_listing_fails(environment, monkeypatch):
    client, event, rooms, media, calls, _, stop, *_ = environment
    room = rooms[0]
    token, auth = await connect_whip(client, event, room, media)
    await ingest.reconcile_once()
    monkeypatch.setitem(active_workers, f"program-test-{room.id}-floor", object())  # the running worker
    async with get_session() as session:
        other = Event(slug="other-event", display_name="Other Event")
        session.add(other)
        await session.flush()
        session.add(Room(event_id=other.id, display_name="Other Room", floor_source="program_ingest"))
    await soft_delete(event)

    # Another room is still active, so the media paths are requested, and that request fails.
    monkeypatch.setattr(ingest, "media_paths", AsyncMock(side_effect=httpx.ConnectError("media down")))
    calls.clear()
    stop.reset_mock()
    await ingest.reconcile_once()
    stop.assert_awaited_once_with(f"program-test-{room.id}-floor")
    assert ("POST", f"/v3/webrtcsessions/kick/{auth['id']}") in calls
    async with get_session() as session:
        assert (await session.get(Room, room.id)).program_session_id is None
    assert (await client.post("/internal/media-auth", json=credentials(room, token))).status_code == 401


@pytest.mark.anyio
async def test_program_floor_worker_follows_the_floor_language(environment, monkeypatch):
    """#1397: the program feed's worker restarts once in the language picked, after the debounce."""
    client, event, rooms, media, _, start, stop, *_ = environment
    room = rooms[0]
    clock = [100.0]
    monkeypatch.setattr(floor_language, "clock", lambda: clock[0])
    broadcast = AsyncMock()
    monkeypatch.setattr(ingest, "broadcast_transcription", broadcast)
    async with get_session() as session:
        session.add(RoomTranslationLanguage(room_id=room.id, language_code="th", language_name="Thai", enabled=True))
    _, auth = await connect_whip(client, event, room, media)
    await ingest.reconcile_once()
    assert start.await_args.kwargs["transcription_language"] == "en"
    booth_id = f"program-test-{room.id}-floor"
    stop.reset_mock()

    response = await client.post(f"/api/rooms/{room.id}/floor-language", json={"language_code": "th"})
    assert response.status_code == 200, response.text
    media[auth["path"]]["bytesReceived"] = 20
    await ingest.reconcile_once()
    assert stop.await_count == 0
    assert start.await_args.kwargs["transcription_language"] == "en"

    clock[0] += 1
    media[auth["path"]]["bytesReceived"] = 30
    await ingest.reconcile_once()
    stop.assert_awaited_once_with(booth_id)
    broadcast.assert_awaited_once_with(booth_id, CLEAR_CAPTION)
    assert start.await_args.kwargs["transcription_language"] == "th"


@pytest.mark.anyio
async def test_program_floor_language_restart_waits_for_a_failed_stop(environment, monkeypatch):
    """A worker that would not stop is not replaced, and no clear is sent for it."""
    client, event, rooms, media, _, start, stop, *_ = environment
    room = rooms[0]
    broadcast = AsyncMock()
    monkeypatch.setattr(ingest, "broadcast_transcription", broadcast)
    await connect_whip(client, event, room, media)
    await ingest.reconcile_once()
    starts = start.await_count
    async with get_session() as session:
        (await session.get(Room, room.id)).floor_language_override = "de"
    stop.reset_mock()
    stop.side_effect = RuntimeError("stuck")
    await ingest.reconcile_once()
    stop.assert_awaited_once()
    assert start.await_count == starts
    broadcast.assert_not_awaited()


class BlockedProvider(TranscriptionProvider):
    """Holds its first chunk like a slow engine call, then returns a sentence and the start of the next."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def process_chunk(self, chunk, language_code, model_variant, config, booth_state=None):
        self.entered.set()
        await self.release.wait()
        return "Hello everyone and welcome to the session. And now"


class EndlessStdout:
    """One chunk of audio, then a feed that stays open."""

    def __init__(self):
        self.sent = False

    async def readexactly(self, n):
        if not self.sent:
            self.sent = True
            return b"\0" * n
        await asyncio.Event().wait()


class EndlessProcess:
    returncode = None

    def __init__(self):
        self.stdout = EndlessStdout()


@contextlib.asynccontextmanager
async def endless_ffmpeg(*_args):
    yield EndlessProcess()


@pytest.mark.anyio
async def test_program_floor_language_switch_keeps_the_old_worker_off_screen(environment, monkeypatch):
    """#1397: words the old worker finishes after the switch's clear are stored, never shown or translated."""
    client, event, rooms, media, *_ = environment
    room = rooms[0]
    booth_id = f"program-test-{room.id}-floor"
    sent = []

    async def broadcast(booth, payload):
        sent.append(payload)

    provider = BlockedProvider()
    monkeypatch.setitem(worker_module.PROVIDERS, "blocked", provider)
    monkeypatch.setattr(worker_module, "FfmpegProcess", endless_ffmpeg)
    monkeypatch.setattr(ingest, "broadcast_transcription", broadcast)
    monkeypatch.setattr(ingest, "stop_transcription_worker", worker_module.stop_transcription_worker)
    translate = AsyncMock()
    monkeypatch.setattr(TranslationWorker, "handle_translation", translate)
    offered = []
    monkeypatch.setattr(aggregator_module.youtube_captions, "offer", lambda *args: offered.append(args))
    tasks_before = asyncio.all_tasks()

    await connect_whip(client, event, room, media)
    await ingest.reconcile_once()
    old = worker_module.TranscriptionWorkerSession(
        event.slug, "floor", booth_id, broadcast, "blocked", "tiny", ProviderConfig(api_key=None), "en", room.id
    )
    active_workers[booth_id] = old
    old.start()
    await asyncio.wait_for(provider.entered.wait(), 2)

    async with get_session() as session:
        (await session.get(Room, room.id)).floor_language_override = "de"
    await ingest.reconcile_once()
    assert booth_id not in active_workers
    assert sent == [CLEAR_CAPTION]

    provider.release.set()
    segment = None
    for _ in range(200):
        async with get_session() as session:
            segment = await session.scalar(select(TranscriptSegment).where(TranscriptSegment.room_id == room.id))
        if segment is not None:
            break
        await asyncio.sleep(0.01)
    for task in asyncio.all_tasks() - tasks_before:
        if task is not asyncio.current_task():
            task.cancel()

    assert sent == [CLEAR_CAPTION]
    assert (segment.text, segment.language_code, segment.source_language_code) == (
        "Hello everyone and welcome to the session.",
        "floor",
        "en",
    )
    translate.assert_not_awaited()
    assert offered == []
