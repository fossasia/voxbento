"""Interpreter booth WHIP publishing requires a single-use ticket (#783)."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from fastapi_app import app
from portal import booth_publish
from portal import program_ingest as ingest
from portal.config import settings
from portal.globals import booths
from portal.websockets.manager import Session, _handle_join, _handle_request_publish_ticket

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
            booth_id=BOOTH_ID, display_name=name, role="interpreter", language="English", channel_id=PATH, room_id=ROOM
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


def session_for(participant_id: str | None) -> Session:
    """A booth WS session bound to *participant_id*, as after ``booth:join``."""
    return Session(booth_id=BOOTH_ID, participant_id=participant_id, language="English", channel_id=PATH)


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
