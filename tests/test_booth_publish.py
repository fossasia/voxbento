"""Interpreter booth WHIP publishing requires a single-use ticket (#783).

The session a ticket opens is ended by the portal when its interpreter leaves the booth (#1285).
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from fastapi_app import app
from portal import booth_publish
from portal import program_ingest as ingest
from portal.config import settings
from portal.globals import booths
from portal.websockets import manager as manager_module
from portal.websockets.manager import (
    Session,
    _handle_join,
    _handle_leave,
    _handle_request_publish_ticket,
    _handle_set_broadcast_unlocked,
    manager,
    release_participant,
    schedule_participant_expiry,
)

EVENT = "ticket-test"
ROOM = 7
BOOTH_ID = f"{EVENT}-{ROOM}-en"
PATH = f"{EVENT}/{ROOM}/en"


class MockWebSocket:
    """Captures JSON frames sent by a handler."""

    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


@pytest.fixture
async def booth(monkeypatch):
    """Two interpreters in one booth; Mission Control has broadcast unlocked."""
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", "")
    monkeypatch.setattr(settings, "debug", True)
    booth_publish.clear_tickets()
    ingest.failures.clear()
    joined = []
    for name in ("Active", "Backup"):
        participant, _ = await booths.join_participant(
            booth_id=BOOTH_ID,
            display_name=name,
            role="interpreter",
            language="English",
            channel_id=PATH,
            room_id=ROOM,
            connection_id=f"{name}-socket",
        )
        joined.append(participant.participant_id)
    booths.get_booth_sync(BOOTH_ID).broadcast_unlocked = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="http://test"
    ) as client:
        yield client, joined
    await booths.remove_booth(EVENT, ROOM, "en")
    booth_publish.clear_tickets()
    ingest.failures.clear()


def session_for(participant_id: str | None, **fields) -> Session:
    """A booth WS session bound to *participant_id*, as after ``booth:join``."""
    return Session(booth_id=BOOTH_ID, participant_id=participant_id, language="English", channel_id=PATH, **fields)


async def request_ticket(participant_id: str | None, **session_overrides) -> dict:
    """Send ``booth:request-publish-ticket`` and return the single reply."""
    ws = MockWebSocket()
    session = session_for(participant_id)
    for key, value in session_overrides.items():
        setattr(session, key, value)
    await _handle_request_publish_ticket(ws, session, {})
    [reply] = ws.sent
    assert reply["type"] == "booth:publish-ticket"
    return reply


def hook_payload(token: str = "", path: str = PATH, **overrides) -> dict:
    """The JSON MediaMTX 1.18 posts to the auth hook for a WHIP publish."""
    return {
        "action": "publish",
        "path": path,
        "protocol": "webrtc",
        "token": token,
        "id": str(uuid4()),
        "ip": "198.51.100.9",
        **overrides,
    }


async def publish(client, **kwargs) -> int:
    """Call the hook and return its status code."""
    return (await client.post("/internal/media-auth", json=hook_payload(**kwargs))).status_code


@pytest.mark.anyio
async def test_anonymous_booth_publish_is_rejected(booth):
    """Without a ticket MediaMTX is told to refuse the publisher."""
    client, _ = booth
    assert await publish(client) == 401
    assert await publish(client, token="guessed-ticket") == 401


@pytest.mark.anyio
async def test_active_interpreter_ticket_publishes_once(booth):
    """The active interpreter's ticket is accepted exactly once."""
    client, (active, _) = booth
    reply = await request_ticket(active)
    assert reply["expires_in"] == int(booth_publish.TICKET_TTL_SECS)

    assert await publish(client, token=reply["ticket"]) == 204
    # Replaying the same ticket (e.g. an attacker who sniffed it) is refused.
    assert await publish(client, token=reply["ticket"]) == 401


@pytest.mark.anyio
async def test_backup_interpreter_and_unjoined_socket_get_no_ticket(booth):
    """Only the active interpreter can obtain a ticket."""
    _, (_, backup) = booth
    assert "ticket" not in await request_ticket(backup)
    assert (await request_ticket(None))["error"] == "Join the booth before going live."


@pytest.mark.anyio
async def test_ticket_is_bound_to_server_path_not_client_channel(booth):
    """The ticket path comes from booth_id, never from the client's channel_id."""
    client, (active, _) = booth
    # A client-supplied channel_id at join must not redirect the ticket.
    reply = await request_ticket(active, channel_id="other-event/1/en")
    assert await publish(client, token=reply["ticket"], path="other-event/1/en") == 401
    # The mismatched attempt spent the ticket.
    assert await publish(client, token=reply["ticket"]) == 401


@pytest.mark.anyio
async def test_handoff_revokes_an_unused_ticket(booth):
    """A handoff before the WHIP offer arrives makes the old ticket useless."""
    client, (active, backup) = booth
    reply = await request_ticket(active)
    await booths.set_active_interpreter(BOOTH_ID, active, backup, "English", PATH)
    assert await publish(client, token=reply["ticket"]) == 401


@pytest.mark.anyio
async def test_expired_ticket_is_rejected(booth, monkeypatch):
    """Tickets stop working after TICKET_TTL_SECS."""
    client, (active, _) = booth
    reply = await request_ticket(active)
    clock = booth_publish.time.monotonic() + booth_publish.TICKET_TTL_SECS + 1
    monkeypatch.setattr(booth_publish.time, "monotonic", lambda: clock)
    assert await publish(client, token=reply["ticket"]) == 401


@pytest.mark.anyio
async def test_booth_paths_refuse_non_webrtc_publishers(booth):
    """Booth paths are browser WHIP only; RTSP is refused even with a ticket."""
    client, (active, _) = booth
    reply = await request_ticket(active)
    assert await publish(client, token=reply["ticket"], protocol="rtsp", ip="172.18.0.2") == 401


@pytest.mark.anyio
async def test_failed_booth_publishes_are_throttled(booth):
    """Booth failures share the floor's per-IP throttle."""
    client, _ = booth
    for _ in range(10):
        assert await publish(client) == 401
    assert await publish(client) == 429


def test_ticket_store_is_bounded(monkeypatch):
    """The in-memory store evicts the oldest tickets beyond MAX_TICKETS."""
    booth_publish.clear_tickets()
    monkeypatch.setattr(booth_publish, "MAX_TICKETS", 3)
    secrets = [booth_publish.issue_ticket(BOOTH_ID, "p", PATH) for _ in range(5)]
    assert len(booth_publish._tickets) == 3
    assert booth_publish.redeem_ticket(secrets[0], PATH) is None
    assert booth_publish.redeem_ticket(secrets[-1], PATH) is not None
    booth_publish.clear_tickets()


@pytest.mark.anyio
async def test_locked_broadcast_issues_no_ticket(booth):
    """Mission Control's Stop is enforced by the server, not only by the browser."""
    _, (active, _) = booth
    booths.get_booth_sync(BOOTH_ID).broadcast_unlocked = False
    assert (await request_ticket(active))["error"] == "Broadcast is stopped by Mission Control."


@pytest.mark.anyio
async def test_lock_after_issue_revokes_the_ticket(booth):
    """A Stop between issuing and redeeming the ticket refuses the publish."""
    client, (active, _) = booth
    reply = await request_ticket(active)
    await booths.set_broadcast_unlocked(BOOTH_ID, False, "English", PATH)
    assert await publish(client, token=reply["ticket"]) == 401


@pytest.mark.anyio
async def test_join_cannot_take_over_another_participants_id(booth):
    """Naming the active interpreter's ID at join yields a fresh ID, not their record or tickets."""
    _, (active, _) = booth
    ws = MockWebSocket()
    intruder = Session(
        booth_id=BOOTH_ID, participant_id=None, language="English", channel_id=PATH, granted_role="interpreter"
    )
    await _handle_join(ws, intruder, {"role": "interpreter", "display_name": "Intruder", "participant_id": active})
    joined = next(m for m in ws.sent if m["type"] == "booth:joined")
    assert joined["participant_id"] != active
    assert intruder.participant_id == joined["participant_id"]
    assert booths.get_booth_sync(BOOTH_ID).participants[active].display_name == "Active"
    assert "ticket" not in await request_ticket(intruder.participant_id)


@pytest.mark.anyio
async def test_socket_can_rejoin_with_its_own_id(booth):
    """A socket re-sending booth:join keeps the ID it already holds."""
    _, (active, _) = booth
    ws = MockWebSocket()
    own = Session(
        booth_id=BOOTH_ID, participant_id=active, language="English", channel_id=PATH, granted_role="interpreter"
    )
    await _handle_join(ws, own, {"role": "interpreter", "display_name": "Active", "participant_id": active})
    assert next(m for m in ws.sent if m["type"] == "booth:joined")["participant_id"] == active


@pytest.mark.anyio
async def test_reconnected_socket_rejoins_with_its_previous_id(booth):
    """After its socket drops, an interpreter's new socket gets its ID back (the booth page relies on this)."""
    _, (active, backup) = booth
    await booths.leave_participant(BOOTH_ID, active, "English", PATH)
    assert booths.get_booth_sync(BOOTH_ID).active_interpreter_id == backup

    ws = MockWebSocket()
    reconnected = Session(
        booth_id=BOOTH_ID, participant_id=None, language="English", channel_id=PATH, granted_role="interpreter"
    )
    await _handle_join(ws, reconnected, {"role": "interpreter", "display_name": "Active", "participant_id": active})
    joined = next(m for m in ws.sent if m["type"] == "booth:joined")
    assert joined["participant_id"] == active
    # The backup took over while the socket was down; rejoining does not take the booth back.
    assert joined["state"]["active_interpreter_id"] == backup
    assert "ticket" not in await request_ticket(active)


# ── Ending the WHIP session when its interpreter leaves (#1285) ──────────────


@pytest.fixture
async def media(booth, monkeypatch):
    """The booth with a fake MediaMTX control API: ``paths`` it serves and ``kicks`` it received."""
    paths: dict[str, dict] = {}
    kicks: list[str] = []
    failure: list[Exception] = []

    def control(request: httpx.Request) -> httpx.Response:
        if failure:
            raise failure[0]
        url = request.url.path
        if "/paths/get/" in url:
            name = url.split("/paths/get/")[1]
            return httpx.Response(200, json=paths[name]) if name in paths else httpx.Response(404)
        session_id = url.rsplit("/", 1)[-1]
        assert request.method == "POST" and url == f"/v3/webrtcsessions/kick/{session_id}"
        kicks.append(session_id)
        live = paths.get(PATH)
        if not live or live["source"]["id"] != session_id:
            return httpx.Response(404)
        del paths[PATH]
        return httpx.Response(200, json={})

    async def broadcast(_booth_id, _state):
        return None

    monkeypatch.setattr(manager_module, "broadcast_booth_state", broadcast)
    async with httpx.AsyncClient(transport=httpx.MockTransport(control)) as media_client:
        monkeypatch.setattr(ingest, "get_http_client", lambda: media_client)
        yield *booth, paths, kicks, failure


async def go_live(client, participant_id: str, paths: dict) -> str:
    """Publish as *participant_id* and return the MediaMTX session it opened."""
    session_id = str(uuid4())
    reply = await request_ticket(participant_id)
    assert await publish(client, token=reply["ticket"], id=session_id) == 204
    paths[PATH] = {"name": PATH, "ready": True, "source": {"type": "webRTCSession", "id": session_id}}
    return session_id


def participant_ids() -> list[str]:
    return list(booths.get_booth_sync(BOOTH_ID).participants)


def socket_session(participant_id: str) -> Session:
    """The session of the socket that currently holds *participant_id*."""
    record = booths.get_booth_sync(BOOTH_ID).participants[participant_id]
    return session_for(participant_id, connection_id=record.connection_id)


@pytest.mark.anyio
async def test_revoked_live_interpreter_has_their_session_kicked(media):
    """Closing a revoked socket ends the audio it was sending, without the browser's help."""
    client, (active, backup), paths, kicks, _ = media
    session_id = await go_live(client, active, paths)

    await release_participant(socket_session(active), immediate=True)

    # The backup is promoted but is not live: the kicked session is the revoked interpreter's.
    assert booths.get_booth_sync(BOOTH_ID).active_interpreter_id == backup
    assert kicks == [session_id]
    assert PATH not in paths


@pytest.mark.anyio
@pytest.mark.parametrize("identity", ["invite", "user"])
async def test_revoking_access_kicks_a_publisher_whose_socket_had_dropped(media, identity):
    """Invite revocation and membership removal reach an interpreter kept for the reconnect grace."""
    client, _, paths, kicks, _ = media
    for participant_id in participant_ids():
        await booths.leave_participant(BOOTH_ID, participant_id, "English", PATH)
    who = {"invite_public_id": "inv-live"} if identity == "invite" else {"user_id": 71}
    live, _ = await booths.join_participant(
        BOOTH_ID, "Live", "interpreter", "English", PATH, room_id=ROOM, connection_id="live-socket", **who
    )
    session_id = await go_live(client, live.participant_id, paths)
    await booths.disconnect_participant(BOOTH_ID, live.participant_id, "live-socket")
    assert kicks == []

    if identity == "invite":
        await manager.close_token_sessions("inv-live", booth_id=BOOTH_ID)
    else:
        await manager.close_user_sessions(71, booth_id=BOOTH_ID)

    assert participant_ids() == []
    assert kicks == [session_id]


@pytest.mark.anyio
async def test_newer_publisher_survives_the_previous_one_being_revoked(media):
    """After a handoff the path belongs to the new interpreter; removing the old one kicks nothing."""
    client, (active, backup), paths, kicks, _ = media
    await go_live(client, active, paths)
    await booths.set_active_interpreter(BOOTH_ID, active, backup, "English", PATH)
    newer = await go_live(client, backup, paths)

    await release_participant(socket_session(active), immediate=True)

    assert kicks == []
    assert paths[PATH]["source"]["id"] == newer


@pytest.mark.anyio
async def test_dropped_socket_keeps_its_audio_until_the_grace_runs_out(media, monkeypatch):
    """A socket that merely drops keeps publishing; only running out of grace ends the session."""
    client, (active, _), paths, kicks, _ = media
    monkeypatch.setattr(settings, "booth_reconnect_grace_seconds", 30.0)
    scheduled = []
    monkeypatch.setattr(manager_module, "schedule_participant_expiry", lambda *args: scheduled.append(args))
    session_id = await go_live(client, active, paths)

    await release_participant(socket_session(active), immediate=False)

    assert active in participant_ids()
    assert kicks == []
    assert scheduled == [(BOOTH_ID, active, "Active-socket", 30.0)]

    schedule_participant_expiry(BOOTH_ID, active, "Active-socket", 0)
    for _ in range(100):
        if kicks:
            break
        await asyncio.sleep(0.01)
    assert active not in participant_ids()
    assert kicks == [session_id]


@pytest.mark.anyio
async def test_leaving_the_booth_ends_the_session(media):
    """An interpreter who leaves is no longer in the booth, so their audio stops with them."""
    client, (active, _), paths, kicks, _ = media
    session_id = await go_live(client, active, paths)
    await _handle_leave(socket_session(active))
    assert kicks == [session_id]


@pytest.mark.anyio
@pytest.mark.parametrize("recorded", [True, False])
async def test_stop_kicks_the_booth_path_source(media, recorded):
    """Mission Control's Stop ends the path's publisher, recorded or not (e.g. after a portal restart)."""
    client, (active, _), paths, kicks, _ = media
    session_id = await go_live(client, active, paths)
    if not recorded:
        booth_publish._publishers.clear()

    ws = MockWebSocket()
    await _handle_set_broadcast_unlocked(ws, session_for(None, granted_role="room_coordinator"), {"unlocked": False})

    assert ws.sent == []
    assert booths.get_booth_sync(BOOTH_ID).broadcast_unlocked is False
    assert kicks == [session_id]
    assert PATH not in paths


@pytest.mark.anyio
async def test_deleting_a_booth_kicks_its_path_source(media):
    """Closing a deleted booth's sessions ends its publisher even with no socket left to close."""
    client, (active, _), paths, kicks, _ = media
    session_id = await go_live(client, active, paths)
    await manager.close_booth_sessions(BOOTH_ID)
    assert kicks == [session_id]


@pytest.mark.anyio
async def test_session_that_already_ended_counts_as_kicked(media, caplog):
    """MediaMTX answering 404 for a session that is gone is not an error."""
    client, (active, _), paths, kicks, _ = media
    session_id = await go_live(client, active, paths)
    del paths[PATH]

    with caplog.at_level("WARNING", logger="portal.booth_publish"):
        await release_participant(socket_session(active), immediate=True)

    assert kicks == [session_id]
    assert caplog.records == []


@pytest.mark.anyio
async def test_unreachable_mediamtx_does_not_block_revocation_or_stop(media, caplog):
    """The kick is best effort: the booth still changes, and the log names no session."""
    client, (active, backup), paths, kicks, failure = media
    session_id = await go_live(client, active, paths)
    failure.append(httpx.ConnectError("media down"))

    with caplog.at_level("WARNING", logger="portal.booth_publish"):
        await release_participant(socket_session(active), immediate=True)
        ws = MockWebSocket()
        await _handle_set_broadcast_unlocked(
            ws, session_for(None, granted_role="room_coordinator"), {"unlocked": False}
        )

    assert participant_ids() == [backup]
    assert booths.get_booth_sync(BOOTH_ID).broadcast_unlocked is False
    assert ws.sent == []
    assert kicks == []
    assert len(caplog.records) == 2
    assert session_id not in caplog.text


@pytest.mark.anyio
async def test_hook_without_a_session_id_records_no_publisher(booth):
    """The ticket still decides the publish; there is just no session to end later."""
    client, (active, _) = booth
    reply = await request_ticket(active)
    assert await publish(client, token=reply["ticket"], id="") == 204
    assert booth_publish._publishers == {}
