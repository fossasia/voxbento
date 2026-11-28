"""A booth's language code can't change while the booth is in use (#1312)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token
from portal.booth_identity import make_booth_id, make_mediamtx_path, parse_booth_id
from portal.database import configure, create_booth, create_event, create_room, dispose, get_session, init_db
from portal.globals import booths
from portal.models import DBBooth, Event, User
from portal.roles import INTERPRETER
from portal.routers import admin
from portal.transcription.worker import active_workers

# These tests are not about CSRF; their requests carry the token a real page would send.
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("auto_csrf")]

ORIGIN = {"origin": "http://test"}
IN_USE = (
    "Someone is in this booth, it is on air or its captions are running. "
    "Change its language code once everyone has left, it is off air and captions are stopped."
)


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    for booth_id in booths.booth_ids():
        await booths.remove_booth(*parse_booth_id(booth_id))
    await booths.take_changes()
    active_workers.clear()
    await dispose()


@pytest.fixture
async def booth():
    async with get_session() as s:
        owner = User(email="owner@example.com", display_name="Owner", is_admin=True, email_verified=True)
        event_owner = User(email="event-owner@example.com", display_name="Event owner", email_verified=True)
        s.add_all([owner, event_owner])
        await s.flush()
        event = await create_event(s, slug="conf", display_name="Conf", owner_user_id=event_owner.id)
        room = await create_room(s, event_id=event.id, display_name="Hall")
        row = await create_booth(s, event_id=event.id, room_id=room.id, language_code="es", language_name="Spanish")
        ids = {"event": event.id, "room": room.id, "booth": row.id}
    return {
        **ids,
        "booth_id": make_booth_id("conf", ids["room"], "es"),
        "channel": make_mediamtx_path("conf", ids["room"], "es"),
        "url": f"/admin/events/{ids['event']}/rooms/{ids['room']}/booths/{ids['booth']}/edit",
        "cookies": {"user_token": create_user_token(user_id=owner.id, email=owner.email, is_admin=True)},
        "owner_cookies": {"user_token": create_user_token(user_id=event_owner.id, email=event_owner.email)},
    }


async def _edit(booth, data, url=None, cookies=None, **headers):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post(
            url or booth["url"],
            data=data,
            headers={**ORIGIN, **headers},
            cookies=cookies or booth["cookies"],
            follow_redirects=False,
        )


async def _saved(booth) -> tuple[str, str]:
    async with get_session() as s:
        row = await s.get(DBBooth, booth["booth"])
        return row.language_code, row.language_name


async def _join(booth) -> str:
    participant, _ = await booths.join_participant(
        booth["booth_id"],
        "Ana",
        INTERPRETER,
        "es",
        booth["channel"],
        room_id=booth["room"],
        joined_by="ana",
        connection_id="socket-1",
    )
    return participant.participant_id


async def test_code_is_kept_while_an_interpreter_is_in_the_booth(booth):
    await _join(booth)
    resp = await _edit(booth, {"language_code": "de", "language_name": "German"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == IN_USE
    assert await _saved(booth) == ("es", "Spanish")  # nothing from the refused form is saved
    assert len(booths.get_booth_sync(booth["booth_id"]).participants) == 1

    # The name can still change, also when the code is sent again in another case.
    renamed = await _edit(booth, {"language_code": "ES", "language_name": "Español"})
    assert renamed.status_code == 303
    assert await _saved(booth) == ("es", "Español")
    # An invalid code is refused for being invalid, before anything else.
    assert (await _edit(booth, {"language_code": "xx"})).status_code == 400
    assert len(booths.get_booth_sync(booth["booth_id"]).participants) == 1


async def test_code_is_kept_while_an_interpreter_is_reconnecting(booth):
    participant_id = await _join(booth)
    # The socket dropped; the participant is kept for the reconnect grace.
    assert await booths.disconnect_participant(booth["booth_id"], participant_id, "socket-1") is not None
    resp = await _edit(booth, {"language_code": "de"})
    assert resp.status_code == 409
    assert await _saved(booth) == ("es", "Spanish")


async def test_code_is_kept_while_the_booth_is_on_air(booth):
    await booths.set_broadcast_unlocked(booth["booth_id"], True, "es", booth["channel"])
    resp = await _edit(booth, {"language_code": "de"})
    assert resp.status_code == 409
    assert await _saved(booth) == ("es", "Spanish")
    assert booths.get_booth_sync(booth["booth_id"]).broadcast_unlocked


async def test_code_is_kept_while_captions_are_running(booth):
    active_workers[booth["booth_id"]] = object()
    resp = await _edit(booth, {"language_code": "de"})
    assert resp.status_code == 409
    assert await _saved(booth) == ("es", "Spanish")


async def test_the_booth_page_says_why(booth):
    await _join(booth)
    resp = await _edit(booth, {"language_code": "de"}, accept="text/html")
    assert resp.status_code == 409
    assert IN_USE in resp.text


async def test_an_event_owner_editing_from_the_workspace_is_refused_too(booth):
    await _join(booth)
    url = booth["url"].replace("/admin/", "/workspace/", 1)
    resp = await _edit(booth, {"language_code": "de"}, url=url, cookies=booth["owner_cookies"])
    assert resp.status_code == 409
    assert resp.json()["detail"] == IN_USE
    assert await _saved(booth) == ("es", "Spanish")


async def test_code_changes_once_everyone_has_left(booth):
    participant_id = await _join(booth)
    await booths.add_chat_message(booth["booth_id"], participant_id, "old note", "es", booth["channel"])
    await booths.leave_participant(booth["booth_id"], participant_id, "es", booth["channel"])
    resp = await _edit(booth, {"language_code": "de", "language_name": "German"})
    assert resp.status_code == 303
    assert await _saved(booth) == ("de", "German")
    # The old code's leftover chat goes with it, so a booth added later with that code starts empty.
    assert booths.get_booth_sync(booth["booth_id"]) is None

    # Nobody has opened it as German: it renames again and no state appears under either code.
    resp = await _edit(booth, {"language_code": "fr"})
    assert resp.status_code == 303
    assert (await _saved(booth))[0] == "fr"
    for code in ("de", "fr"):
        assert booths.get_booth_sync(make_booth_id("conf", booth["room"], code)) is None


async def test_someone_joining_during_the_save_keeps_the_old_booth(booth, monkeypatch):
    participant_id = await _join(booth)
    await booths.leave_participant(booth["booth_id"], participant_id, "es", booth["channel"])
    # Empty when checked, then someone joins before the save finishes.
    monkeypatch.setattr(admin, "_booth_in_use", Mock(side_effect=[False, True]))
    resp = await _edit(booth, {"language_code": "de"})
    assert resp.status_code == 303
    assert booths.get_booth_sync(booth["booth_id"]) is not None


async def test_code_change_on_a_deleted_event_is_not_found(booth):
    async with get_session() as s:
        event = await s.get(Event, booth["event"])
        event.deleted_at = datetime.now(timezone.utc)
    resp = await _edit(booth, {"language_code": "de"})
    assert resp.status_code == 404
    assert (await _saved(booth))[0] == "es"
