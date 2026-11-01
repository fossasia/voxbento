"""RTMPS ingest as an independent extra destination (#1087, plan 004): no relay service."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from unittest import mock
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from fastapi_app import app
from portal import program_ingest as ingest
from portal import program_relay as relay
from portal.auth import create_user_token
from portal.config import settings
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import Event, Room

RELAY_SECRET = "r" * 64
YOUTUBE_SERVER = "rtmps://a.rtmps.youtube.com/live2"
YOUTUBE_KEY = "abcd-efgh-ijkl-mnop-qrst"


@pytest.fixture
async def rtmps_env(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "program_rtmps_enabled", True)
    monkeypatch.setattr(settings, "program_relay_enabled", False)
    monkeypatch.setattr(settings, "program_relay_control_secret", RELAY_SECRET)
    monkeypatch.setattr(settings, "program_rtmps_public_base", "rtmps://ingest.example.test:1936")
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
    calls: list[tuple[str, str]] = []

    def control(request):
        calls.append((request.method, request.url.host))
        if request.url.host == "program-relay":
            if request.method == "GET":
                return httpx.Response(200, json={"relays": relays})
            relay_id = request.url.path.rsplit("/", 1)[1]
            if request.method == "DELETE":
                return httpx.Response(200, json={"stopped": relays.pop(relay_id, None) is not None})
            relays[relay_id] = {"state": "live", "reason": "Relaying."}
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
                email="rtmps-admin@example.com",
                display_name="RTMPS Admin",
                email_verified=True,
                is_admin=True,
            )
            event = Event(slug="rtmps-test", display_name="RTMPS Test")
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
                "calls": calls,
                "start": start,
            }
    ingest.health.clear()
    ingest.failures.clear()
    ingest.room_locks.clear()
    ingest.audio_progress.clear()
    relay.relay_health.clear()
    await dispose()


def ingest_url(event, room):
    return f"/admin/events/{event.id}/rooms/{room.id}/program-ingest"


async def enable_rtmps(client, event, room):
    response = await client.post(ingest_url(event, room), json={"action": "enable", "mode": "rtmps"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stream_key"] == f"floor?key={body['secret']}"
    return body["secret"]


def rtmps_publish(room, secret, **overrides):
    """What MediaMTX sends for a "Custom RTMP" destination rtmps://host:1936/rtmps-test/{room} + floor?key=..."""
    return {
        "action": "publish",
        "path": f"rtmps-test/{room.id}/floor",
        "protocol": "rtmp",
        "query": f"key={secret}",
        "id": str(uuid4()),
        "ip": f"198.51.100.{uuid4().int % 200 + 1}",
        **overrides,
    }


def live_feed(env, room, session_id, tracks=("H264", "MPEG-4 Audio")):
    path = f"rtmps-test/{room.id}/floor"
    env["media"][path] = {
        "name": path,
        "ready": True,
        "source": {"type": "rtmpsConn", "id": session_id},
        "tracks": list(tracks),
        "bytesReceived": 4096,
    }


async def stored_room(room_id):
    async with get_session() as session:
        return await session.get(Room, room_id)


@pytest.mark.anyio
async def test_enable_rtmps_mode_issues_obs_style_stream_key(rtmps_env):
    client, event, room = rtmps_env["client"], rtmps_env["event"], rtmps_env["rooms"][0]
    secret = await enable_rtmps(client, event, room)
    stored = await stored_room(room.id)
    assert stored.program_ingest_mode == relay.RTMPS_MODE
    assert stored.floor_source == "program_ingest"
    # Rotation keeps the mode and the OBS key shape.
    rotated = (await client.post(ingest_url(event, room), json={"action": "rotate"})).json()
    assert rotated["secret"] != secret
    assert rotated["stream_key"] == f"floor?key={rotated['secret']}"
    assert (await stored_room(room.id)).program_ingest_mode == relay.RTMPS_MODE


@pytest.mark.anyio
async def test_rtmps_mode_requires_operator_flag(rtmps_env, monkeypatch):
    client, event, room = rtmps_env["client"], rtmps_env["event"], rtmps_env["rooms"][0]
    monkeypatch.setattr(settings, "program_rtmps_enabled", False)
    response = await client.post(ingest_url(event, room), json={"action": "enable", "mode": "rtmps"})
    assert response.status_code == 409
    assert "RTMPS ingest" in response.json()["detail"]
    status = (await client.get(ingest_url(event, room))).json()
    assert status["rtmps_available"] is False and status["enabled"] is False
    assert status["rtmps_server"] == ""  # never advertise a listener the operator has not opened
    # A relay deployment implies RTMPS ingest.
    monkeypatch.setattr(settings, "program_relay_enabled", True)
    assert settings.rtmps_ingest_enabled is True
    await enable_rtmps(client, event, room)


@pytest.mark.anyio
async def test_publish_permitted_binds_key_to_rtmps(rtmps_env):
    client, event, room = rtmps_env["client"], rtmps_env["event"], rtmps_env["rooms"][0]
    secret = await enable_rtmps(client, event, room)
    stored = await stored_room(room.id)
    digest = ingest.digest_program_secret(secret)
    assert ingest.publish_permitted(stored, "rtmp", digest) is True
    assert ingest.publish_permitted(stored, "webrtc", digest) is False
    assert ingest.publish_permitted(stored, "rtmp", ingest.digest_program_secret("wrong")) is False
    assert ingest.reserved_source(stored) is None
    stored.program_session_id = str(uuid4())
    assert ingest.reserved_source(stored).type == "rtmpsConn"


@pytest.mark.anyio
async def test_browser_sender_session_secret_never_authorizes_rtmps(rtmps_env):
    client, event, room = rtmps_env["client"], rtmps_env["event"], rtmps_env["rooms"][0]
    await enable_rtmps(client, event, room)
    session_secret = "browser-sender-session-secret"
    async with get_session() as session:
        stored = await session.get(Room, room.id)
        stored.program_session_secret_hash = ingest.digest_program_secret(session_secret)
        stored.program_session_secret_expires_at = ingest.utc_now() + timedelta(minutes=2)
    stored = await stored_room(room.id)
    assert ingest.publish_permitted(stored, "rtmp", ingest.digest_program_secret(session_secret)) is False
    response = await client.post("/internal/media-auth", json=rtmps_publish(room, session_secret))
    assert response.status_code == 401


def test_presented_secret_parses_key_with_rtmps_only(monkeypatch):
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "program_rtmps_enabled", True)
    monkeypatch.setattr(settings, "program_relay_enabled", False)

    def presented(**fields):
        payload = {"action": "publish", "path": "e/1/floor", "protocol": "rtmp", "ip": "198.51.100.1", **fields}
        return ingest.presented_secret(ingest.MediaAuth.model_validate(payload))

    assert presented(query="key=abc") == "abc"
    assert presented(query="key=abc&key=def") == ""
    assert presented(query="key=abc&user=x") == ""
    assert presented(query="key=abc", password="other") == ""
    monkeypatch.setattr(settings, "program_rtmps_enabled", False)
    assert presented(query="key=abc") == ""


@pytest.mark.anyio
async def test_browser_sender_cannot_take_rtmps_floor(rtmps_env):
    client, event, room = rtmps_env["client"], rtmps_env["event"], rtmps_env["rooms"][0]
    secret = await enable_rtmps(client, event, room)
    response = await client.post(f"/admin/events/{event.id}/rooms/{room.id}/program-sender", json={})
    assert response.status_code == 409
    assert "RTMPS encoder owns the floor" in response.json()["detail"]
    stored = await stored_room(room.id)
    assert stored.program_ingest_mode == relay.RTMPS_MODE
    assert ingest.publish_permitted(stored, "rtmp", ingest.digest_program_secret(secret)) is True


@pytest.mark.anyio
async def test_rtmps_mode_never_relays_even_with_destination(rtmps_env, monkeypatch):
    env = rtmps_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    monkeypatch.setattr(settings, "program_relay_enabled", True)
    secret = await enable_rtmps(client, event, room)
    response = await client.put(
        f"/admin/events/{event.id}/rooms/{room.id}/program-relay",
        json={"server_url": YOUTUBE_SERVER, "stream_key": YOUTUBE_KEY},
    )
    assert response.status_code == 200, response.text
    auth = rtmps_publish(room, secret)
    assert (await client.post("/internal/media-auth", json=auth)).status_code == 204
    live_feed(env, room, auth["id"])
    async with get_session() as session:
        row = await session.get(Room, room.id)
        stored_event = await session.get(Event, event.id)
        assert row.program_relay_key_enc
        path = (await ingest.media_paths())[f"rtmps-test/{room.id}/floor"]
        assert ingest.relay_request(row, stored_event, path) is None
    await ingest.reconcile_once()
    assert env["relays"] == {}
    assert ("PUT", "program-relay") not in env["calls"]
    health = (await client.get(ingest_url(event, room))).json()["health"]
    assert health["relay"]["state"] == "disabled"
    assert health["ingest"]["state"] == "connected"


@pytest.mark.anyio
async def test_status_reports_rtmps_without_relay(rtmps_env):
    client, event, room = rtmps_env["client"], rtmps_env["event"], rtmps_env["rooms"][0]
    status = (await client.get(ingest_url(event, room))).json()
    assert status["rtmps_available"] is True
    assert status["relay_available"] is False
    assert status["rtmps_server"] == f"rtmps://ingest.example.test:1936/rtmps-test/{room.id}"
    response = await client.post(ingest_url(event, room), json={"action": "enable", "mode": "rtmps_relay"})
    assert response.status_code == 409


@pytest.mark.anyio
async def test_connected_feed_drives_captions_without_relay_service(rtmps_env, caplog):
    env = rtmps_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_rtmps(client, event, room)
    auth = rtmps_publish(room, secret)
    assert (await client.post("/internal/media-auth", json=auth)).status_code == 204
    live_feed(env, room, auth["id"])
    await ingest.reconcile_once()
    assert env["start"].call_args.kwargs["room_id"] == room.id
    health = (await client.get(ingest_url(event, room))).json()["health"]
    assert health["ingest"]["state"] == "connected"
    assert health["processing"]["state"] == "processing"
    assert all(host != "program-relay" for _, host in env["calls"])
    # A feed without a decodable audio track gets the RTMPS (AAC) hint, not the WHIP one.
    live_feed(env, room, auth["id"], tracks=("H264",))
    await ingest.reconcile_once()
    ingest_health = (await client.get(ingest_url(event, room))).json()["health"]["ingest"]
    assert ingest_health["state"] == "degraded" and "AAC" in ingest_health["reason"]
    assert secret not in caplog.text


@pytest.mark.anyio
async def test_rtmps_publish_is_scoped_and_operator_switch_closes_it(rtmps_env, monkeypatch):
    client, event = rtmps_env["client"], rtmps_env["event"]
    room, other = rtmps_env["rooms"]
    secret = await enable_rtmps(client, event, room)
    whip_secret = (await client.post(ingest_url(event, other), json={"action": "enable"})).json()["secret"]

    async def auth(payload):
        return (await client.post("/internal/media-auth", json=payload)).status_code

    rejected = [
        rtmps_publish(other, secret, path=f"rtmps-test/{other.id}/floor"),  # wrong room
        rtmps_publish(room, ""),  # anonymous
        rtmps_publish(room, secret, protocol="webrtc", query="", token=secret),  # WHIP replay of the key
        rtmps_publish(other, whip_secret),  # WHIP-mode room never accepts RTMP
        rtmps_publish(room, secret, protocol="srt"),
    ]
    for payload in rejected:
        assert await auth(payload) == 401, payload
    assert await auth(rtmps_publish(room, secret)) == 204
    # Switching the room back to WHIP retires the RTMPS key.
    ingest.health.clear()
    whip_again = (await client.post(ingest_url(event, room), json={"action": "enable", "mode": "whip"})).json()
    assert "stream_key" not in whip_again
    assert await auth(rtmps_publish(room, secret)) == 401
    assert await auth(rtmps_publish(room, whip_again["secret"])) == 401
    # The operator switch closes RTMPS even for rooms already in rtmps mode.
    fresh = await enable_rtmps(client, event, room)
    ingest.health.clear()
    monkeypatch.setattr(settings, "program_rtmps_enabled", False)
    assert await auth(rtmps_publish(room, fresh)) == 401
    assert json.dumps(rtmps_env["calls"]).count("program-relay") == 0


async def connect_live(env, room, secret):
    auth = rtmps_publish(room, secret)
    assert (await env["client"].post("/internal/media-auth", json=auth)).status_code == 204
    live_feed(env, room, auth["id"])
    return auth


async def reconcile_live_feed(env, room) -> list[str]:
    """Reconcile one room against its live feed; return the sessions that were kicked."""
    kicked: list[str] = []
    orig_kick = ingest.kick

    async def spy_kick(path, source):
        if source is not None:
            kicked.append(source.id)
        return await orig_kick(path, source)

    path = ingest.MediaPath.model_validate(env["media"][f"rtmps-test/{room.id}/floor"])
    with mock.patch.object(ingest, "kick", spy_kick):
        async with get_session() as session:
            stored = await session.get(Room, room.id)
            stored_event = await session.get(Event, env["event"].id)
            await ingest.reconcile_room(stored, stored_event, path)
    return kicked


@pytest.mark.anyio
async def test_switching_rtmps_off_disconnects_a_live_feed(rtmps_env, monkeypatch):
    env = rtmps_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    auth = await connect_live(env, room, await enable_rtmps(client, event, room))
    assert await reconcile_live_feed(env, room) == []  # Live and permitted: kept.
    assert ingest.health[room.id].ingest_state == "connected"
    monkeypatch.setattr(settings, "program_rtmps_enabled", False)
    assert auth["id"] in await reconcile_live_feed(env, room)
    assert ingest.health[room.id].ingest_state == "disconnected"
    assert "switched off" in ingest.health[room.id].ingest_reason


@pytest.mark.anyio
async def test_deleted_event_stops_and_refuses_rtmps(rtmps_env):
    env = rtmps_env
    client, event, room = env["client"], env["event"], env["rooms"][0]
    secret = await enable_rtmps(client, event, room)
    auth = await connect_live(env, room, secret)
    assert await reconcile_live_feed(env, room) == []
    async with get_session() as session:
        (await session.get(Event, event.id)).deleted_at = ingest.utc_now()
    assert auth["id"] in await reconcile_live_feed(env, room)
    ingest.health.clear()
    response = await client.post("/internal/media-auth", json=rtmps_publish(room, secret))
    assert response.status_code == 401
