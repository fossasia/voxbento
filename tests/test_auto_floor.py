"""Automatic floor audio for listeners on interpreter channels (#1397, part 1).

Covers the room setting, which rooms have floor audio and the listener page payload
(floor_fallback, the floor entry and its label). ``on_air`` is in test_booth_on_air.py.
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import json
import re
from datetime import timedelta

import pytest

from portal.auth import create_user_token
from portal.models import Room

pytestmark = [pytest.mark.usefixtures("auto_csrf"), pytest.mark.anyio]


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def seed():
    """An event with one room and a Thai booth; the room has no floor source yet."""
    from portal.database import (
        create_booth,
        create_event,
        create_room,
        create_user,
        get_session,
        set_event_membership,
        set_room_membership,
    )

    async with get_session() as s:
        event = await create_event(s, slug="floorcon", display_name="FloorCon")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        await create_booth(s, event_id=event.id, room_id=room.id, language_code="th", language_name="Thai")
        owner = await create_user(s, email="floor-owner@example.com", display_name="Owner")
        coordinator = await create_user(s, email="floor-coord@example.com", display_name="Coordinator")
        await set_event_membership(s, user_id=owner.id, event_id=event.id, role="event_owner")
        await set_room_membership(s, user_id=coordinator.id, room_id=room.id, role="room_coordinator")
    return {
        "event_id": event.id,
        "room_id": room.id,
        "owner": {"user_token": create_user_token(user_id=owner.id, email=owner.email)},
        "coordinator": {"user_token": create_user_token(user_id=coordinator.id, email=coordinator.email)},
    }


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _update_room(room_id: int, **fields) -> None:
    from portal.database import get_session

    async with get_session() as s:
        room = await s.get(Room, room_id)
        for name, value in fields.items():
            setattr(room, name, value)


async def _listener_data(cookies: dict | None = None, params: str = "") -> dict:
    async with _client() as c:
        resp = await c.get(f"/listener/floorcon{params}", cookies=cookies)
    assert resp.status_code == 200
    match = re.search(r'<script id="listener-data" type="application/json">(.*?)</script>', resp.text, re.S)
    assert match, "listener page data missing"
    return json.loads(match.group(1))


def _entry(data: dict, code: str) -> dict | None:
    return next((b for b in data["booths"] if b["language_code"] == code), None)


# ── room_has_floor_audio ─────────────────────────────────────────────────────


def _future() -> object:
    from portal.program_ingest import utc_now

    return utc_now() + timedelta(days=1)


def _past() -> object:
    from portal.program_ingest import utc_now

    return utc_now() - timedelta(days=1)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"floor_source": "jitsi_bot"}, False),
        ({"floor_source": "jitsi_bot", "meeting_agent_enabled": True}, True),
        # Guided setup saves a meeting link for every room; only the running agent puts audio on the floor.
        ({"floor_source": "jitsi_bot", "jitsi_url": "https://meet.example.org/Hall"}, False),
        (
            {"floor_source": "jitsi_bot", "jitsi_url": "https://meet.example.org/Hall", "meeting_agent_enabled": True},
            True,
        ),
        ({"floor_source": "program_ingest"}, False),
        ({"floor_source": "program_ingest", "program_key_hash": "h", "program_key_expires_at": "future"}, True),
        ({"floor_source": "program_ingest", "program_key_hash": "h", "program_key_expires_at": "past"}, False),
        (
            {
                "floor_source": "program_ingest",
                "program_session_secret_hash": "s",
                "program_session_secret_expires_at": "future",
            },
            True,
        ),
        ({"floor_source": "program_ingest", "program_session_id": "0b7c6a2e-2f51-4f45-9a43-1f1b0a2d9c11"}, True),
        # RTMPS floors carry AAC, which WebRTC listeners can't play.
        *(
            (
                {
                    "floor_source": "program_ingest",
                    "program_ingest_mode": mode,
                    "program_key_hash": "h",
                    "program_key_expires_at": "future",
                    "program_session_id": "0b7c6a2e-2f51-4f45-9a43-1f1b0a2d9c11",
                },
                False,
            )
            for mode in ("rtmps", "rtmps_relay")
        ),
        (
            {
                "floor_source": "program_ingest",
                "program_ingest_mode": "whip",
                "program_key_hash": "h",
                "program_key_expires_at": "future",
            },
            True,
        ),
        # A program-ingest room's meeting link is not its floor.
        ({"floor_source": "program_ingest", "jitsi_url": "https://meet.example.org/Hall"}, False),
    ],
)
async def test_room_has_floor_audio(fields, expected):
    from portal.program_ingest import room_has_floor_audio

    when = {"future": _future(), "past": _past()}
    room = Room(**{name: when.get(value, value) if isinstance(value, str) else value for name, value in fields.items()})
    assert room_has_floor_audio(room) is expected


# ── Room setting ─────────────────────────────────────────────────────────────


async def test_new_rooms_have_auto_floor_on(seed):
    from portal.database import get_session

    async with get_session() as s:
        assert (await s.get(Room, seed["room_id"])).auto_floor_enabled is True


async def test_owner_turns_auto_floor_off_and_on(seed):
    from portal.database import get_session

    edit = f"/workspace/events/{seed['event_id']}/rooms/{seed['room_id']}/edit"
    page = f"/workspace/events/{seed['event_id']}/rooms/{seed['room_id']}/"
    general = {"form_section": "general", "display_name": "Main Hall", "audio_delay_ms": "250"}
    async with _client() as c:
        resp = await c.post(edit, data=general, cookies=seed["owner"], follow_redirects=False)
        assert resp.status_code == 303
        async with get_session() as s:
            room = await s.get(Room, seed["room_id"])
            assert room.auto_floor_enabled is False
            assert room.audio_delay_ms == 250
        shown = await c.get(page, cookies=seed["owner"])
        assert 'name="auto_floor_enabled" >' in shown.text

        resp = await c.post(
            edit, data={**general, "auto_floor_enabled": "on"}, cookies=seed["owner"], follow_redirects=False
        )
        assert resp.status_code == 303
        async with get_session() as s:
            assert (await s.get(Room, seed["room_id"])).auto_floor_enabled is True
        shown = await c.get(page, cookies=seed["owner"])
        assert 'name="auto_floor_enabled" checked>' in shown.text


async def test_other_sections_leave_auto_floor_alone(seed):
    from portal.database import get_session

    edit = f"/workspace/events/{seed['event_id']}/rooms/{seed['room_id']}/edit"
    async with _client() as c:
        resp = await c.post(edit, data={"form_section": "monitor"}, cookies=seed["owner"], follow_redirects=False)
    assert resp.status_code == 303
    async with get_session() as s:
        assert (await s.get(Room, seed["room_id"])).auto_floor_enabled is True


async def test_coordinator_cannot_change_auto_floor(seed):
    from portal.database import get_session

    edit = f"/workspace/events/{seed['event_id']}/rooms/{seed['room_id']}/edit"
    async with _client() as c:
        resp = await c.post(
            edit,
            data={"form_section": "general", "display_name": "Main Hall", "audio_delay_ms": "0"},
            cookies=seed["coordinator"],
            follow_redirects=False,
        )
    assert resp.status_code in {302, 303, 403}
    async with get_session() as s:
        assert (await s.get(Room, seed["room_id"])).auto_floor_enabled is True


# ── Listener page payload ────────────────────────────────────────────────────


async def test_booth_gets_floor_fallback_when_the_room_has_floor_audio(seed, monkeypatch):
    from portal.config import settings
    from portal.routers import listener

    ensured = []

    async def ensure(channel_id):
        ensured.append(channel_id)

    monkeypatch.setattr(listener, "_ensure_mediamtx_path", ensure)
    await _update_room(seed["room_id"], meeting_agent_enabled=True)
    data = await _listener_data(seed["owner"])

    thai = _entry(data, "th")
    floor_path = f"floorcon/{seed['room_id']}/floor"
    assert thai["floor_fallback"] == {
        "whep_url": f"{settings.mediamtx_whip_base}/{floor_path}/whep",
        "booth_id": f"floorcon-{seed['room_id']}-floor",
        "channel_id": floor_path,
    }
    floor = _entry(data, "floor")
    assert floor is not None, "the floor is listed with captions off once it can carry audio"
    assert floor["whep_url"] == thai["floor_fallback"]["whep_url"]
    assert floor["language_name"] == "Floor Audio (Original)"
    assert floor["spoken_language_code"] is None
    assert floor_path in ensured


@pytest.mark.parametrize(
    ("spoken", "label", "code"),
    [("en", "English (original)", "en"), ("th", "Thai (original)", "th"), ("zz", "Floor Audio (Original)", None)],
)
async def test_floor_is_labelled_with_the_spoken_language(seed, spoken, label, code):
    await _update_room(seed["room_id"], meeting_agent_enabled=True, floor_language_code=spoken)
    floor = _entry(await _listener_data(seed["owner"]), "floor")
    assert floor["language_name"] == label
    assert floor["spoken_language_code"] == code


async def test_auto_floor_off_sends_no_fallback(seed):
    await _update_room(seed["room_id"], meeting_agent_enabled=True, auto_floor_enabled=False)
    data = await _listener_data(seed["owner"])
    assert _entry(data, "th")["floor_fallback"] is None
    assert _entry(data, "floor") is not None, "the floor stays a channel of its own"


async def test_room_without_floor_source(seed):
    data = await _listener_data(seed["owner"])
    assert _entry(data, "th")["floor_fallback"] is None
    assert _entry(data, "floor") is None

    await _update_room(seed["room_id"], floor_transcription_enabled=True)
    data = await _listener_data(seed["owner"])
    assert _entry(data, "th")["floor_fallback"] is None, "floor captions alone are not floor audio"
    assert _entry(data, "floor") is not None, "floor captions still list the floor as before"


async def test_meeting_link_without_the_agent_is_not_floor_audio(seed):
    from portal.room_setup import default_jitsi_url

    await _update_room(seed["room_id"], jitsi_url=default_jitsi_url("floorcon", "Main Hall"), floor_language_code="en")
    data = await _listener_data(seed["owner"])
    assert _entry(data, "th")["floor_fallback"] is None
    assert _entry(data, "floor") is None, "a saved meeting link alone does not list the floor"

    await _update_room(seed["room_id"], floor_transcription_enabled=True)
    data = await _listener_data(seed["owner"])
    assert _entry(data, "floor") is not None, "floor captions still list the floor"
    assert _entry(data, "th")["floor_fallback"] is None


async def test_program_ingest_floor_gets_fallback_without_creating_the_path(seed, monkeypatch):
    from portal.routers import listener

    ensured = []

    async def ensure(channel_id):
        ensured.append(channel_id)

    monkeypatch.setattr(listener, "_ensure_mediamtx_path", ensure)
    await _update_room(
        seed["room_id"], floor_source="program_ingest", program_key_hash="h", program_key_expires_at=_future()
    )
    data = await _listener_data(seed["owner"])
    assert _entry(data, "th")["floor_fallback"]["channel_id"] == f"floorcon/{seed['room_id']}/floor"
    assert ensured == [f"floorcon/{seed['room_id']}/th"], "program ingest owns its floor path"


async def test_join_code_event_still_requires_the_code(seed):
    from portal.database import get_session
    from portal.models import Event

    async with get_session() as s:
        event = await s.get(Event, seed["event_id"])
        event.listener_join_code = "ROOM42"
    await _update_room(seed["room_id"], meeting_agent_enabled=True)

    async with _client() as c:
        gated = await c.get("/listener/floorcon")
    assert "listener-data" not in gated.text
    assert "Enter the join code" in gated.text

    data = await _listener_data(params="?code=ROOM42")
    assert _entry(data, "th")["floor_fallback"] is not None


# ── Embed ────────────────────────────────────────────────────────────────────


async def _embed_config(room_id: int, language: str) -> dict:
    from portal.auth import create_embed_token

    async with _client() as c:
        resp = await c.get(
            f"/embed/floorcon/{room_id}/{language}", params={"token": create_embed_token(event_slug="floorcon")}
        )
    assert resp.status_code == 200, resp.text
    match = re.search(r'<script type="application/json" id="embed-config">(.*?)</script>', resp.text, re.S)
    assert match, "embed config missing"
    return json.loads(match.group(1))


async def test_embed_gets_the_same_floor_fallback(seed):
    from portal.config import settings

    room_id = seed["room_id"]
    await _update_room(room_id, meeting_agent_enabled=True)
    config = await _embed_config(room_id, "th")
    assert config["floor_fallback"] == {
        "whep_url": f"{settings.mediamtx_whip_base}/floorcon/{room_id}/floor/whep",
        "booth_id": f"floorcon-{room_id}-floor",
        "channel_id": f"floorcon/{room_id}/floor",
    }
    assert (await _embed_config(room_id, "floor"))["floor_fallback"] is None, "the floor channel needs no fallback"

    await _update_room(room_id, auto_floor_enabled=False)
    assert (await _embed_config(room_id, "th"))["floor_fallback"] is None


async def test_embed_scripts_stay_classic_for_sandboxed_iframes(seed):
    """A module script needs CORS, so a sandbox="allow-scripts" iframe (opaque origin) would never run it."""
    from portal.auth import create_embed_token

    async with _client() as c:
        resp = await c.get(
            f"/embed/floorcon/{seed['room_id']}/th", params={"token": create_embed_token(event_slug="floorcon")}
        )
    assert resp.status_code == 200, resp.text
    assert 'type="module"' not in resp.text
    channel_source = resp.text.index('<script src="/static/js/channel-source.js')
    assert channel_source < resp.text.index('<script src="/static/js/embed-player.js')


async def test_embed_without_floor_audio_has_no_fallback(seed):
    await _update_room(seed["room_id"], jitsi_url="https://meet.example.org/Hall")
    assert (await _embed_config(seed["room_id"], "th"))["floor_fallback"] is None


# ── Original channel follows a two-way booth (#1397, part 2) ─────────────────


async def _make_thai_two_way(room_id: int) -> None:
    from sqlalchemy import select

    from portal.database import get_session
    from portal.models import DBBooth

    async with get_session() as s:
        booth = (await s.execute(select(DBBooth).where(DBBooth.room_id == room_id))).scalar_one()
        booth.return_language_code = "en"


async def test_original_channel_follows_a_two_way_booth(seed, monkeypatch):
    from portal.config import settings
    from portal.routers import listener

    ensured = []

    async def ensure(channel_id):
        ensured.append(channel_id)

    monkeypatch.setattr(listener, "_ensure_mediamtx_path", ensure)
    room_id = seed["room_id"]
    await _update_room(room_id, meeting_agent_enabled=True, floor_language_code="en")
    data = await _listener_data(seed["owner"])
    assert _entry(data, "floor")["return_whep_url"] is None, "no two-way booth, plain floor"
    assert f"floorcon/{room_id}/en" not in ensured

    await _make_thai_two_way(room_id)
    data = await _listener_data(seed["owner"])
    assert _entry(data, "floor")["return_whep_url"] == f"{settings.mediamtx_whip_base}/floorcon/{room_id}/en/whep"
    assert f"floorcon/{room_id}/en" in ensured, "the return path exists before anyone reads it"
    assert "return_whep_url" not in _entry(data, "th")

    # A return language that no longer matches the spoken language is not followed.
    await _update_room(room_id, floor_language_code="de")
    assert _entry(await _listener_data(seed["owner"]), "floor")["return_whep_url"] is None


async def test_embed_original_channel_follows_a_two_way_booth(seed):
    from portal.config import settings

    room_id = seed["room_id"]
    await _update_room(room_id, meeting_agent_enabled=True, floor_language_code="en")
    assert (await _embed_config(room_id, "floor"))["return_whep_url"] is None
    await _make_thai_two_way(room_id)
    config = await _embed_config(room_id, "floor")
    assert config["return_whep_url"] == f"{settings.mediamtx_whip_base}/floorcon/{room_id}/en/whep"
    assert config["floor_fallback"] is None
    assert (await _embed_config(room_id, "th"))["return_whep_url"] is None, "only the original channel follows"
