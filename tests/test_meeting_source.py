from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from fastapi_app import app
from portal import meeting_source as meeting
from portal import program_ingest as ingest
from portal.auth import create_user_token
from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.config import settings
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import Event, Room

SECRET_URL = "https://meet.example.org/TrackA?jwt=meeting-secret-token"


class FakeAgents:
    """Floor-bot double: one process per room key, replaced on every start."""

    def __init__(self):
        self.rooms: dict[str, dict] = {}
        self.starts: list[dict] = []
        self.stops: list[str] = []
        self.reachable = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.reachable:
            raise httpx.ConnectError("floor-bot down", request=request)
        if request.url.path == "/status":
            return httpx.Response(200, json={"active_rooms": self.rooms})
        body = json.loads(request.content)
        key = f"{body['event_slug']}-{body['room_id']}"
        if request.url.path == "/start":
            self.starts.append(body)
            self.rooms[key] = {
                "event_slug": body["event_slug"],
                "room_id": body["room_id"],
                "state": "healthy",
                "stage": "launching",
            }
        elif request.url.path == "/stop":
            self.stops.append(key)
            self.rooms.pop(key, None)
        return httpx.Response(200, json={})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def env(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(settings, "debug", True)
    monkeypatch.setattr(settings, "program_ingest_enabled", True)
    monkeypatch.setattr(settings, "meeting_agent_max_restarts", 2)
    monkeypatch.setattr(settings, "meeting_agent_no_audio_secs", 30.0)
    meeting.health.clear()
    monkeypatch.setattr(meeting, "_swept", False)
    ingest.health.clear()
    ingest.room_locks.clear()
    clock = Clock()
    monkeypatch.setattr(meeting, "monotonic", clock)
    agents = FakeAgents()
    media: dict[str, dict] = {}

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "floor-bot":
            return agents.handle(request)
        if "/paths/list" in request.url.path:
            return httpx.Response(200, json={"items": list(media.values())})
        if "/paths/get/" in request.url.path:
            name = request.url.path.split("/paths/get/")[1]
            return httpx.Response(200, json=media[name]) if name in media else httpx.Response(404)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as control:
        monkeypatch.setattr(meeting, "get_http_client", lambda: control)
        monkeypatch.setattr(ingest, "get_http_client", lambda: control)
        start, stop = AsyncMock(), AsyncMock()

        async def started(**kwargs):
            meeting.audio_progress[kwargs["booth_id"]] = clock.now

        start.side_effect = started
        monkeypatch.setattr(meeting, "start_transcription_worker", start)
        monkeypatch.setattr(meeting, "stop_transcription_worker", stop)
        monkeypatch.setattr(ingest, "stop_transcription_worker", AsyncMock())
        async with get_session() as session:
            owner = await create_user(
                session, email="meeting-owner@example.com", display_name="Owner", email_verified=True, is_admin=True
            )
            event = Event(slug="meet-test", display_name="Meeting Test")
            session.add(event)
            await session.flush()
            room = Room(
                event_id=event.id,
                display_name="Track A",
                floor_transcription_enabled=True,
                floor_transcription_provider="local",
                floor_language_code="en",
            )
            session.add(room)
            await session.flush()
            event_id, room_id = event.id, room.id
        token = create_user_token(user_id=owner.id, email=owner.email, is_admin=True)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test", cookies={"user_token": token}
        ) as client:
            yield {
                "client": client,
                "agents": agents,
                "media": media,
                "clock": clock,
                "start": start,
                "stop": stop,
                "url": f"/workspace/events/{event_id}/rooms/{room_id}/meeting-source",
                "event_id": event_id,
                "room_id": room_id,
                "key": f"meet-test-{room_id}",
                "path": make_mediamtx_path("meet-test", room_id, "floor"),
                "booth_id": make_booth_id("meet-test", room_id, "floor"),
            }
    await dispose()


async def room_row(room_id: int) -> Room:
    async with get_session() as session:
        return await session.get(Room, room_id)


def publish(env, received: int) -> None:
    env["media"][env["path"]] = {
        "name": env["path"],
        "ready": True,
        "source": {"type": "rtspSession", "id": "8c5f3e1a-1111-4b8e-9b9e-123456789abc"},
        "tracks": ["Opus"],
        "bytesReceived": received,
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("meet.example.org/TrackA", "starts with https://"),
        ("ftp://meet.example.org/TrackA", "starts with https://"),
        ("https://user:pw@meet.example.org/TrackA", "username and password"),
        ("https://meet.example.org/TrackA#config.x=1", "'#' part"),
        ("https://meet.example.org/", "end with the Jitsi room name"),
        ("https://meet.example.org/a/b/c", "end with the Jitsi room name"),
    ],
)
async def test_invalid_meeting_url_is_rejected_without_starting_agent(env, url, message):
    response = await env["client"].post(env["url"], json={"action": "connect", "meeting_url": url})
    assert response.status_code == 422
    assert message in response.json()["detail"]
    assert env["agents"].starts == []
    assert not (await room_row(env["room_id"])).meeting_agent_enabled


@pytest.mark.anyio
async def test_connect_full_lifecycle_and_disconnect(env):
    response = await env["client"].post(
        env["url"], json={"action": "connect", "meeting_url": "https://meet.example.org/TrackA"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "starting"
    room = await room_row(env["room_id"])
    assert room.meeting_agent_enabled and room.jitsi_url == "https://meet.example.org/TrackA"
    assert len(env["agents"].starts) == 1

    await meeting.reconcile_once()
    assert meeting.health[env["room_id"]].state == "starting"

    env["agents"].rooms[env["key"]]["stage"] = "in_meeting"
    await meeting.reconcile_once()
    assert meeting.health[env["room_id"]].state == "joined"
    env["start"].assert_not_awaited()

    publish(env, 100)
    await meeting.reconcile_once()
    assert meeting.health[env["room_id"]].state == "processing"
    env["start"].assert_awaited_once()
    assert env["start"].await_args.kwargs["booth_id"] == env["booth_id"]

    # Steady state never adds a second participant or worker.
    for received in (200, 300, 400):
        env["clock"].now += 2
        publish(env, received)
        await meeting.reconcile_once()
    assert len(env["agents"].starts) == 1
    assert {call.kwargs["booth_id"] for call in env["start"].await_args_list} == {env["booth_id"]}

    response = await env["client"].post(env["url"], json={"action": "disconnect"})
    assert response.status_code == 200
    assert response.json()["state"] == "stopped"
    assert env["agents"].stops == [env["key"]]
    env["stop"].assert_awaited_with(env["booth_id"])
    assert not (await room_row(env["room_id"])).meeting_agent_enabled


@pytest.mark.anyio
async def test_repeated_connect_does_not_duplicate_participant(env):
    for _ in range(3):
        response = await env["client"].post(env["url"], json={"action": "connect"})
        assert response.status_code == 200
    assert len(env["agents"].starts) == 1


@pytest.mark.anyio
async def test_connect_requires_confirmation_to_replace_program_ingest(env):
    async with get_session() as session:
        room = await session.get(Room, env["room_id"])
        room.floor_source = "program_ingest"
        room.program_key_hash = "a" * 64

    response = await env["client"].post(env["url"], json={"action": "connect"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "source_conflict"
    assert env["agents"].starts == []
    assert (await room_row(env["room_id"])).floor_source == "program_ingest"

    response = await env["client"].post(env["url"], json={"action": "connect", "confirm_source_change": True})
    assert response.status_code == 200, response.text
    room = await room_row(env["room_id"])
    assert room.floor_source == "jitsi_bot"
    assert room.program_key_hash is None
    assert room.meeting_agent_enabled
    assert len(env["agents"].starts) == 1


@pytest.mark.anyio
async def test_enabling_program_ingest_releases_the_meeting_agent(env):
    await env["client"].post(env["url"], json={"action": "connect"})
    response = await env["client"].post(
        f"/workspace/events/{env['event_id']}/rooms/{env['room_id']}/program-ingest", json={"action": "enable"}
    )
    assert response.status_code == 200, response.text
    room = await room_row(env["room_id"])
    assert room.floor_source == "program_ingest" and not room.meeting_agent_enabled
    assert env["key"] in env["agents"].stops
    status = (await env["client"].get(env["url"])).json()
    assert status["state"] == "stopped" and status["floor_source"] == "program_ingest"


@pytest.mark.anyio
async def test_browser_sender_takeover_releases_the_meeting_agent(env):
    await env["client"].post(env["url"], json={"action": "connect"})
    response = await env["client"].post(
        f"/admin/events/{env['event_id']}/rooms/{env['room_id']}/program-sender",
        json={"sync_offset_ms": 0},
    )
    assert response.status_code == 200, response.text
    room = await room_row(env["room_id"])
    assert room.floor_source == "program_ingest" and not room.meeting_agent_enabled
    assert env["key"] in env["agents"].stops

    # The reconcile loop must not bring the agent back while the sender owns the floor.
    for _ in range(3):
        env["clock"].now += 60
        await meeting.reconcile_once()
    assert len(env["agents"].starts) == 1
    assert env["room_id"] not in meeting.health


@pytest.mark.anyio
async def test_unreachable_agent_service_leaves_floor_unchanged(env):
    async with get_session() as session:
        room = await session.get(Room, env["room_id"])
        room.floor_source = "program_ingest"
        room.program_key_hash = "a" * 64
    env["agents"].reachable = False
    response = await env["client"].post(env["url"], json={"action": "connect", "confirm_source_change": True})
    assert response.status_code == 502
    assert "floor-bot service" in response.json()["detail"]
    room = await room_row(env["room_id"])
    assert room.floor_source == "program_ingest" and room.program_key_hash == "a" * 64
    assert not room.meeting_agent_enabled


@pytest.mark.anyio
async def test_failed_agent_reconnects_within_bounded_policy(env):
    await env["client"].post(env["url"], json={"action": "connect"})
    for attempt in range(1, 3):
        env["agents"].rooms[env["key"]] = {"state": "dead", "stage": "dead", "failure": "join_failed"}
        await meeting.reconcile_once()
        current = meeting.health[env["room_id"]]
        assert current.state == "reconnecting"
        assert "could not join the meeting" in current.reason
        assert len(env["agents"].starts) == attempt  # waits for the backoff first
        env["clock"].now += 60
        await meeting.reconcile_once()
        assert len(env["agents"].starts) == attempt + 1

    env["agents"].rooms[env["key"]] = {"state": "dead", "stage": "dead", "failure": "join_failed"}
    for _ in range(3):
        env["clock"].now += 60
        await meeting.reconcile_once()
    current = meeting.health[env["room_id"]]
    assert current.state == "degraded"
    assert "Automatic reconnects stopped after 2 attempts" in current.reason
    assert len(env["agents"].starts) == 3
    env["start"].assert_not_awaited()

    # The owner reconnecting resets the incident budget.
    await env["client"].post(env["url"], json={"action": "connect"})
    assert len(env["agents"].starts) == 4
    assert meeting.health[env["room_id"]].restarts == 0


@pytest.mark.anyio
async def test_joined_without_audio_restarts_agent(env):
    await env["client"].post(env["url"], json={"action": "connect"})
    env["agents"].rooms[env["key"]]["stage"] = "in_meeting"
    await meeting.reconcile_once()
    env["clock"].now += 31
    await meeting.reconcile_once()
    assert env["key"] in env["agents"].stops
    current = meeting.health[env["room_id"]]
    assert current.state == "reconnecting"
    assert "no meeting audio reached" in current.reason


@pytest.mark.anyio
async def test_pipeline_settings_problem_is_actionable(env):
    async with get_session() as session:
        room = await session.get(Room, env["room_id"])
        room.floor_language_code = None
    await env["client"].post(env["url"], json={"action": "connect"})
    env["agents"].rooms[env["key"]]["stage"] = "in_meeting"
    publish(env, 100)
    await meeting.reconcile_once()
    current = meeting.health[env["room_id"]]
    assert current.state == "degraded"
    assert "floor source language" in current.reason
    env["start"].assert_not_awaited()


@pytest.mark.anyio
async def test_restart_reconciliation_adopts_desired_and_stops_stale_agents(env):
    async with get_session() as session:
        room = await session.get(Room, env["room_id"])
        room.meeting_agent_enabled = True
        event = (await session.execute(select(Event).where(Event.slug == "meet-test"))).scalar_one()
        other = Room(event_id=event.id, display_name="Track B")
        session.add(other)
        await session.flush()
        stale_key = f"meet-test-{other.id}"
    # A portal restart loses in-memory health; the bot still runs both agents.
    env["agents"].rooms[env["key"]] = {
        "event_slug": "meet-test",
        "room_id": env["room_id"],
        "state": "healthy",
        "stage": "in_meeting",
    }
    env["agents"].rooms[stale_key] = {
        "event_slug": "meet-test",
        "room_id": other.id,
        "state": "healthy",
        "stage": "in_meeting",
    }
    publish(env, 100)

    await meeting.reconcile_once()

    assert env["agents"].stops == [stale_key]
    assert env["agents"].starts == []  # adopted, not rejoined
    assert meeting.health[env["room_id"]].state == "processing"
    env["start"].assert_awaited_once()


@pytest.mark.anyio
async def test_restart_reconciliation_restarts_missing_desired_agent(env):
    async with get_session() as session:
        room = await session.get(Room, env["room_id"])
        room.meeting_agent_enabled = True
    await meeting.reconcile_once()
    assert meeting.health[env["room_id"]].state == "reconnecting"
    env["clock"].now += 60
    await meeting.reconcile_once()
    assert len(env["agents"].starts) == 1


@pytest.mark.anyio
async def test_failed_disconnect_is_cleaned_up_by_reconcile(env):
    await env["client"].post(env["url"], json={"action": "connect"})
    env["agents"].reachable = False
    response = await env["client"].post(env["url"], json={"action": "disconnect"})
    assert response.status_code == 202
    assert response.json()["cleanup_pending"] is True
    assert not (await room_row(env["room_id"])).meeting_agent_enabled

    env["agents"].reachable = True
    await meeting.reconcile_once()
    assert env["key"] in env["agents"].stops
    assert env["agents"].rooms == {}


@pytest.mark.anyio
async def test_meeting_url_absent_from_status_and_logs(env, caplog):
    # Scope to VoxBento loggers: the SQLite driver echoes bound parameters at DEBUG.
    caplog.set_level(logging.DEBUG, logger="portal")
    response = await env["client"].post(env["url"], json={"action": "connect", "meeting_url": SECRET_URL})
    assert response.status_code == 200
    env["agents"].rooms[env["key"]] = {"state": "dead", "stage": "dead", "failure": "not_jitsi"}
    await meeting.reconcile_once()
    status = await env["client"].get(env["url"])
    legacy = await env["client"].get(f"/api/rooms/{env['room_id']}/floor-transcription/status")
    for body in (response.text, status.text, legacy.text, caplog.text):
        assert "meeting-secret-token" not in body
        assert "meet.example.org" not in body
    # The agent itself still receives the real URL over the private control channel.
    assert env["agents"].starts[0]["jitsi_url"] == SECRET_URL


@pytest.mark.anyio
async def test_owner_endpoints_reject_cross_origin_and_wrong_event(env):
    response = await env["client"].post(
        env["url"], json={"action": "connect"}, headers={"origin": "https://evil.example"}
    )
    assert response.status_code == 403
    wrong = f"/workspace/events/{env['event_id'] + 99}/rooms/{env['room_id']}/meeting-source"
    assert (await env["client"].get(wrong)).status_code in {403, 404}
    assert env["agents"].starts == []
