"""Room-scoped RTMPS ingest with YouTube relay (#749): portal control plane."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from fastapi_app import app
from portal import program_ingest as ingest
from portal import program_relay as relay
from portal.auth import create_user_token
from portal.config import settings
from portal.crypto import decrypt_val
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import Event, Room

RELAY_SECRET = "r" * 64
YOUTUBE_SERVER = "rtmps://a.rtmps.youtube.com/live2"
YOUTUBE_KEY = "abcd-efgh-ijkl-mnop-qrst"


@pytest.fixture
async def relay_env(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "program_relay_enabled", True)
    monkeypatch.setattr(settings, "program_relay_control_secret", RELAY_SECRET)
    monkeypatch.setattr(settings, "program_rtmps_public_base", "rtmps://ingest.example.test:1936")
    monkeypatch.setattr(settings, "program_relay_max", 4)
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", "")
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "program_ingest_disconnect_grace_secs", 0)
    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    relay.relay_health.clear()
    relay.list_outage.failures = 0
    media: dict[str, dict] = {}
    relays: dict[str, dict] = {}
    pushed: dict[str, str] = {}
    calls: list[tuple[str, str]] = []
    service = {"up": True}

    def control(request):
        calls.append((request.method, request.url.path))
        if request.url.host == "program-relay":
            assert request.headers["authorization"] == f"Bearer {RELAY_SECRET}"
            if not service["up"]:
                return httpx.Response(500, text=f"upstream said {YOUTUBE_KEY}")
            if request.method == "GET":
                return httpx.Response(200, json={"relays": relays})
            relay_id = request.url.path.rsplit("/", 1)[1]
            if request.method == "DELETE":
                return httpx.Response(200, json={"stopped": relays.pop(relay_id, None) is not None})
            body = json.loads(request.content)
            if relay_id not in relays and len(relays) >= 4:
                return httpx.Response(503, json={"error": "Relay capacity reached"})
            pushed[relay_id] = body["destination"]
            relays[relay_id] = {
                "source": body["source"],
                "fingerprint": body["fingerprint"],
                "state": "live",
                "reason": "Relaying the program feed to the destination.",
            }
            return httpx.Response(200, json=relays[relay_id])
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
                email="relay-admin@example.com",
                display_name="Relay Admin",
                email_verified=True,
                is_admin=True,
            )
            event = Event(slug="relay-test", display_name="Relay Test")
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
                "relays": relays,
                "pushed": pushed,
                "calls": calls,
                "service": service,
                "start": start,
                "stop": stop,
            }
    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    ingest.audio_progress.clear()
    relay.relay_health.clear()
    await dispose()


def ingest_url(event, room):
    return f"/admin/events/{event.id}/rooms/{room.id}/program-ingest"


def relay_url(event, room):
    return f"/admin/events/{event.id}/rooms/{room.id}/program-relay"


async def enable_relay(client, event, room):
    response = await client.post(ingest_url(event, room), json={"action": "enable", "mode": "rtmps_relay"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stream_key"] == f"floor?key={body['secret']}"
    return body["secret"]


async def save_destination(client, event, room, key=YOUTUBE_KEY):
    response = await client.put(relay_url(event, room), json={"server_url": YOUTUBE_SERVER, "stream_key": key})
    assert response.status_code == 200, response.text


def rtmps_publish(room, secret, **overrides):
    """What MediaMTX sends for OBS server rtmps://host:1936/relay-test/{room} + key floor?key=..."""
    return {
        "action": "publish",
        "path": f"relay-test/{room.id}/floor",
        "protocol": "rtmp",
        "query": f"key={secret}",
        "id": str(uuid4()),
        "ip": f"198.51.100.{uuid4().int % 200 + 1}",
        **overrides,
    }


def live_feed(env, room, session_id, tracks=("H264", "MPEG-4 Audio")):
    path = f"relay-test/{room.id}/floor"
    env["media"][path] = {
        "name": path,
        "ready": True,
        "source": {"type": "rtmpsConn", "id": session_id},
        "tracks": list(tracks),
        "bytesReceived": 4096,
    }


async def connect(env, room, secret):
    auth = rtmps_publish(room, secret)
    assert (await env["client"].post("/internal/media-auth", json=auth)).status_code == 204
    live_feed(env, room, auth["id"])
    return auth


def test_destination_validation_is_allowlisted_rtmps_only():
    assert relay.normalize_destination("rtmps://A.RTMPS.youtube.com/live2/", YOUTUBE_KEY) == YOUTUBE_SERVER
    assert relay.normalize_destination("rtmps://b.rtmps.youtube.com:443/live2", YOUTUBE_KEY) == (
        "rtmps://b.rtmps.youtube.com:443/live2"
    )
    rejected = [
        ("rtmp://a.rtmps.youtube.com/live2", YOUTUBE_KEY),
        ("https://a.rtmps.youtube.com/live2", YOUTUBE_KEY),
        ("rtmps://evil.example/live2", YOUTUBE_KEY),
        ("rtmps://127.0.0.1/live2", YOUTUBE_KEY),
        ("rtmps://mediamtx/live2", YOUTUBE_KEY),
        ("rtmps://user:pass@a.rtmps.youtube.com/live2", YOUTUBE_KEY),
        ("rtmps://a.rtmps.youtube.com/live2?x=1", YOUTUBE_KEY),
        ("rtmps://a.rtmps.youtube.com", YOUTUBE_KEY),
        (YOUTUBE_SERVER, "key with spaces"),
        (YOUTUBE_SERVER, "../../etc"),
        (YOUTUBE_SERVER, "abc"),
    ]
    for server, key in rejected:
        with pytest.raises(ValueError):
            relay.normalize_destination(server, key)


@pytest.mark.anyio
async def test_destination_encrypted_write_only_rotatable_and_revocable(relay_env, caplog):
    client, event, room = relay_env["client"], relay_env["event"], relay_env["rooms"][0]
    await enable_relay(client, event, room)
    await save_destination(client, event, room)
    async with get_session() as session:
        saved = await session.get(Room, room.id)
        assert saved.program_relay_key_enc and YOUTUBE_KEY not in saved.program_relay_key_enc
        assert decrypt_val(saved.program_relay_key_enc) == YOUTUBE_KEY
    status = await client.get(ingest_url(event, room))
    assert YOUTUBE_KEY not in status.text and "key_enc" not in status.text
    assert status.json()["destination"] == {
        "configured": True,
        "server_url": YOUTUBE_SERVER,
        "updated_at": status.json()["destination"]["updated_at"],
    }
    # Invalid input never replaces a working destination.
    bad = await client.put(
        relay_url(event, room), json={"server_url": "rtmps://evil.example/live2", "stream_key": "x" * 8}
    )
    assert bad.status_code == 422
    rotated = "zzzz-yyyy-xxxx-wwww-vvvv"
    await save_destination(client, event, room, rotated)
    async with get_session() as session:
        assert decrypt_val((await session.get(Room, room.id)).program_relay_key_enc) == rotated
    cleared = await client.delete(relay_url(event, room))
    assert cleared.status_code == 200 and cleared.json()["configured"] is False
    async with get_session() as session:
        saved = await session.get(Room, room.id)
        assert saved.program_relay_key_enc is None and saved.program_relay_server is None
    assert ("DELETE", f"/relays/{room.id}") in relay_env["calls"]
    assert YOUTUBE_KEY not in caplog.text and rotated not in caplog.text


@pytest.mark.anyio
async def test_request_validation_errors_never_echo_the_stream_key(relay_env):
    client, event, room = relay_env["client"], relay_env["event"], relay_env["rooms"][0]
    long_key = "k" * 200 + "-unique-tail"
    for payload in [
        {"server_url": YOUTUBE_SERVER, "stream_key": long_key},  # over max_length
        {"server_url": YOUTUBE_SERVER, "stream_key": [long_key]},  # wrong type
        {"stream_key": long_key},  # "missing" reports the whole body as its input
    ]:
        response = await client.put(relay_url(event, room), json=payload)
        assert response.status_code == 422, response.text
        assert long_key not in response.text and "unique-tail" not in response.text
        assert response.json()["detail"], "validation details are still reported"
    # Other validation errors keep FastAPI's normal response, including the rejected input.
    response = await client.post(ingest_url(event, room), json={"action": "bogus-action"})
    assert response.status_code == 422
    assert any(error.get("input") == "bogus-action" for error in response.json()["detail"])


@pytest.mark.anyio
async def test_relay_requires_operator_flag_owner_and_origin(relay_env, monkeypatch):
    client, event, room = relay_env["client"], relay_env["event"], relay_env["rooms"][0]
    put = {"server_url": YOUTUBE_SERVER, "stream_key": YOUTUBE_KEY}
    assert (
        await client.put(relay_url(event, room), json=put, headers={"Origin": "https://evil.example"})
    ).status_code == 403
    assert (
        await client.put(relay_url(event, room).replace(f"events/{event.id}", "events/999"), json=put)
    ).status_code == 404
    owner_cookie = client.cookies.get("user_token")
    client.cookies.clear()
    assert (await client.put(relay_url(event, room), json=put)).status_code == 403
    client.cookies.set("user_token", owner_cookie)
    monkeypatch.setattr(settings, "program_relay_enabled", False)
    response = await client.post(ingest_url(event, room), json={"action": "enable", "mode": "rtmps_relay"})
    assert response.status_code == 409
    assert (await client.put(relay_url(event, room), json=put)).status_code == 409
    assert (await client.get(ingest_url(event, room))).json()["relay_available"] is False


@pytest.mark.anyio
async def test_rtmps_publish_is_scoped_to_one_room_and_mode(relay_env, monkeypatch, caplog):
    client, event = relay_env["client"], relay_env["event"]
    room, other = relay_env["rooms"]
    secret = await enable_relay(client, event, room)
    status = (await client.get(ingest_url(event, room))).json()
    assert status["mode"] == "rtmps_relay"
    assert status["rtmps_server"] == f"rtmps://ingest.example.test:1936/relay-test/{room.id}"
    whip_secret = (await client.post(ingest_url(event, other), json={"action": "enable"})).json()["secret"]

    async def auth(payload):
        return (await client.post("/internal/media-auth", json=payload)).status_code

    rejected = [
        rtmps_publish(other, secret, path=f"relay-test/{other.id}/floor"),  # wrong room
        rtmps_publish(room, ""),  # anonymous
        rtmps_publish(room, secret, query=""),  # no key at all
        rtmps_publish(room, "wrong"),
        rtmps_publish(room, secret, query=f"key={secret}&key={secret}"),
        rtmps_publish(room, secret, query=f"key={secret}&user=x"),
        rtmps_publish(room, secret, protocol="webrtc", query="", token=secret),  # WHIP replay of relay key
        rtmps_publish(other, whip_secret),  # WHIP-mode room never accepts RTMP
        rtmps_publish(room, secret, protocol="srt"),
    ]
    for payload in rejected:
        assert await auth(payload) == 401, payload
    assert await auth(rtmps_publish(room, secret)) == 204
    # Rotation invalidates the old key; revocation and expiry fail closed.
    rotated = (await client.post(ingest_url(event, room), json={"action": "rotate"})).json()
    assert rotated["stream_key"] == f"floor?key={rotated['secret']}"
    ingest.health.clear()
    assert await auth(rtmps_publish(room, secret)) == 401
    assert await auth(rtmps_publish(room, rotated["secret"])) == 204
    await client.post(ingest_url(event, room), json={"action": "revoke"})
    assert await auth(rtmps_publish(room, rotated["secret"])) == 401
    fresh = (await client.post(ingest_url(event, room), json={"action": "rotate"})).json()["secret"]
    async with get_session() as session:
        (await session.get(Room, room.id)).program_key_expires_at = ingest.utc_now() - timedelta(seconds=1)
    assert await auth(rtmps_publish(room, fresh)) == 401
    # The operator switch closes RTMPS even for rooms already in relay mode.
    async with get_session() as session:
        (await session.get(Room, room.id)).program_key_expires_at = ingest.utc_now() + timedelta(days=1)
    monkeypatch.setattr(settings, "program_relay_enabled", False)
    assert await auth(rtmps_publish(room, fresh)) == 401
    for value in (secret, rotated["secret"], fresh):
        assert value not in caplog.text


@pytest.mark.anyio
async def test_relay_follows_feed_and_is_isolated_from_language_processing(relay_env):
    env = relay_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_relay(client, event, room)
    await save_destination(client, event, room)
    await connect(env, room, secret)
    await ingest.reconcile_once()
    assert env["relays"][str(room.id)]["source"] == f"relay-test/{room.id}/floor"
    assert env["pushed"][str(room.id)] == f"{YOUTUBE_SERVER}/{YOUTUBE_KEY}"
    assert env["start"].call_args.kwargs["room_id"] == room.id
    health = (await client.get(ingest_url(event, room))).json()["health"]
    assert health["ingest"]["state"] == "connected"
    assert health["relay"]["state"] == "live"
    assert health["processing"]["state"] == "processing"
    # Steady state: no repeated PUT carrying the destination.
    env["calls"].clear()
    await ingest.reconcile_once()
    assert ("PUT", f"/relays/{room.id}") not in env["calls"]
    # Captions turned off: processing degrades, YouTube keeps flowing.
    async with get_session() as session:
        (await session.get(Room, room.id)).floor_transcription_enabled = False
    await ingest.reconcile_once()
    health = (await client.get(ingest_url(event, room))).json()["health"]
    assert health["processing"]["state"] == "degraded"
    assert health["relay"]["state"] == "live" and str(room.id) in env["relays"]
    # Relay service outage: relay health degrades, ingest stays connected, nothing leaks.
    env["service"]["up"] = False
    await ingest.reconcile_once()
    health = (await client.get(ingest_url(event, room))).json()["health"]
    assert health["relay"]["state"] == "degraded" and health["ingest"]["state"] == "connected"
    assert YOUTUBE_KEY not in json.dumps(health)


@pytest.mark.anyio
async def test_processing_failure_never_stops_the_relay(relay_env, monkeypatch):
    env = relay_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_relay(client, event, room)
    await save_destination(client, event, room)
    await connect(env, room, secret)
    await ingest.reconcile_once()
    assert str(room.id) in env["relays"]
    env["calls"].clear()
    # Worker cap / corrupt provider key: start_transcription_worker raises ValueError.
    env["start"].side_effect = ValueError("Maximum transcription workers reached")
    for _ in range(2):
        await ingest.reconcile_once()
    health = (await client.get(ingest_url(event, room))).json()["health"]
    assert str(room.id) in env["relays"] and ("DELETE", f"/relays/{room.id}") not in env["calls"]
    assert health["relay"]["state"] == "live"
    assert health["processing"]["state"] == "degraded" and health["ingest"]["state"] == "connected"


@pytest.mark.anyio
async def test_processing_error_stops_running_worker_and_keeps_relay(relay_env, monkeypatch):
    env = relay_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_relay(client, event, room)
    await save_destination(client, event, room)
    await connect(env, room, secret)
    await ingest.reconcile_once()
    booth_id = f"relay-test-{room.id}-floor"
    assert env["start"].await_count == 1 and ingest.health[room.id].worker_config
    env["stop"].reset_mock()

    def broken_settings(*_args):
        raise ValueError("undecryptable provider key")

    monkeypatch.setattr(ingest, "pipeline_problem", broken_settings)
    # The stop itself fails first: the config is kept so the next cycle retries, relay unaffected.
    env["stop"].side_effect = RuntimeError("worker stop failed")
    await ingest.reconcile_once()
    assert ingest.health[room.id].worker_config and str(room.id) in env["relays"]
    env["stop"].side_effect = None
    await ingest.reconcile_once()
    env["stop"].assert_any_await(booth_id)
    assert ingest.health[room.id].worker_config is None
    health = (await client.get(ingest_url(event, room))).json()["health"]
    assert health["relay"]["state"] == "live" and health["processing"]["state"] == "degraded"
    assert str(room.id) in env["relays"]


@pytest.mark.anyio
async def test_stuck_worker_stop_does_not_block_other_rooms_relays(relay_env):
    env = relay_env
    client, event = env["client"], env["event"]
    first, second = env["rooms"]
    for room in (first, second):
        secret = await enable_relay(client, event, room)
        await save_destination(client, event, room)
        await connect(env, room, secret)
    await ingest.reconcile_once()
    assert set(env["relays"]) == {str(first.id), str(second.id)}
    # The first room's worker cannot be stopped; the relay service also lost the second relay.
    async with get_session() as session:
        (await session.get(Room, first.id)).floor_transcription_enabled = False
    env["stop"].side_effect = RuntimeError("worker stop failed")
    env["relays"].pop(str(second.id))
    await ingest.reconcile_once()
    # Reconciliation continued past the first room, and sync restored the second relay.
    assert set(env["relays"]) == {str(first.id), str(second.id)}
    assert ingest.health[first.id].worker_config is not None  # retried next cycle
    env["stop"].side_effect = None
    await ingest.reconcile_once()
    assert ingest.health[first.id].worker_config is None


@pytest.mark.anyio
async def test_reconcile_failure_does_not_report_stale_live_relay(relay_env, monkeypatch):
    env = relay_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_relay(client, event, room)
    await save_destination(client, event, room)
    await connect(env, room, secret)
    await ingest.reconcile_once()
    assert (await client.get(ingest_url(event, room))).json()["health"]["relay"]["state"] == "live"

    async def broken(*_args):
        raise httpx.ConnectError("media control down")

    monkeypatch.setattr(ingest, "reconcile_room", broken)
    await ingest.reconcile_once()
    relay_health = (await client.get(ingest_url(event, room))).json()["health"]["relay"]
    assert relay_health["state"] == "degraded" and str(room.id) not in env["relays"]


@pytest.mark.anyio
async def test_relay_service_outage_is_logged_once_not_every_tick(relay_env, caplog):
    env = relay_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_relay(client, event, room)
    await save_destination(client, event, room)
    await connect(env, room, secret)
    env["service"]["up"] = False
    for _ in range(5):
        await ingest.reconcile_once()
    relay_health = (await client.get(ingest_url(event, room))).json()["health"]["relay"]
    assert relay_health["state"] == "degraded"
    env["service"]["up"] = True
    await ingest.reconcile_once()
    await ingest.reconcile_once()
    lines = [record.getMessage() for record in caplog.records if record.name == relay.logger.name]
    assert lines.count("program_relay list_unavailable") == 1
    recovered = [line for line in lines if "recovered" in line]
    assert len(recovered) == 1 and recovered[0].startswith("program_relay list_unavailable recovered failures=5 ")
    assert YOUTUBE_KEY not in caplog.text


@pytest.mark.anyio
async def test_incompatible_feed_and_missing_destination_do_not_relay(relay_env):
    env = relay_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_relay(client, event, room)
    auth = await connect(env, room, secret)
    await ingest.reconcile_once()
    assert env["relays"] == {}
    assert (await client.get(ingest_url(event, room))).json()["health"]["relay"]["state"] == "unconfigured"
    await save_destination(client, event, room)
    live_feed(env, room, auth["id"], tracks=("Opus",))
    await ingest.reconcile_once()
    relay_health = (await client.get(ingest_url(event, room))).json()["health"]["relay"]
    assert env["relays"] == {} and relay_health["state"] == "degraded" and "H.264" in relay_health["reason"]


@pytest.mark.anyio
async def test_capacity_fails_closed_without_displacing_live_relay(relay_env, monkeypatch):
    env = relay_env
    client, event = env["client"], env["event"]
    first, second = env["rooms"]
    for room in (first, second):
        secret = await enable_relay(client, event, room)
        await save_destination(client, event, room)
        await connect(env, room, secret)
    monkeypatch.setattr(settings, "program_relay_max", 1)
    env["relays"]["999"] = {"source": "gone/999/floor", "fingerprint": "0" * 32, "state": "live"}
    env["relays"][str(second.id)] = {
        "source": f"relay-test/{second.id}/floor",
        "fingerprint": "f" * 32,
        "state": "live",
    }
    await ingest.reconcile_once()
    # The orphan is stopped; the already-live room keeps its slot; the other room is refused.
    assert "999" not in env["relays"]
    assert set(env["relays"]) == {str(second.id)}
    capacity = (await client.get(ingest_url(event, first))).json()["health"]["relay"]
    assert capacity["state"] == "capacity" and "capacity" in capacity["reason"]


@pytest.mark.anyio
async def test_stop_revoke_and_source_switch_leave_no_relay(relay_env):
    env = relay_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_relay(client, event, room)
    await save_destination(client, event, room)
    await connect(env, room, secret)
    await ingest.reconcile_once()
    assert str(room.id) in env["relays"]
    await client.post(ingest_url(event, room), json={"action": "revoke"})
    assert str(room.id) not in env["relays"]
    encoder_session = env["media"][f"relay-test/{room.id}/floor"]["source"]["id"]
    assert ("POST", f"/v3/rtmpsconns/kick/{encoder_session}") in env["calls"]
    await ingest.reconcile_once()
    assert str(room.id) not in env["relays"]
    async with get_session() as session:
        assert (await session.get(Room, room.id)).program_session_id is None
    # Switching to the independent WHIP mode stops relaying and rebinds the credential.
    secret = (await client.post(ingest_url(event, room), json={"action": "rotate"})).json()["secret"]
    env["media"].clear()
    await connect(env, room, secret)
    await ingest.reconcile_once()
    assert str(room.id) in env["relays"]
    switched = await client.post(ingest_url(event, room), json={"action": "enable", "mode": "whip"})
    assert "stream_key" not in switched.json()
    assert str(room.id) not in env["relays"]
    assert (
        await client.post("/internal/media-auth", json=rtmps_publish(room, switched.json()["secret"]))
    ).status_code == 401
    # Returning the floor to the Jitsi bot also leaves nothing behind.
    secret = await enable_relay(client, event, room)
    env["media"].clear()
    await connect(env, room, secret)
    await ingest.reconcile_once()
    await client.post(ingest_url(event, room), json={"action": "disable"})
    await ingest.reconcile_once()
    assert env["relays"] == {}


@pytest.mark.anyio
async def test_browser_sender_cannot_take_relay_floor(relay_env):
    client, event, room = relay_env["client"], relay_env["event"], relay_env["rooms"][0]
    await enable_relay(client, event, room)
    response = await client.post(f"/admin/events/{event.id}/rooms/{room.id}/program-sender", json={})
    assert response.status_code == 409
    assert "relay" in response.json()["detail"]


@pytest.mark.anyio
async def test_workspace_namespace_reaches_relay_destination(relay_env):
    client, event, room = relay_env["client"], relay_env["event"], relay_env["rooms"][0]
    response = await client.put(
        f"/workspace/events/{event.id}/rooms/{room.id}/program-relay",
        json={"server_url": YOUTUBE_SERVER, "stream_key": YOUTUBE_KEY},
    )
    assert response.status_code == 200
