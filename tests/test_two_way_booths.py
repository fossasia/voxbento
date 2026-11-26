"""Two-way booths (#1397): a booth can also interpret back into the room's spoken language.

A Thai booth in an English room interprets into Thai, or switches to English for a question
asked in Thai. The server picks the path the interpreter publishes to; one booth at a time
interprets into a return channel; everything tied to the booth's path follows its direction.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from fastapi_app import app
from portal import booth_publish, booth_recovery
from portal import program_ingest as ingest
from portal.attendance import is_interpreting
from portal.booth_state import BoothRegistry
from portal.config import settings
from portal.database import configure, create_booth, create_event, create_room, dispose, get_session, init_db
from portal.globals import booths
from portal.models import BoothLiveState, DBBooth, Room
from portal.room_setup import (
    ReturnLanguageInUse,
    booth_return_language,
    drop_mismatched_returns,
    return_language_error,
    two_way_language,
)
from portal.websockets import manager as manager_module
from portal.websockets.manager import (
    Session,
    _handle_join,
    _handle_request_publish_ticket,
    _handle_set_broadcast_unlocked,
    _handle_set_output,
    floor_channel_state,
    listener_manager,
    manager,
    release_participant,
    schedule_participant_expiry,
    send_channel_state,
)

EVENT = "twoway"


class MockWebSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


# ── Validation ───────────────────────────────────────────────────────────────


def test_two_way_needs_a_real_spoken_language():
    assert two_way_language("en") == "en"
    assert two_way_language(" EN ") == "en"
    for spoken in (None, "", "floor", "english", "zz", "e1"):
        assert two_way_language(spoken) is None


@pytest.mark.parametrize(
    ("return_code", "booth", "spoken", "others", "message"),
    [
        ("en", "th", "en", ["ja"], None),
        ("en", "th", None, [], "once the room's spoken language is set"),
        ("en", "th", "floor", [], "once the room's spoken language is set"),
        ("fr", "th", "en", [], "back into the room's spoken language, English"),
        ("floor", "th", "en", [], "back into the room's spoken language, English"),
        ("en", "en", "en", [], "The English booth already interprets into English"),
        ("en", "th", "en", ["en"], "already has a English booth"),
    ],
)
def test_return_language_rules(return_code, booth, spoken, others, message):
    error = return_language_error(
        return_code, booth_language=booth, spoken_language=spoken, other_booth_languages=others
    )
    if message is None:
        assert error is None
    else:
        assert message in error


# ── Database fixtures ────────────────────────────────────────────────────────


@pytest.fixture
async def room(monkeypatch):
    """An English room with a Thai and a Japanese booth, both two-way into English."""
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", "")
    monkeypatch.setattr(settings, "debug", True)
    ensured: list[str] = []

    async def ensure(path):
        ensured.append(path)

    monkeypatch.setattr(manager_module, "_ensure_mediamtx_path", ensure)
    booth_publish.clear_tickets()
    ingest.failures.clear()
    async with get_session() as session:
        event = await create_event(session, slug=EVENT, display_name="Two Way")
        room = await create_room(session, event_id=event.id, display_name="Main")
        room.floor_language_code = "en"
        for code, name in (("th", "Thai"), ("ja", "Japanese")):
            booth = await create_booth(
                session, event_id=event.id, room_id=room.id, language_code=code, language_name=name
            )
            booth.return_language_code = "en"
        room_id = room.id
    yield Room_(room_id, ensured)
    for code in ("th", "ja", "en", "fr"):
        await booths.remove_booth(EVENT, room_id, code)
    booth_publish.clear_tickets()
    ingest.failures.clear()
    manager_module._last_channel_states.clear()
    manager_module._last_return_booths.clear()
    await dispose()


class Room_:
    def __init__(self, room_id: int, ensured: list[str]):
        self.id = room_id
        self.ensured = ensured

    def booth_id(self, code: str) -> str:
        return f"{EVENT}-{self.id}-{code}"

    def path(self, code: str) -> str:
        return f"{EVENT}/{self.id}/{code}"

    def session(self, code: str, participant_id: str | None, role: str = "interpreter", **fields) -> Session:
        return Session(
            booth_id=self.booth_id(code),
            participant_id=participant_id,
            language={"th": "Thai", "ja": "Japanese"}.get(code, "English"),
            channel_id=self.path(code),
            granted_role=role,
            **fields,
        )


async def join(room: Room_, code: str, name: str, *, role: str = "interpreter") -> str:
    participant, _ = await booths.join_participant(
        booth_id=room.booth_id(code),
        display_name=name,
        role=role,
        language={"th": "Thai", "ja": "Japanese"}[code],
        channel_id=room.path(code),
        room_id=room.id,
        connection_id=f"{name}-socket",
        joined_by=f"user:{name}",
    )
    return participant.participant_id


async def set_output(room: Room_, code: str, participant_id: str | None, output, **extra) -> list[dict]:
    ws = MockWebSocket()
    await _handle_set_output(
        ws,
        room.session(code, participant_id, **extra.pop("session", {})),
        {"type": "booth:set-output", "output": output, **extra},
    )
    return ws.sent


async def ticket(room: Room_, code: str, participant_id: str) -> dict:
    ws = MockWebSocket()
    await _handle_request_publish_ticket(ws, room.session(code, participant_id), {})
    [reply] = ws.sent
    return reply


def hook(token: str, path: str, session_id: str | None = None) -> dict:
    return {
        "action": "publish",
        "path": path,
        "protocol": "webrtc",
        "token": token,
        "id": session_id or str(uuid4()),
        "ip": "198.51.100.9",
    }


@pytest.fixture
async def media(room, monkeypatch):
    """A fake MediaMTX control API: ``paths`` it serves and the session ``kicks`` it received."""
    paths: dict[str, dict] = {}
    kicks: list[str] = []

    def control(request: httpx.Request) -> httpx.Response:
        url = request.url.path
        if "/paths/get/" in url:
            name = url.split("/paths/get/")[1]
            return httpx.Response(200, json=paths[name]) if name in paths else httpx.Response(404)
        session_id = url.rsplit("/", 1)[-1]
        assert request.method == "POST" and url == f"/v3/webrtcsessions/kick/{session_id}"
        kicks.append(session_id)
        for name, live in list(paths.items()):
            if live["source"]["id"] == session_id:
                del paths[name]
                return httpx.Response(200, json={})
        return httpx.Response(404)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(control)) as media_client,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)), base_url="http://test"
        ) as client,
    ):
        monkeypatch.setattr(ingest, "get_http_client", lambda: media_client)
        yield room, client, paths, kicks


async def go_live(room, client, paths, code, participant_id) -> str:
    """Unlock the booth and publish as its active interpreter to its current path."""
    booths.get_booth_sync(room.booth_id(code)).broadcast_unlocked = True
    reply = await ticket(room, code, participant_id)
    session_id = str(uuid4())
    response = await client.post("/internal/media-auth", json=hook(reply["ticket"], reply["path"], session_id))
    assert response.status_code == 204
    paths[reply["path"]] = {"name": reply["path"], "ready": True, "source": {"type": "webRTCSession", "id": session_id}}
    await booths.update_participant_state(
        room.booth_id(code), participant_id, "Thai", room.path(code), mic_active=True, ingest_connected=True
    )
    return session_id


# ── Switching ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_switch_while_idle_and_back(room):
    ana = await join(room, "th", "Ana")
    assert await set_output(room, "th", ana, "return") == []
    booth = booths.get_booth_sync(room.booth_id("th"))
    state = booth.as_public_dict()
    assert state["output_language_code"] == "en"
    assert state["output_mediamtx_path"] == room.path("en")
    assert state["return_language_code"] == "en"
    assert state["return_language"] == "English"
    # Existing keys keep naming the booth's own channel.
    assert state["mediamtx_path"] == room.path("th")
    assert state["language_code"] == "th"
    assert room.ensured == [room.path("en")]

    assert await set_output(room, "th", ana, "primary") == []
    assert booth.output_language_code == "th"
    assert booth.output_mediamtx_path == room.path("th")


@pytest.mark.anyio
async def test_switch_to_the_same_output_changes_nothing(room, monkeypatch):
    ana = await join(room, "th", "Ana")
    sent = []

    async def broadcast(booth_id, state):
        sent.append(state["output_language_code"])

    monkeypatch.setattr(manager_module, "broadcast_booth_state", broadcast)
    assert await set_output(room, "th", ana, "primary") == []
    await set_output(room, "th", ana, "return")
    await set_output(room, "th", ana, "return")
    assert sent == ["th", "en", "en"]
    assert room.ensured == [room.path("en")]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "message",
    [
        {"type": "booth:set-output"},
        {"type": "booth:set-output", "output": 1},
        {"type": "booth:set-output", "output": None},
        {"type": "booth:set-output", "output": "RETURN"},
        {"type": "booth:set-output", "output": ["return"]},
        {"type": "booth:set-output", "output": "return", "path": "twoway/1/en"},
    ],
)
async def test_malformed_requests_are_refused(room, message):
    ana = await join(room, "th", "Ana")
    ws = MockWebSocket()
    await _handle_set_output(ws, room.session("th", ana), message)
    assert ws.sent == [{"type": "booth:error", "message": "Send output as 'primary' or 'return'."}]
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "th"


@pytest.mark.anyio
async def test_refused_for_a_one_way_booth_and_an_unknown_one(room):
    async with get_session() as session:
        booth = await session.scalar(select(DBBooth).where(DBBooth.language_code == "ja"))
        booth.return_language_code = None
    kai = await join(room, "ja", "Kai")
    assert await set_output(room, "ja", kai, "return") == [
        {"type": "booth:error", "message": "This booth is not two-way."}
    ]
    ws = MockWebSocket()
    await _handle_set_output(
        ws,
        Session(booth_id="not-a-booth", participant_id=None, language="x", channel_id="x"),
        {"type": "booth:set-output", "output": "return"},
    )
    assert ws.sent == [{"type": "booth:error", "message": "This booth is not two-way."}]


@pytest.mark.anyio
async def test_a_failed_return_language_read_refuses_the_switch(room, monkeypatch):
    async def broken(_booth_id):
        raise SQLAlchemyError("down")

    ana = await join(room, "th", "Ana")
    monkeypatch.setattr(manager_module, "load_booth_return_language", broken)
    assert await set_output(room, "th", ana, "return") == [
        {"type": "booth:error", "message": "Could not switch the channel. Try again."}
    ]
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "th"


@pytest.mark.anyio
async def test_the_active_interpreter_of_another_booth_is_refused(room):
    ana = await join(room, "th", "Ana")
    kai = await join(room, "ja", "Kai")
    [error] = await set_output(room, "ja", ana, "return")
    assert "active interpreter" in error["message"]
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "th"
    assert booths.get_booth_sync(room.booth_id("ja")).output_language_code == "ja"
    assert kai


@pytest.mark.anyio
async def test_a_return_language_made_invalid_later_is_refused(room):
    """The switch re-checks the database, so a stale two-way setting can't open a second publisher."""
    async with get_session() as session:
        (await session.get(Room, room.id)).floor_language_code = "fr"
    ana = await join(room, "th", "Ana")
    assert await set_output(room, "th", ana, "return") == [
        {"type": "booth:error", "message": "This booth is not two-way."}
    ]
    async with get_session() as session:
        room_row = await session.get(Room, room.id)
        room_row.floor_language_code = "en"
        session.add(DBBooth(event_id=room_row.event_id, room_id=room.id, language_code="en", language_name="English"))
    assert await set_output(room, "th", ana, "return") == [
        {"type": "booth:error", "message": "This booth is not two-way."}
    ]
    async with get_session() as session:
        assert await booth_return_language(session, "no-such-event", room.id, "th") is None


@pytest.mark.anyio
async def test_only_the_active_interpreter_or_a_coordinator_may_switch(room):
    ana = await join(room, "th", "Ana")
    ben = await join(room, "th", "Ben")
    [error] = await set_output(room, "th", ben, "return")
    assert error["type"] == "booth:error" and "active interpreter" in error["message"]
    [error] = await set_output(room, "th", None, "return")
    assert "active interpreter" in error["message"]
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "th"
    # A coordinator's socket may switch without joining (Mission Control permission).
    assert await set_output(room, "th", None, "return", session={"role": "room_coordinator"}) == []
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "en"
    assert ana


@pytest.mark.anyio
async def test_switch_needs_a_booth_and_someone_interpreting(room):
    [error] = await set_output(room, "th", None, "return", session={"role": "room_coordinator"})
    assert error["message"] == "Join the booth first."
    coordinator = await join(room, "th", "Cora", role="room_coordinator")
    await booths.leave_participant(room.booth_id("th"), coordinator, "Thai", room.path("th"))
    [error] = await set_output(room, "th", None, "return", session={"role": "room_coordinator"})
    assert error["message"] == "Nobody is interpreting in this booth."


@pytest.mark.anyio
async def test_registry_rejects_an_unknown_output():
    registry = BoothRegistry()
    with pytest.raises(ValueError, match="'primary' or 'return'"):
        await registry.set_output(
            "x-1-th", None, "sideways", can_reassign=True, return_language_code="en", return_language="English"
        )


@pytest.mark.anyio
async def test_one_booth_at_a_time_into_a_return_channel(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    kai = await join(room, "ja", "Kai")
    await set_output(room, "th", ana, "return")
    thai_session = await go_live(room, client, paths, "th", ana)

    # Japanese can't take English while Thai interprets into it; nothing is kicked.
    assert await set_output(room, "ja", kai, "return") == [
        {"type": "booth:error", "message": "The Thai booth is already interpreting into English."}
    ]
    assert kicks == []
    assert booths.get_booth_sync(room.booth_id("ja")).return_taken_by == "Thai"
    assert booths.get_booth_sync(room.booth_id("ja")).as_public_dict()["return_taken_by"] == "Thai"

    # They take turns: Thai switches back, Japanese takes English.
    assert await set_output(room, "th", ana, "primary") == []
    assert kicks == [thai_session]
    assert booths.get_booth_sync(room.booth_id("ja")).return_taken_by is None
    assert await set_output(room, "ja", kai, "return") == []
    assert booths.get_booth_sync(room.booth_id("th")).return_taken_by == "Japanese"
    japanese_session = await go_live(room, client, paths, "ja", kai)
    assert paths[room.path("en")]["source"]["id"] == japanese_session


@pytest.mark.anyio
async def test_switch_while_live_kicks_the_old_path_and_clears_ingest(media, monkeypatch):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    session_id = await go_live(room, client, paths, "th", ana)
    order = []

    async def broadcast(booth_id, state):
        order.append(("broadcast", list(kicks)))

    monkeypatch.setattr(manager_module, "broadcast_booth_state", broadcast)
    assert await set_output(room, "th", ana, "return") == []
    # Kicked after the state change, before the broadcast.
    assert order == [("broadcast", [session_id])]
    assert room.path("th") not in paths
    booth = booths.get_booth_sync(room.booth_id("th"))
    # Nothing publishes to English until the browser publishes again.
    assert booth.ingest_status == "disconnected"
    assert not booth.participants[ana].ingest_connected

    # Twice quickly: back and forth before the browser republished. Nothing more to kick.
    await set_output(room, "th", ana, "primary")
    await set_output(room, "th", ana, "return")
    assert kicks == [session_id]


@pytest.mark.anyio
async def test_a_switch_that_finishes_during_a_slow_kick_is_not_undone(media, monkeypatch):
    """Two sockets switch close together: the slower broadcast still sends the current direction."""
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    await go_live(room, client, paths, "th", ana)
    real_end = manager_module.end_publishers
    gate = asyncio.Event()
    sent = []

    async def slow_end(publishers, **kwargs):
        if publishers:
            await gate.wait()
        await real_end(publishers, **kwargs)

    async def broadcast(booth_id, state):
        sent.append(state["output_language_code"])

    monkeypatch.setattr(manager_module, "end_publishers", slow_end)
    monkeypatch.setattr(manager_module, "broadcast_booth_state", broadcast)
    first = asyncio.create_task(set_output(room, "th", ana, "return"))
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    await set_output(room, "th", None, "primary", session={"role": "room_coordinator"})
    gate.set()
    await first
    assert sent == ["th", "th"]
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "th"


@pytest.mark.anyio
async def test_switch_kick_failure_is_logged_and_never_blocks(media, monkeypatch, caplog):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    await go_live(room, client, paths, "th", ana)

    async def unreachable(*_args):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(booth_publish, "_kick_session", unreachable)
    with caplog.at_level("WARNING", logger="portal.booth_publish"):
        assert await set_output(room, "th", ana, "return") == []
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "en"
    assert "Could not end the publisher" in caplog.text


# ── Publish tickets and the media auth hook ─────────────────────────────────


@pytest.mark.anyio
async def test_ticket_follows_the_output_and_the_hook_checks_it(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    booths.get_booth_sync(room.booth_id("th")).broadcast_unlocked = True

    primary = await ticket(room, "th", ana)
    assert primary["path"] == room.path("th")
    # A primary-path ticket presented on the return path is refused (and spent).
    assert (await client.post("/internal/media-auth", json=hook(primary["ticket"], room.path("en")))).status_code == 401

    await set_output(room, "th", ana, "return")
    room.ensured.clear()
    returned = await ticket(room, "th", ana)
    assert returned["path"] == room.path("en")
    # The return path is created for the publish, e.g. after MediaMTX restarted.
    assert room.ensured == [room.path("en")]
    # A return-path ticket presented on the booth's own path is refused.
    assert (
        await client.post("/internal/media-auth", json=hook(returned["ticket"], room.path("th")))
    ).status_code == 401

    returned = await ticket(room, "th", ana)
    assert (
        await client.post("/internal/media-auth", json=hook(returned["ticket"], room.path("en")))
    ).status_code == 204


@pytest.mark.anyio
async def test_ticket_issued_before_a_switch_is_refused(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    booths.get_booth_sync(room.booth_id("th")).broadcast_unlocked = True
    stale = await ticket(room, "th", ana)
    await set_output(room, "th", ana, "return")
    assert (await client.post("/internal/media-auth", json=hook(stale["ticket"], stale["path"]))).status_code == 401

    stale = await ticket(room, "th", ana)
    await set_output(room, "th", ana, "primary")
    assert (await client.post("/internal/media-auth", json=hook(stale["ticket"], stale["path"]))).status_code == 401


@pytest.mark.anyio
async def test_a_booth_for_the_return_language_cannot_publish_while_it_is_taken(media):
    """Backstop for a booth added for the spoken language while a two-way booth holds it."""
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    eve, _ = await booths.join_participant(
        booth_id=room.booth_id("en"),
        display_name="Eve",
        role="interpreter",
        language="English",
        channel_id=room.path("en"),
        room_id=room.id,
        connection_id="eve-socket",
    )
    booths.get_booth_sync(room.booth_id("en")).broadcast_unlocked = True
    reply = await ticket(room, "en", eve.participant_id)
    assert reply["error"] == "The Thai booth is interpreting into English right now."

    # A ticket issued before Thai took English is refused at the hook.
    await set_output(room, "th", ana, "primary")
    early = await ticket(room, "en", eve.participant_id)
    await set_output(room, "th", ana, "return")
    assert (await client.post("/internal/media-auth", json=hook(early["ticket"], early["path"]))).status_code == 401


def test_publish_path_rejects_a_booth_without_identity():
    from portal.booth_state import Booth

    legacy = Booth(booth_id="legacy", event_slug="", language_code="", language="x", channel_id="x")
    with pytest.raises(ValueError):
        booth_publish.publish_path(legacy)
    booths._booths["legacy"] = legacy
    legacy.broadcast_unlocked = True
    try:
        assert not booth_publish.still_entitled(booth_publish.PublishTicket("legacy", "p", "x", 1e12))
    finally:
        booths._booths.pop("legacy")


# ── Stop, revoke, leave, grace, delete ──────────────────────────────────────


@pytest.mark.anyio
async def test_stop_kicks_both_paths_and_frees_the_return_channel(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    own_session = await go_live(room, client, paths, "th", ana)
    await set_output(room, "th", ana, "return")
    return_session = await go_live(room, client, paths, "th", ana)
    assert kicks == [own_session]  # the switch ended the session on the booth's own path
    kicks.clear()
    # Neither is recorded (e.g. both opened before a restart): Stop still ends both.
    booth_publish._publishers.clear()
    paths[room.path("th")] = {"name": room.path("th"), "source": {"type": "webRTCSession", "id": own_session}}

    ws = MockWebSocket()
    await _handle_set_broadcast_unlocked(ws, room.session("th", None, role="room_coordinator"), {"unlocked": False})
    assert ws.sent == []
    assert sorted(kicks) == sorted([own_session, return_session])
    booth = booths.get_booth_sync(room.booth_id("th"))
    assert booth.output_language_code == "th"
    assert booths.return_claim_owner(EVENT, room.id, "en") is None


@pytest.mark.anyio
async def test_stop_leaves_another_booths_return_publisher_alone(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    kai = await join(room, "ja", "Kai")
    await set_output(room, "ja", kai, "return")
    japanese = await go_live(room, client, paths, "ja", kai)
    booths.get_booth_sync(room.booth_id("th")).broadcast_unlocked = True
    # Thai knows it is two-way into English, so Stop checks that path too.
    booths.set_return_language_sync(room.booth_id("th"), "en", "English")

    ws = MockWebSocket()
    await _handle_set_broadcast_unlocked(ws, room.session("th", None, role="room_coordinator"), {"unlocked": False})
    assert kicks == []
    assert paths[room.path("en")]["source"]["id"] == japanese
    assert ana


@pytest.mark.anyio
async def test_leave_and_revoke_kick_the_return_session_and_free_the_channel(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    session_id = await go_live(room, client, paths, "th", ana)
    record = booths.get_booth_sync(room.booth_id("th")).participants[ana]
    await release_participant(room.session("th", ana, connection_id=record.connection_id), immediate=True)
    assert kicks == [session_id]
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "th"
    assert booths.return_claim_owner(EVENT, room.id, "en") is None


@pytest.mark.anyio
async def test_revoking_one_of_two_keeps_the_direction_for_the_backup(media):
    """A handover keeps the booth in English: the backup carries on there."""
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    ben = await join(room, "th", "Ben")
    await set_output(room, "th", ana, "return")
    session_id = await go_live(room, client, paths, "th", ana)
    record = booths.get_booth_sync(room.booth_id("th")).participants[ana]
    await release_participant(room.session("th", ana, connection_id=record.connection_id), immediate=True)
    assert kicks == [session_id]
    booth = booths.get_booth_sync(room.booth_id("th"))
    assert booth.active_interpreter_id == ben
    assert booth.output_language_code == "en"


@pytest.mark.anyio
async def test_grace_expiry_kicks_the_return_session(media, monkeypatch):
    room, client, paths, kicks = media
    monkeypatch.setattr(settings, "booth_reconnect_grace_seconds", 30)
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    session_id = await go_live(room, client, paths, "th", ana)
    record = booths.get_booth_sync(room.booth_id("th")).participants[ana]
    scheduled = []
    monkeypatch.setattr(manager_module, "schedule_participant_expiry", lambda *args: scheduled.append(args))
    await release_participant(room.session("th", ana, connection_id=record.connection_id), immediate=False)
    # Still in the booth during the grace: the channel stays theirs.
    assert kicks == []
    assert booths.return_claim_owner(EVENT, room.id, "en").booth_id == room.booth_id("th")
    [(booth_id, participant_id, connection_id, _delay)] = scheduled

    monkeypatch.setattr(manager_module.asyncio, "sleep", _no_sleep)
    schedule_participant_expiry(booth_id, participant_id, connection_id, 0)
    await _drain()
    assert kicks == [session_id]
    assert booths.return_claim_owner(EVENT, room.id, "en") is None


async def _no_sleep(_delay):
    return None


async def _drain():
    import asyncio

    from portal.tasks import _tasks

    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


@pytest.mark.anyio
async def test_deleting_a_booth_kicks_its_return_path(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    session_id = await go_live(room, client, paths, "th", ana)
    booth_publish._publishers.clear()
    await manager.close_booth_sessions(room.booth_id("th"))
    assert kicks == [session_id]
    assert booths.return_claim_owner(EVENT, room.id, "en") is None


@pytest.mark.anyio
async def test_a_deleted_booth_gone_from_memory_still_has_its_return_path_ended(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    session_id = await go_live(room, client, paths, "th", ana)
    booth_publish._publishers.clear()
    await booths.remove_booth(EVENT, room.id, "th")
    assert booths.return_claim_owner(EVENT, room.id, "en") is None
    await manager.close_booth_sessions(room.booth_id("th"), return_language_code="en")
    assert kicks == [session_id]


@pytest.mark.anyio
async def test_deletion_through_the_api_and_admin_ends_the_return_path(media, monkeypatch):
    """Room, event and booth deletion read the return language from the database."""
    room, client, paths, kicks = media
    from portal.websockets.revocation import revoke_booths_for_deleted_entity

    calls = []

    async def close(booth_id, **kwargs):
        calls.append((booth_id, kwargs))

    monkeypatch.setattr(manager, "close_booth_sessions", close)
    async with get_session() as session:
        thai = await session.scalar(select(DBBooth).where(DBBooth.language_code == "th"))
        await revoke_booths_for_deleted_entity(session, level="booth", target_id=thai.id)
        await revoke_booths_for_deleted_entity(session, level="room", target_id=room.id)
        await revoke_booths_for_deleted_entity(session, level="event", target_id=thai.event_id)
    assert calls[0] == (room.booth_id("th"), {"return_language_code": "en"})
    assert {booth_id for booth_id, _ in calls[1:]} == {room.booth_id("th"), room.booth_id("ja")}
    assert all(kwargs == {"return_language_code": "en"} for _, kwargs in calls)


@pytest.mark.anyio
@pytest.mark.parametrize("delete_path", ["by-room-id", "by-eventyay-room"])
async def test_legacy_booth_delete_ends_the_return_path(room, monkeypatch, delete_path):
    from portal.routers import api as api_module

    async with get_session() as session:
        (await session.get(Room, room.id)).eventyay_room_id = "main"
    monkeypatch.setattr(settings, "booth_access_token", "")
    stopped, told = [], []

    async def stop(booth_id, publishers, **kwargs):
        stopped.append((booth_id, kwargs))

    async def tell(event_slug, room_id):
        told.append((event_slug, room_id))

    async def no_worker(_booth_id):
        return None

    monkeypatch.setattr(api_module, "stop_booth_publish", stop)
    monkeypatch.setattr(api_module, "broadcast_channel_state", tell)
    monkeypatch.setattr(api_module, "stop_transcription_worker", no_worker)
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    url = (
        f"/api/events/{EVENT}/rooms/{room.id}/booths/th"
        if delete_path == "by-room-id"
        else f"/api/events/{EVENT}/eventyay-rooms/main/booths/th"
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.delete(url)).status_code == 204
    assert stopped == [(room.booth_id("th"), {"return_language_code": "en"})]
    assert told == [(EVENT, room.id)]
    assert booths.return_claim_owner(EVENT, room.id, "en") is None


def test_taking_one_paths_sessions_keeps_the_others():
    booth_publish.clear_tickets()
    for session_id, path in (("a", "ev/1/th"), ("b", "ev/1/en")):
        booth_publish._remember(booth_publish.Publisher("ev-1-th", "p", path, session_id))
    assert [p.session_id for p in booth_publish.take_path_publishers("ev-1-th", "ev/1/en")] == ["b"]
    assert list(booth_publish._publishers["ev-1-th"]) == ["a"]
    assert booth_publish.take_path_publishers("ev-1-th", "ev/1/en") == []
    assert booth_publish.take_path_publishers("nobody", "ev/1/en") == []
    booth_publish.clear_tickets()


@pytest.mark.anyio
async def test_stopping_a_malformed_booth_id_is_logged(caplog):
    with caplog.at_level("WARNING", logger="portal.booth_publish"):
        await booth_publish.stop_booth_publish("legacy", [])
    assert "Could not stop the publisher of booth legacy" in caplog.text


def test_a_booth_can_start_on_its_return_channel():
    from portal.booth_state import Booth

    booth = Booth(
        booth_id="ev-1-th",
        event_slug="ev",
        language_code="th",
        language="Thai",
        channel_id="",
        room_id=1,
        output_language_code="en",
    )
    assert booth.outputs_into_return and booth.output_mediamtx_path == "ev/1/en"


@pytest.mark.anyio
async def test_each_room_has_its_own_return_channel():
    registry = BoothRegistry()
    await registry.restore([_record("ev-1-th", "en"), {**_record("ev-2-th", "en"), "room_id": 2}])
    assert registry.room_return_booth("ev", 2).booth_id == "ev-2-th"
    assert registry.room_return_booth("ev", 1).booth_id == "ev-1-th"
    assert registry.room_return_booth("ev", 3) is None


@pytest.mark.anyio
async def test_removing_idle_booths_frees_their_return_channel(room):
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    assert await booths.remove_idle_booths(EVENT, room.id, ["th"]) is None
    assert booths.return_claim_owner(EVENT, room.id, "en") is None


@pytest.mark.anyio
async def test_lock_broadcast_sync_resets_an_idle_return_booth(room):
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    booths.lock_broadcast_sync(room.booth_id("th"))
    assert booths.get_booth_sync(room.booth_id("th")).output_language_code == "th"
    assert booths.return_claim_owner(EVENT, room.id, "en") is None


@pytest.mark.anyio
async def test_losing_the_return_language_while_idle_resets_the_direction(room):
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    assert booths.set_return_language_sync(room.booth_id("th"), None, "") is True
    booth = booths.get_booth_sync(room.booth_id("th"))
    assert booth.output_language_code == "th" and booth.return_language_code is None
    assert booths.set_return_language_sync(room.booth_id("th"), None, "") is False
    assert booths.set_return_language_sync("unknown-1-th", "en", "English") is False


# ── Joining picks up the return language ────────────────────────────────────


@pytest.mark.anyio
async def test_join_reports_the_return_language(room):
    ws = MockWebSocket()
    session = room.session("th", None, session_key="user:Ana", return_language=("en", "English"))
    await _handle_join(ws, session, {"type": "booth:join", "display_name": "Ana", "language": "Thai"})
    joined = next(m for m in ws.sent if m["type"] == "booth:joined")
    assert joined["state"]["return_language_code"] == "en"
    assert joined["state"]["return_language"] == "English"


@pytest.mark.anyio
async def test_booth_socket_loads_the_return_language(room):
    from portal.websockets.handlers import _booth_return_language

    assert await _booth_return_language(room.booth_id("th")) == ("en", "English")
    assert await _booth_return_language("malformed") is None


@pytest.mark.anyio
async def test_booth_socket_survives_a_failed_return_language_read(monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError

    from portal.websockets import handlers

    async def broken(_booth_id):
        raise OperationalError("select", {}, Exception("down"))

    monkeypatch.setattr(handlers, "load_booth_return_language", broken)
    with caplog.at_level("WARNING"):
        assert await handlers._booth_return_language("x-1-th") is None
    assert "Could not read the return language" in caplog.text


# ── Attendance ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_interpreting_into_the_return_language_counts_as_interpreting(media):
    room, client, paths, kicks = media
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    await go_live(room, client, paths, "th", ana)
    assert is_interpreting(booths.get_booth_sync(room.booth_id("th")), ana)


# ── Restart persistence ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_direction_survives_a_restart_and_the_same_session_reclaims(room):
    ana = await join(room, "th", "Ana")
    booths.get_booth_sync(room.booth_id("th")).broadcast_unlocked = True
    await set_output(room, "th", ana, "return")
    record = booths.get_booth_sync(room.booth_id("th")).live_record()
    assert record["output_language_code"] == "en"
    await booth_recovery.save_changes({room.booth_id("th"): record}, booth_recovery._utc_now())
    async with get_session() as session:
        row = await session.get(BoothLiveState, room.booth_id("th"))
        assert row.output_language_code == "en"

    restarted = BoothRegistry()
    placeholders = await booth_recovery.restore(restarted, max_age_seconds=600)
    assert placeholders == [(room.booth_id("th"), ana)]
    booth = restarted.get_booth_sync(room.booth_id("th"))
    assert booth.output_language_code == "en"
    assert booth.output_mediamtx_path == room.path("en")
    assert restarted.return_claim_owner(EVENT, room.id, "en") is booth
    participant, _ = await restarted.join_participant(
        booth_id=room.booth_id("th"),
        display_name="Ana",
        role="interpreter",
        language="Thai",
        channel_id=room.path("th"),
        participant_id=ana,
        joined_by="user:Ana",
        connection_id="new-socket",
    )
    assert participant.participant_id == ana
    assert restarted.get_booth_sync(room.booth_id("th")).output_language_code == "en"


@pytest.mark.anyio
async def test_one_way_booth_saves_no_direction(room):
    ana = await join(room, "th", "Ana")
    booths.get_booth_sync(room.booth_id("th")).broadcast_unlocked = True
    assert booths.get_booth_sync(room.booth_id("th")).live_record()["output_language_code"] is None
    assert ana


@pytest.mark.anyio
@pytest.mark.parametrize("saved", [None, "th", 5, "zz", "floor"])
async def test_restore_ignores_a_missing_or_bad_direction(saved):
    registry = BoothRegistry()
    await registry.restore([_record("ev-1-th", saved)])
    assert registry.get_booth_sync("ev-1-th").output_language_code == "th"


@pytest.mark.anyio
async def test_restore_never_puts_two_booths_on_one_channel():
    registry = BoothRegistry()
    await registry.restore([_record("ev-1-th", "en"), _record("ev-1-ja", "en")])
    assert registry.get_booth_sync("ev-1-th").output_language_code == "en"
    assert registry.get_booth_sync("ev-1-ja").output_language_code == "ja"
    # When Japanese rejoins and learns its return language, it sees English is taken.
    registry.set_return_language_sync("ev-1-ja", "en", "English")
    assert registry.get_booth_sync("ev-1-ja").return_taken_by == "Thai"


def _record(booth_id: str, output) -> dict:
    return {
        "booth_id": booth_id,
        "language": "Thai" if booth_id.endswith("th") else "Japanese",
        "channel_id": booth_id,
        "room_id": 1,
        "broadcast_unlocked": True,
        "active_participant_id": uuid4().hex,
        "active_session_key": "user:1",
        "active_display_name": "Ana",
        "active_role": "interpreter",
        "active_user_id": None,
        "active_invite_public_id": None,
        "active_mic_active": False,
        "active_ingest_connected": False,
        "output_language_code": output,
    }


# ── Spoken language changes ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_spoken_language_change_drops_return_languages(room):
    async with get_session() as session:
        assert await drop_mismatched_returns(session, room.id, "en") == ()
        assert set(await drop_mismatched_returns(session, room.id, "fr")) == {"th", "ja"}
        assert {b.return_language_code for b in (await session.scalars(select(DBBooth))).all()} == {None}


@pytest.mark.anyio
async def test_spoken_language_change_refused_while_a_booth_outputs_into_it(room):
    async with get_session() as session:
        with pytest.raises(ReturnLanguageInUse, match="The Thai booth is interpreting into English right now"):
            await drop_mismatched_returns(session, room.id, "fr", outputting=["th"])
        # Nothing was changed.
        assert {b.return_language_code for b in (await session.scalars(select(DBBooth))).all()} == {"en"}


# ── Original-channel listeners (server side of channel:state) ──────────────


class ListenerSocket(MockWebSocket):
    async def accept(self):
        return None


@pytest.mark.anyio
async def test_channel_state_follows_the_return_booth_on_and_off_air(media):
    room, client, paths, kicks = media
    floor_id = room.booth_id("floor")
    listener = ListenerSocket()
    listener_manager.add(listener, floor_id)
    try:
        ana = await join(room, "th", "Ana")
        await manager_module.broadcast_channel_state(EVENT, room.id)
        assert listener.sent == []  # no two-way booth on air: rooms without one never get the message

        await set_output(room, "th", ana, "return")
        assert listener.sent[-1] == {
            "type": "channel:state",
            "state": {"return_on_air": False, "whep_url": None, "language_code": "en"},
        }
        await go_live(room, client, paths, "th", ana)
        await manager_module.broadcast_booth_state(
            room.booth_id("th"), booths.get_booth_sync(room.booth_id("th")).as_public_dict()
        )
        on_air = listener.sent[-1]
        assert on_air == {
            "type": "channel:state",
            "state": {
                "return_on_air": True,
                "whep_url": f"{settings.mediamtx_whip_base}/{room.path('en')}/whep",
                "language_code": "en",
            },
        }
        # Listener-safe: exactly these keys, nothing about the booth's people or chat.
        assert set(on_air["state"]) == {"return_on_air", "whep_url", "language_code"}
        count = len(listener.sent)
        await manager_module.broadcast_channel_state(EVENT, room.id)
        assert len(listener.sent) == count  # unchanged state is not resent

        await set_output(room, "th", ana, "primary")
        assert listener.sent[-1] == {
            "type": "channel:state",
            "state": {"return_on_air": False, "whep_url": None, "language_code": None},
        }
    finally:
        listener_manager.remove(listener, floor_id)


@pytest.mark.anyio
async def test_new_original_channel_listener_hears_about_a_return_already_on_air(media):
    room, client, paths, kicks = media
    floor_id = room.booth_id("floor")
    ana = await join(room, "th", "Ana")
    # Like ws_captions, each socket joins the feed before it is sent the current state.
    idle, first, second = ListenerSocket(), ListenerSocket(), ListenerSocket()
    listener_manager.add(idle, floor_id)
    await send_channel_state(idle, floor_id)
    listener_manager.remove(idle, floor_id)
    assert idle.sent == []
    await set_output(room, "th", ana, "return")
    await go_live(room, client, paths, "th", ana)
    expected = [{"type": "channel:state", "state": floor_channel_state(EVENT, room.id)}]
    try:
        listener_manager.add(first, floor_id)
        await send_channel_state(first, floor_id)
        listener_manager.add(second, floor_id)
        await send_channel_state(second, floor_id)
    finally:
        listener_manager.remove(first, floor_id)
        listener_manager.remove(second, floor_id)
    assert first.sent == expected and second.sent == expected
    assert first.sent[0]["state"]["return_on_air"] is True
    # Other caption feeds and malformed ids get nothing.
    other = ListenerSocket()
    await send_channel_state(other, room.booth_id("th"))
    await send_channel_state(other, "malformed")
    assert other.sent == []


@pytest.mark.anyio
async def test_a_listener_joining_before_the_broadcast_does_not_hide_it_from_the_others(room):
    """A listener connects after the registry changed but before that change is broadcast."""
    floor_id = room.booth_id("floor")
    early, late = ListenerSocket(), ListenerSocket()
    listener_manager.add(early, floor_id)
    listener_manager.add(late, floor_id)
    try:
        ana = await join(room, "th", "Ana")
        booth = booths.get_booth_sync(room.booth_id("th"))
        for output, on_air in (("return", True), ("primary", False)):
            await booths.set_output(
                booth.booth_id, ana, output, can_reassign=False, return_language_code="en", return_language="English"
            )
            booth.broadcast_unlocked, booth.ingest_status = True, "connected"
            await send_channel_state(late, floor_id)
            await manager_module.broadcast_channel_state(EVENT, room.id)
            assert early.sent[-1]["state"]["return_on_air"] is on_air
            assert late.sent[-1]["state"]["return_on_air"] is on_air
            assert early.sent.count(early.sent[-1]) == 1
    finally:
        listener_manager.remove(early, floor_id)
        listener_manager.remove(late, floor_id)


@pytest.mark.anyio
async def test_a_listener_who_joined_on_air_is_told_when_the_return_goes_off(media):
    """The first listener joins while the return is already on air, then the booth switches back."""
    room, client, paths, kicks = media
    floor_id = room.booth_id("floor")
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    await go_live(room, client, paths, "th", ana)
    listener = ListenerSocket()
    listener_manager.add(listener, floor_id)
    try:
        await send_channel_state(listener, floor_id)
        assert listener.sent[-1]["state"]["return_on_air"] is True
        await set_output(room, "th", ana, "primary")
        assert listener.sent[-1] == {"type": "channel:state", "state": manager_module._NO_RETURN}
    finally:
        listener_manager.remove(listener, floor_id)


@pytest.mark.anyio
async def test_deleting_the_return_booth_tells_original_channel_listeners(media):
    room, client, paths, kicks = media
    floor_id = room.booth_id("floor")
    listener = ListenerSocket()
    listener_manager.add(listener, floor_id)
    try:
        ana = await join(room, "th", "Ana")
        await set_output(room, "th", ana, "return")
        await go_live(room, client, paths, "th", ana)
        await manager_module.broadcast_channel_state(EVENT, room.id)
        assert listener.sent[-1]["state"]["return_on_air"] is True
        await manager.close_booth_sessions(room.booth_id("th"))
        assert listener.sent[-1]["state"] == {"return_on_air": False, "whep_url": None, "language_code": None}
    finally:
        listener_manager.remove(listener, floor_id)


@pytest.mark.anyio
async def test_channel_state_ignores_rooms_it_cannot_name(room):
    await manager_module.broadcast_channel_state(None, room.id)
    await manager_module.broadcast_channel_state(EVENT, "1")
    await manager_module.broadcast_channel_state("", 1)
    assert manager_module._last_channel_states == {}


@pytest.mark.anyio
async def test_sibling_booths_are_told_when_the_return_channel_is_taken(room, monkeypatch):
    ana = await join(room, "th", "Ana")
    kai = await join(room, "ja", "Kai")
    booths.set_return_language_sync(room.booth_id("ja"), "en", "English")
    sent = []

    async def booth_broadcast(booth_id, message):
        sent.append((booth_id, message["state"]["return_taken_by"]))

    monkeypatch.setattr(manager, "broadcast", booth_broadcast)
    await set_output(room, "th", ana, "return")
    assert (room.booth_id("ja"), "Thai") in sent
    sent.clear()
    await set_output(room, "th", ana, "primary")
    assert (room.booth_id("ja"), None) in sent
    assert kai


# ── Booth WebSocket dispatch ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_booth_socket_dispatches_set_output_and_loads_the_return_language(room, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import WebSocketDisconnect

    from portal.websockets import handlers

    received = []

    async def handle(ws, session, data):
        received.append((session.return_language, data))

    monkeypatch.setattr(handlers, "_handle_set_output", handle)
    monkeypatch.setattr(handlers, "resolve_ws_auth", AsyncMock(return_value={"booth_id": 1, "role": "interpreter"}))
    monkeypatch.setattr(handlers, "resolve_booth_role", AsyncMock(return_value="interpreter"))
    monkeypatch.setattr(handlers, "manager", manager_module.ConnectionManager())
    ws = ListenerSocket()
    ws.receive_text = AsyncMock(
        side_effect=[json.dumps({"type": "booth:set-output", "output": "return"}), WebSocketDisconnect()]
    )
    await handlers.ws_booth(ws, room.booth_id("th"))
    assert received == [(("en", "English"), {"type": "booth:set-output", "output": "return"})]


# ── On air, by direction (#1397 parts 1 and 2 together) ──────────────────────


@pytest.fixture
def known_paths():
    from portal import booth_on_air

    booth_on_air.live_paths = None
    yield booth_on_air
    booth_on_air.live_paths = None


@pytest.mark.anyio
async def test_own_channel_is_off_air_while_the_booth_outputs_into_return(media, known_paths):
    from portal.websockets.manager import listener_booth_state

    room, client, paths, _kicks = media
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    await go_live(room, client, paths, "th", ana)
    state = booths.get_booth_sync(room.booth_id("th")).as_public_dict()

    # MediaMTX unknown: the interpreter's report alone must not put Thai on air.
    assert listener_booth_state(state)["on_air"] is False
    assert known_paths.is_return_on_air(state) is True
    # MediaMTX known, even listing the Thai path: Thai still isn't what the booth sends.
    known_paths.live_paths = frozenset({room.path("th"), room.path("en")})
    assert listener_booth_state(state)["on_air"] is False
    assert floor_channel_state(EVENT, room.id)["return_on_air"] is True

    await set_output(room, "th", ana, "primary")
    await go_live(room, client, paths, "th", ana)
    state = booths.get_booth_sync(room.booth_id("th")).as_public_dict()
    assert listener_booth_state(state)["on_air"] is True
    assert known_paths.is_return_on_air(state) is False


@pytest.mark.anyio
async def test_return_needs_a_publisher_on_the_return_path(media, known_paths):
    room, client, paths, _kicks = media
    ana = await join(room, "th", "Ana")
    await set_output(room, "th", ana, "return")
    await go_live(room, client, paths, "th", ana)

    known_paths.live_paths = frozenset({room.path("th")})
    assert floor_channel_state(EVENT, room.id) == {"return_on_air": False, "whep_url": None, "language_code": "en"}
    known_paths.live_paths = frozenset({room.path("en")})
    assert floor_channel_state(EVENT, room.id)["return_on_air"] is True

    booths.get_booth_sync(room.booth_id("th")).broadcast_unlocked = False
    assert floor_channel_state(EVENT, room.id)["return_on_air"] is False


@pytest.mark.anyio
async def test_on_air_loop_tells_the_floor_feed_when_the_return_stream_goes(media, known_paths, monkeypatch):
    room, client, paths, _kicks = media
    floor_id = room.booth_id("floor")
    listener = ListenerSocket()
    listener_manager.add(listener, floor_id)
    stray = ListenerSocket()
    listener_manager.add(stray, "not a booth id")
    try:
        ana = await join(room, "th", "Ana")
        await set_output(room, "th", ana, "return")
        await go_live(room, client, paths, "th", ana)
        await manager_module.broadcast_channel_state(EVENT, room.id)
        assert listener.sent[-1]["state"]["return_on_air"] is True

        async def path_gone():
            known_paths.live_paths = frozenset()

        async def one_tick(_seconds):
            raise asyncio.CancelledError

        monkeypatch.setattr(manager_module, "refresh_live_paths", path_gone)
        monkeypatch.setattr(manager_module.asyncio, "sleep", one_tick)
        with pytest.raises(asyncio.CancelledError):
            await manager_module.on_air_loop()
        assert listener.sent[-1] == {
            "type": "channel:state",
            "state": {"return_on_air": False, "whep_url": None, "language_code": "en"},
        }
        assert stray.sent == []
    finally:
        listener_manager.remove(listener, floor_id)
        listener_manager.remove(stray, "not a booth id")


@pytest.mark.anyio
async def test_go_live_into_return_is_checked_on_the_return_path(media, known_paths, monkeypatch):
    room, client, paths, _kicks = media
    floor_id = room.booth_id("floor")
    listener = ListenerSocket()
    listener_manager.add(listener, floor_id)
    checked: list[str] = []

    async def confirm(path):
        checked.append(path)
        known_paths.live_paths = known_paths.live_paths | {path}
        return True

    monkeypatch.setattr(manager_module, "confirm_live_path", confirm)
    try:
        ana = await join(room, "th", "Ana")
        await set_output(room, "th", ana, "return")
        known_paths.live_paths = frozenset()  # read before the booth went live
        await go_live(room, client, paths, "th", ana)
        booth_id = room.booth_id("th")
        await manager_module.broadcast_booth_state(booth_id, booths.get_booth_sync(booth_id).as_public_dict())
        for _ in range(5):
            await asyncio.sleep(0)
        assert checked and set(checked) == {room.path("en")}, "only floor-feed listeners, and the return path"
        assert listener.sent[-1]["state"]["return_on_air"] is True
    finally:
        listener_manager.remove(listener, floor_id)
