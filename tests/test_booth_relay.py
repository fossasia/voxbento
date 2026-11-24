"""Relay interpreting (#1382).

The relay booth is never offered its own channel, Mission Control can change the relay during a
session, and the room's open booths are told at once (``booth:relay``) when it changes or the relay
booth goes on or off air.
"""

from __future__ import annotations

import html
import json
import re
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal import booth_relay
from portal.auth import create_participant_token, create_user_token
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_room_by_id,
    get_session,
    init_db,
)
from portal.globals import booths
from portal.models import BoothMembership, RoomMembership
from portal.roles import INTERPRETER, ROOM_COORDINATOR
from portal.websockets import manager as ws

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("auto_csrf")]


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    booth_relay.reset()
    yield
    booth_relay.reset()
    await dispose()


@pytest.fixture
async def rooms():
    """One room with an English and a French booth, and another room with a German booth."""
    async with get_session() as s:
        event = await create_event(s, slug="relaycon", display_name="RelayCon")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        other = await create_room(s, event_id=event.id, display_name="Hall B")
        en = await create_booth(s, event_id=event.id, room_id=room.id, language_code="en", language_name="English")
        fr = await create_booth(s, event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
        de = await create_booth(s, event_id=event.id, room_id=other.id, language_code="de", language_name="German")
        ids = SimpleNamespace(event=event.id, room=room.id, other=other.id, en=en.id, fr=fr.id, de=de.id)
    ids.en_booth = f"relaycon-{ids.room}-en"
    ids.fr_booth = f"relaycon-{ids.room}-fr"
    ids.de_booth = f"relaycon-{ids.other}-de"
    return ids


async def _set_relay(room_id: int, booth_db_id: int | None) -> None:
    async with get_session() as s:
        (await get_room_by_id(s, room_id)).relay_booth_id = booth_db_id


async def _user(email: str, *, is_admin: bool = False, room_role: tuple[int, str] | None = None, booth_role=None):
    async with get_session() as s:
        user = await create_user(s, email=email, display_name=email, is_admin=is_admin, email_verified=True)
        if room_role:
            s.add(RoomMembership(user_id=user.id, room_id=room_role[0], role=room_role[1]))
        if booth_role:
            s.add(BoothMembership(user_id=user.id, booth_id=booth_role[0], role=booth_role[1]))
        user_id = user.id
    return {"user_token": create_user_token(user_id=user_id, email=email, is_admin=is_admin)}


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def sent(monkeypatch, rooms):
    """Pretend the room's two booths and the other room's booth have open sockets; record what each gets."""
    messages: list[tuple[str, dict]] = []

    async def broadcast(booth_id: str, message: dict) -> None:
        messages.append((booth_id, message))

    monkeypatch.setattr(ws.manager, "connected_booth_ids", lambda: [rooms.en_booth, rooms.fr_booth, rooms.de_booth])
    monkeypatch.setattr(ws.manager, "broadcast", broadcast)
    return messages


def _relay_messages(messages: list[tuple[str, dict]]) -> dict[str, dict]:
    return {booth_id: msg for booth_id, msg in messages if msg["type"] == "booth:relay"}


def _page_relay(page_html: str) -> dict:
    match = re.search(r"data-relay='([^']*)'", page_html)
    assert match, "the relay data is not in the page"
    return json.loads(html.unescape(match.group(1)))


# ── The booth page ───────────────────────────────────────────────────────────


async def _booth_page(rooms, language_code: str) -> str:
    token = create_participant_token(
        booth_id=getattr(rooms, language_code),
        role=INTERPRETER,
        event_slug="relaycon",
        room_id=rooms.room,
        language_code=language_code,
    )
    async with _client() as c:
        page = await c.get(f"/interpreter/relaycon/{rooms.room}/{language_code}", cookies={"session_token": token})
    assert page.status_code == 200
    return page.text


async def test_relay_booth_is_not_offered_its_own_channel(rooms):
    await _set_relay(rooms.room, rooms.en)

    own = _page_relay(await _booth_page(rooms, "en"))
    assert own == {"relay_booth_id": rooms.en_booth, "relay": None}
    assert "data-relay-whep-url=''" in await _booth_page(rooms, "en")

    other = _page_relay(await _booth_page(rooms, "fr"))
    assert other["relay_booth_id"] == rooms.en_booth
    assert other["relay"]["language_name"] == "English"
    assert other["relay"]["whep_url"].endswith(f"/relaycon/{rooms.room}/en/whep")
    assert other["relay"]["on_air"] is False


async def test_page_without_relay(rooms):
    assert _page_relay(await _booth_page(rooms, "fr")) == {"relay_booth_id": None, "relay": None}


# ── Choosing the relay from Mission Control ──────────────────────────────────


async def test_coordinator_switches_the_relay_and_open_booths_follow(rooms, sent):
    cookie = await _user("coord@example.com", room_role=(rooms.room, ROOM_COORDINATOR))
    async with _client() as c:
        resp = await c.post(f"/api/rooms/{rooms.room}/relay", json={"language_code": "fr"}, cookies=cookie)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"relay_booth_id": rooms.fr_booth}
    async with get_session() as s:
        assert (await get_room_by_id(s, rooms.room)).relay_booth_id == rooms.fr

    pushed = _relay_messages(sent)
    # The French booth is the relay: told so, but given nothing to play.
    assert pushed[rooms.fr_booth] == {"type": "booth:relay", "relay_booth_id": rooms.fr_booth, "relay": None}
    assert pushed[rooms.en_booth]["relay"]["whep_url"].endswith(f"/relaycon/{rooms.room}/fr/whep")
    # Another room's booth hears nothing about this room's relay.
    assert rooms.de_booth not in pushed

    sent.clear()
    async with _client() as c:
        resp = await c.post(f"/api/rooms/{rooms.room}/relay", json={"language_code": None}, cookies=cookie)
    assert resp.json() == {"relay_booth_id": None}
    assert _relay_messages(sent)[rooms.en_booth] == {"type": "booth:relay", "relay_booth_id": None, "relay": None}


async def test_interpreter_cannot_change_the_relay(rooms, sent):
    cookie = await _user("interp@example.com", booth_role=(rooms.en, INTERPRETER))
    async with _client() as c:
        resp = await c.post(f"/api/rooms/{rooms.room}/relay", json={"language_code": "en"}, cookies=cookie)
    assert resp.status_code == 403
    async with get_session() as s:
        assert (await get_room_by_id(s, rooms.room)).relay_booth_id is None
    assert sent == []


async def test_coordinator_of_another_room_cannot_change_the_relay(rooms):
    cookie = await _user("other@example.com", room_role=(rooms.other, ROOM_COORDINATOR))
    async with _client() as c:
        resp = await c.post(f"/api/rooms/{rooms.room}/relay", json={"language_code": "en"}, cookies=cookie)
    assert resp.status_code == 403


async def test_relay_must_be_a_booth_of_the_room(rooms):
    cookie = await _user("admin@example.com", is_admin=True)
    async with _client() as c:
        other_room = await c.post(f"/api/rooms/{rooms.room}/relay", json={"language_code": "de"}, cookies=cookie)
        unknown = await c.post(f"/api/rooms/{rooms.room}/relay", json={"language_code": "ja"}, cookies=cookie)
    assert other_room.status_code == 404
    assert unknown.status_code == 404
    async with get_session() as s:
        assert (await get_room_by_id(s, rooms.room)).relay_booth_id is None


async def test_room_page_save_reaches_open_booths(rooms, sent):
    cookie = await _user("admin@example.com", is_admin=True)
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{rooms.event}/rooms/{rooms.room}/edit",
            data={"form_section": "relay", "relay_booth_id": str(rooms.en)},
            cookies=cookie,
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert _relay_messages(sent)[rooms.fr_booth]["relay_booth_id"] == rooms.en_booth


async def test_deleting_the_relay_booth_stops_the_relay(rooms, sent):
    await _set_relay(rooms.room, rooms.en)
    await booth_relay.load_room_relay(rooms.room)
    cookie = await _user("admin@example.com", is_admin=True)
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{rooms.event}/rooms/{rooms.room}/booths/{rooms.en}/delete",
            cookies=cookie,
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert _relay_messages(sent)[rooms.fr_booth] == {"type": "booth:relay", "relay_booth_id": None, "relay": None}


# ── On air, and joining ──────────────────────────────────────────────────────


async def test_relay_booth_going_on_and_off_air_is_pushed_once(rooms, sent, monkeypatch):
    await _set_relay(rooms.room, rooms.en)
    await booth_relay.load_room_relay(rooms.room)
    ingest = {"status": "disconnected"}
    monkeypatch.setattr(
        booths,
        "get_booth_sync",
        lambda booth_id: SimpleNamespace(ingest_status=ingest["status"]) if booth_id == rooms.en_booth else None,
    )

    ingest["status"] = "connected"
    await ws.broadcast_booth_state(rooms.en_booth, {"ingest_status": "connected"})
    assert _relay_messages(sent)[rooms.fr_booth]["relay"]["on_air"] is True

    # Chat, roster changes and the like rebroadcast the same state: nothing more to say.
    sent.clear()
    await ws.broadcast_booth_state(rooms.en_booth, {"ingest_status": "connected"})
    await ws.broadcast_booth_state(rooms.fr_booth, {"ingest_status": "disconnected"})
    assert _relay_messages(sent) == {}

    ingest["status"] = "disconnected"
    await ws.broadcast_booth_state(rooms.en_booth, {"ingest_status": "disconnected"})
    assert _relay_messages(sent)[rooms.fr_booth]["relay"]["on_air"] is False


async def test_join_carries_the_current_relay(rooms):
    await _set_relay(rooms.room, rooms.en)
    joining_fr = await ws._current_relay_fields(rooms.fr_booth)
    assert joining_fr["relay_booth_id"] == rooms.en_booth
    assert joining_fr["relay"]["language_code"] == "en"
    assert await ws._current_relay_fields(rooms.en_booth) == {"relay_booth_id": rooms.en_booth, "relay": None}

    # Read fresh on every join, so a booth that reconnects after a change is up to date.
    await _set_relay(rooms.room, None)
    assert await ws._current_relay_fields(rooms.fr_booth) == {"relay_booth_id": None, "relay": None}


async def test_join_for_a_booth_id_of_another_event_gets_no_relay(rooms):
    await _set_relay(rooms.room, rooms.en)
    assert await ws._current_relay_fields(f"othercon-{rooms.room}-fr") == {"relay_booth_id": None, "relay": None}
