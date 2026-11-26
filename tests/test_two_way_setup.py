"""Setting up two-way booths (#1397): room setup, the booth page, the APIs and Mission Control.

A two-way booth's return language is always the room's spoken language. Wherever it can be
set, the same rules apply, and a booth interpreting into it right now keeps it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.datastructures import FormData

from fastapi_app import app
from portal import room_setup
from portal.auth import create_user_token, hash_password
from portal.config import settings
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
    set_event_membership,
)
from portal.globals import booths
from portal.models import DBBooth, DeveloperAccount, OAuthClient, OAuthToken, Room, User
from portal.roles import EVENT_OWNER, INTERPRETER
from portal.room_setup import AI_TEXT, engine_catalog, load_snapshot, parse_plan

pytestmark = pytest.mark.usefixtures("auto_csrf")

ORIGIN = {"origin": "http://test"}
SLUG = "twowaysetup"
ACCESS_TOKEN = "two-way-api-token"


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _form(**fields: object) -> FormData:
    items: list[tuple[str, str]] = []
    for key, value in fields.items():
        for v in value if isinstance(value, list) else [value]:
            items.append((key, str(v)))
    return FormData(items)


@pytest.fixture(autouse=True)
def installed_optional_engines(monkeypatch):
    monkeypatch.setattr(room_setup, "find_spec", lambda name: object())


@pytest.fixture
async def world():
    """An English room with a one-way Thai booth, an admin and an API token for the event."""
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as s:
        admin = User(email="tw-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        s.add(admin)
        owner = await create_user(
            s, email="tw-owner@example.com", display_name="Owner", password_hash=hash_password("pw123456")
        )
        event = await create_event(s, slug=SLUG, display_name="Two Way Setup")
        await set_event_membership(s, user_id=owner.id, event_id=event.id, role=EVENT_OWNER)
        room = await create_room(s, event_id=event.id, display_name="Main", eventyay_room_id="main")
        room.floor_language_code = "en"
        thai = await create_booth(s, event_id=event.id, room_id=room.id, language_code="th", language_name="Thai")
        account = DeveloperAccount(user_id=owner.id, status="approved")
        s.add(account)
        await s.flush()
        client = OAuthClient(developer_account_id=account.id, client_id="tw-client", name="TW")
        s.add(client)
        await s.flush()
        s.add(
            OAuthToken(
                client_id=client.id,
                user_id=owner.id,
                event_id=event.id,
                scopes=["rooms:write", "booths:read", "booths:write"],
                access_token_hash=hashlib.sha256(ACCESS_TOKEN.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await s.flush()
        ids = {"event": event.id, "room": room.id, "thai": thai.id, "admin": admin.id}
    ids["cookie"] = {"user_token": create_user_token(user_id=ids["admin"], email="tw-admin@example.com", is_admin=True)}
    yield ids
    for code in ("th", "ja", "en"):
        await booths.remove_booth(SLUG, ids["room"], code)
    await dispose()


async def _returns(room_id: int) -> dict[str, str | None]:
    async with get_session() as s:
        return {
            b.language_code: b.return_language_code
            for b in (await s.scalars(select(DBBooth).where(DBBooth.room_id == room_id))).all()
        }


async def _set_return(language_code: str, value: str | None) -> None:
    async with get_session() as s:
        booth = await s.scalar(select(DBBooth).where(DBBooth.language_code == language_code))
        booth.return_language_code = value


async def _outputting_thai(world) -> str:
    """Put the Thai booth on its return channel, as the switch would."""
    booth_id = f"{SLUG}-{world['room']}-th"
    participant, _ = await booths.join_participant(
        booth_id=booth_id,
        display_name="Ana",
        role="interpreter",
        language="Thai",
        channel_id=f"{SLUG}/{world['room']}/th",
        room_id=world["room"],
        connection_id="ana",
    )
    await booths.set_output(
        booth_id,
        participant.participant_id,
        "return",
        can_reassign=False,
        return_language_code="en",
        return_language="English",
    )
    return booth_id


def _event():
    from portal.models import Event

    return Event(slug="x", display_name="X", transcription_api_enabled=False)


# ── The plan ────────────────────────────────────────────────────────────────


def test_parse_plan_reads_two_way_only_for_interpreters_and_the_current_spoken_language():
    catalog = engine_catalog(_event())
    plan, errors = parse_plan(
        _form(
            display_name="Hall",
            spoken_language="en",
            languages=["th", "ja", "fr"],
            delivery_th=INTERPRETER,
            two_way_th="en",
            delivery_ja=INTERPRETER,
            two_way_ja="fr",  # ticked while the spoken language was French
            delivery_fr=AI_TEXT,
            two_way_fr="en",  # AI languages have no booth
        ),
        catalog,
    )
    assert errors == ()
    assert plan.two_way == ("th",)


def test_parse_plan_refuses_two_way_next_to_a_spoken_language_booth():
    catalog = engine_catalog(_event())
    plan, errors = parse_plan(
        _form(
            display_name="Hall",
            spoken_language="en",
            languages=["en", "th"],
            delivery_en=INTERPRETER,
            delivery_th=INTERPRETER,
            two_way_th="en",
        ),
        catalog,
        existing_booth_codes=["en"],
    )
    assert any("already has its own English booth" in error for error in errors)


def test_parse_plan_keeps_a_booth_two_way_while_it_interprets_into_its_return():
    catalog = engine_catalog(_event())
    form = {"display_name": "Hall", "spoken_language": "en", "languages": ["th"], "delivery_th": INTERPRETER}
    _, errors = parse_plan(_form(**form), catalog, outputting=["th"])
    assert errors == (
        "The Thai booth is interpreting into another language right now. "
        "Ask its interpreter to switch back to Thai, then save again.",
    )
    _, errors = parse_plan(_form(**form, two_way_th="en"), catalog, outputting=["th"])
    assert errors == ()


@pytest.mark.anyio
async def test_snapshot_shows_two_way_booths(world):
    await _set_return("th", "en")
    async with get_session() as s:
        room = await s.get(Room, world["room"])
        snapshot = await load_snapshot(s, room, engine_catalog(_event()))
        assert snapshot.plan.two_way == ("th",)
        room.floor_language_code = None
        assert (await load_snapshot(s, room, engine_catalog(_event()))).plan.two_way == ()


# ── Room setup page ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_room_setup_makes_a_booth_two_way_and_back(world):
    url = f"/admin/events/{world['event']}/rooms/{world['room']}/setup"
    form = {"display_name": "Main", "spoken_language": "en", "languages": ["th"], "delivery_th": INTERPRETER}
    async with _client() as c:
        page = await c.get(url, cookies=world["cookie"])
        assert 'name="two_way_th" value="en"' in page.text
        assert "Two-way with <span data-rs-two-way-name>English</span>" in page.text

        response = await c.post(url, data={**form, "two_way_th": "en"}, headers=ORIGIN, cookies=world["cookie"])
        assert response.status_code == 303
        assert "one_way" not in response.headers["location"]
        assert await _returns(world["room"]) == {"th": "en"}
        page = await c.get(url, cookies=world["cookie"])
        assert 'name="two_way_th" value="en" checked data-rs-was-two-way' in page.text

        response = await c.post(url, data=form, headers=ORIGIN, cookies=world["cookie"])
        assert "one_way=th" in response.headers["location"]
        assert await _returns(world["room"]) == {"th": None}
        notice = await c.get(response.headers["location"], cookies=world["cookie"])
        assert "Thai is no longer two-way" in notice.text


@pytest.mark.anyio
async def test_changing_the_spoken_language_in_setup_drops_two_way(world):
    await _set_return("th", "en")
    url = f"/admin/events/{world['event']}/rooms/{world['room']}/setup"
    async with _client() as c:
        # The box was shown for English; the room now speaks French.
        response = await c.post(
            url,
            data={
                "display_name": "Main",
                "spoken_language": "fr",
                "languages": ["th"],
                "delivery_th": INTERPRETER,
                "two_way_th": "en",
            },
            headers=ORIGIN,
            cookies=world["cookie"],
        )
    assert "one_way=th" in response.headers["location"]
    assert await _returns(world["room"]) == {"th": None}


@pytest.mark.anyio
async def test_room_setup_refuses_while_the_booth_interprets_into_its_return(world):
    await _set_return("th", "en")
    booth_id = await _outputting_thai(world)
    url = f"/admin/events/{world['event']}/rooms/{world['room']}/setup"
    async with _client() as c:
        response = await c.post(
            url,
            data={"display_name": "Main", "spoken_language": "fr", "languages": ["th"], "delivery_th": INTERPRETER},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
    assert response.status_code == 400
    assert "The Thai booth is interpreting into another language right now" in response.text
    assert await _returns(world["room"]) == {"th": "en"}
    assert booths.get_booth_sync(booth_id).output_language_code == "en"


@pytest.mark.anyio
async def test_room_setup_tells_a_live_booth_its_new_return_language(world):
    booth_id = f"{SLUG}-{world['room']}-th"
    await booths.join_participant(
        booth_id=booth_id,
        display_name="Ana",
        role="interpreter",
        language="Thai",
        channel_id=f"{SLUG}/{world['room']}/th",
        room_id=world["room"],
        connection_id="ana",
    )
    url = f"/admin/events/{world['event']}/rooms/{world['room']}/setup"
    async with _client() as c:
        await c.post(
            url,
            data={
                "display_name": "Main",
                "spoken_language": "en",
                "languages": ["th"],
                "delivery_th": INTERPRETER,
                "two_way_th": "en",
            },
            headers=ORIGIN,
            cookies=world["cookie"],
        )
    assert booths.get_booth_sync(booth_id).return_language_code == "en"
    assert booths.get_booth_sync(booth_id).return_language == "English"


@pytest.mark.anyio
async def test_room_setup_form_renders_the_two_way_template_row(world):
    async with _client() as c:
        page = await c.get(f"/admin/events/{world['event']}/rooms/new", cookies=world["cookie"])
    template = re.search(r'<template id="rs-delivery-row-template">(.*?)</template>', page.text, re.S).group(1)
    assert "data-rs-two-way" in template and "data-rs-two-way-name" in template


# ── Room settings form (spoken language) ────────────────────────────────────


@pytest.mark.anyio
async def test_room_spoken_language_edit_drops_two_way(world):
    await _set_return("th", "en")
    async with _client() as c:
        response = await c.post(
            f"/admin/events/{world['event']}/rooms/{world['room']}/edit",
            data={"display_name": "Main", "floor_language_code": "fr"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/rooms/{world['room']}/?one_way=th")
    assert await _returns(world["room"]) == {"th": None}
    async with _client() as c:
        page = await c.get(response.headers["location"], cookies=world["cookie"])
        assert "Thai is no longer two-way" in page.text
        # Saving again without a spoken language change says nothing about two-way.
        response = await c.post(
            f"/admin/events/{world['event']}/rooms/{world['room']}/edit",
            data={"display_name": "Main", "floor_language_code": "fr"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
    assert response.headers["location"].endswith(f"/rooms/{world['room']}/")


@pytest.mark.anyio
async def test_room_spoken_language_edit_refused_while_outputting(world):
    await _set_return("th", "en")
    await _outputting_thai(world)
    async with _client() as c:
        response = await c.post(
            f"/admin/events/{world['event']}/rooms/{world['room']}/edit",
            data={"display_name": "Main", "floor_language_code": "fr"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
    assert response.status_code == 409
    assert "The Thai booth is interpreting into English right now" in response.text
    async with get_session() as s:
        assert (await s.get(Room, world["room"])).floor_language_code == "en"
    assert await _returns(world["room"]) == {"th": "en"}


# ── Booth settings ──────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_booth_page_offers_two_way_and_saves_it(world):
    booth_url = f"/admin/events/{world['event']}/rooms/{world['room']}/booths/{world['thai']}"
    async with _client() as c:
        page = await c.get(f"{booth_url}/", cookies=world["cookie"])
        assert "Two-way with English" in page.text and 'name="two_way_shown"' in page.text

        response = await c.post(
            f"{booth_url}/edit",
            data={"language_name": "Thai", "language_code": "th", "two_way_shown": "1", "two_way": "en"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
        assert response.status_code == 303
        assert await _returns(world["room"]) == {"th": "en"}
        page = await c.get(f"{booth_url}/", cookies=world["cookie"])
        assert "Thai (th), two-way with English" in page.text

        # An older form without the choice leaves it alone.
        await c.post(f"{booth_url}/edit", data={"language_name": "Thai"}, headers=ORIGIN, cookies=world["cookie"])
        assert await _returns(world["room"]) == {"th": "en"}

        await c.post(
            f"{booth_url}/edit",
            data={"language_name": "Thai", "two_way_shown": "1"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
        assert await _returns(world["room"]) == {"th": None}


@pytest.mark.anyio
async def test_booth_page_hides_two_way_without_a_spoken_language(world):
    async with get_session() as s:
        (await s.get(Room, world["room"])).floor_language_code = None
    async with _client() as c:
        page = await c.get(
            f"/admin/events/{world['event']}/rooms/{world['room']}/booths/{world['thai']}/", cookies=world["cookie"]
        )
    assert "Two-way with" not in page.text


@pytest.mark.anyio
async def test_a_spoken_language_booth_is_never_offered_two_way(world):
    async with get_session() as s:
        english = await create_booth(
            s, event_id=world["event"], room_id=world["room"], language_code="en", language_name="English"
        )
        english_id = english.id
    async with _client() as c:
        page = await c.get(
            f"/admin/events/{world['event']}/rooms/{world['room']}/booths/{english_id}/", cookies=world["cookie"]
        )
    assert page.status_code == 200
    assert "Two-way with" not in page.text


@pytest.mark.anyio
async def test_booth_language_edit_while_two_way(world):
    await _set_return("th", "en")
    booth_url = f"/admin/events/{world['event']}/rooms/{world['room']}/booths/{world['thai']}"
    async with _client() as c:
        # Its own language can't become its return language.
        response = await c.post(
            f"{booth_url}/edit", data={"language_code": "en"}, headers=ORIGIN, cookies=world["cookie"]
        )
        assert response.status_code == 400
        assert "already interprets into English" in response.text
        # Another language keeps the two-way setting.
        response = await c.post(
            f"{booth_url}/edit", data={"language_code": "lo"}, headers=ORIGIN, cookies=world["cookie"]
        )
        assert response.status_code == 303
    assert await _returns(world["room"]) == {"lo": "en"}


@pytest.mark.anyio
async def test_booth_edit_refused_while_it_interprets_into_its_return(world):
    await _set_return("th", "en")
    await _outputting_thai(world)
    booth_url = f"/admin/events/{world['event']}/rooms/{world['room']}/booths/{world['thai']}"
    async with _client() as c:
        response = await c.post(
            f"{booth_url}/edit",
            data={"language_name": "Thai", "two_way_shown": "1"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
        assert response.status_code == 409
        assert "is interpreting into English right now" in response.text
        # Renaming it is fine.
        response = await c.post(
            f"{booth_url}/edit",
            data={"language_name": "Thai (Bangkok)", "two_way_shown": "1", "two_way": "en"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
        assert response.status_code == 303
    assert await _returns(world["room"]) == {"th": "en"}


@pytest.mark.anyio
async def test_booth_two_way_refused_next_to_a_spoken_language_booth(world):
    async with get_session() as s:
        await create_booth(
            s, event_id=world["event"], room_id=world["room"], language_code="en", language_name="English"
        )
    async with _client() as c:
        response = await c.post(
            f"/admin/events/{world['event']}/rooms/{world['room']}/booths/{world['thai']}/edit",
            data={"two_way_shown": "1", "two_way": "en"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
    assert response.status_code == 400
    assert "already has its own English booth" in response.text


@pytest.mark.anyio
async def test_a_booth_cannot_take_a_language_another_booth_returns_into(world):
    await _set_return("th", "en")
    async with get_session() as s:
        japanese = await create_booth(
            s, event_id=world["event"], room_id=world["room"], language_code="ja", language_name="Japanese"
        )
        japanese_id = japanese.id
    async with _client() as c:
        response = await c.post(
            f"/admin/events/{world['event']}/rooms/{world['room']}/booths/{japanese_id}/edit",
            data={"language_code": "en"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
    assert response.status_code == 400
    assert "The Thai booth interprets back into English" in response.text


@pytest.mark.anyio
async def test_adding_a_booth_for_a_return_language_is_refused(world):
    await _set_return("th", "en")
    async with _client() as c:
        response = await c.post(
            f"/admin/events/{world['event']}/rooms/{world['room']}/booths/",
            data={"language_code": "en", "language_name": "English"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
        assert response.headers["location"].endswith("?error=return_language")
        page = await c.get(response.headers["location"], cookies=world["cookie"])
        assert "already interprets into that language" in page.text

        response = await c.post(
            f"/admin/events/{world['event']}/setup/booths",
            data={"room_id": str(world["room"]), "language_code": "en"},
            headers=ORIGIN,
            cookies=world["cookie"],
        )
        assert "error=return_language" in response.headers["location"]
        page = await c.get(response.headers["location"], cookies=world["cookie"])
        assert "already interprets into that language" in page.text
    assert await _returns(world["room"]) == {"th": "en"}


# ── Mission Control ─────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_mission_control_gets_each_booths_direction(world):
    await _set_return("th", "en")
    async with _client() as c:
        page = await c.get(f"/mission-control/{SLUG}/", cookies=world["cookie"])
    assert '"return_language_code": "en"' in page.text
    assert '"return_language": "English"' in page.text
    assert f'"output_mediamtx_path": "{SLUG}/{world["room"]}/th"' in page.text

    # A booth already in memory learns its return language from the page load.
    booth_id = await _outputting_thai(world)
    booths.get_booth_sync(booth_id).return_language = ""
    async with _client() as c:
        page = await c.get(f"/mission-control/{SLUG}/", cookies=world["cookie"])
    assert booths.get_booth_sync(booth_id).return_language == "English"
    assert '"output_language_code": "en"' in page.text


# ── Booth page monitor ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_booth_page_never_offers_its_own_channel_as_relay(world):
    async with get_session() as s:
        room = await s.get(Room, world["room"])
        room.relay_booth_id = world["thai"]
        japanese = await create_booth(
            s, event_id=world["event"], room_id=world["room"], language_code="ja", language_name="Japanese"
        )
        await s.flush()
    async with _client() as c:
        own = await c.get(f"/interpreter/{SLUG}/{world['room']}/th", cookies=world["cookie"])
        other = await c.get(f"/interpreter/{SLUG}/{world['room']}/ja", cookies=world["cookie"])
    assert "value='relay'" not in own.text
    assert "value='relay'" in other.text
    assert "id='output-switch'" in own.text
    assert japanese


# ── API v1 ──────────────────────────────────────────────────────────────────


def _auth():
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


@pytest.mark.anyio
async def test_api_creates_a_two_way_booth_and_reads_it_back(world):
    base = f"/api/v1/events/{SLUG}/rooms/{world['room']}/booths"
    async with _client() as c:
        response = await c.post(f"{base}/ja", json={"return_language_code": "EN"}, headers=_auth())
        assert response.status_code == 200
        assert response.json()["return_language_code"] == "en"
        assert (await c.get(f"{base}/ja", headers=_auth())).json()["return_language_code"] == "en"
        listed = {b["language_code"]: b["return_language_code"] for b in (await c.get(base, headers=_auth())).json()}
        assert listed == {"th": None, "ja": "en"}
        # Without a body it is one-way, as before, and other keys are still ignored.
        response = await c.post(f"{base}/de", headers=_auth())
        assert response.status_code == 200 and response.json()["return_language_code"] is None
        response = await c.post(f"{base}/fr", json={"language_name": "French"}, headers=_auth())
        assert response.status_code == 200 and response.json()["return_language_code"] is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"return_language_code": "fr"}, 422, "back into the room's spoken language"),
        ({"return_language_code": "floor"}, 422, None),
        ({"return_language_code": "eng"}, 422, None),
    ],
)
async def test_api_refuses_a_bad_return_language(world, body, status, detail):
    async with _client() as c:
        response = await c.post(f"/api/v1/events/{SLUG}/rooms/{world['room']}/booths/ja", json=body, headers=_auth())
    assert response.status_code == status
    if detail:
        assert detail in response.json()["detail"]
    assert await _returns(world["room"]) == {"th": None}


@pytest.mark.anyio
async def test_api_refuses_a_booth_for_a_language_another_booth_returns_into(world):
    await _set_return("th", "en")
    async with _client() as c:
        response = await c.post(f"/api/v1/events/{SLUG}/rooms/{world['room']}/booths/en", headers=_auth())
        assert response.status_code == 409
        response = await c.put(
            f"/api/v1/events/{SLUG}/rooms/main", json={"target_languages": ["th", "en"]}, headers=_auth()
        )
        assert response.status_code == 409
        assert "The Thai booth interprets back into English" in response.json()["detail"]
        # Dropping Thai frees English.
        response = await c.put(f"/api/v1/events/{SLUG}/rooms/main", json={"target_languages": ["en"]}, headers=_auth())
        assert response.status_code == 200
    assert await _returns(world["room"]) == {"en": None}


@pytest.mark.anyio
async def test_api_spoken_language_change_drops_two_way(world):
    await _set_return("th", "en")
    booth_id = f"{SLUG}-{world['room']}-th"
    await booths.join_participant(
        booth_id=booth_id,
        display_name="Ana",
        role="interpreter",
        language="Thai",
        channel_id=f"{SLUG}/{world['room']}/th",
        room_id=world["room"],
        connection_id="ana",
    )
    booths.set_return_language_sync(booth_id, "en", "English")
    async with _client() as c:
        response = await c.put(f"/api/v1/events/{SLUG}/rooms/main", json={"source_language": "en"}, headers=_auth())
        assert response.status_code == 200
        assert await _returns(world["room"]) == {"th": "en"}
        response = await c.put(f"/api/v1/events/{SLUG}/rooms/main", json={"source_language": "fr"}, headers=_auth())
        assert response.status_code == 200
    assert await _returns(world["room"]) == {"th": None}
    assert booths.get_booth_sync(booth_id).return_language_code is None


@pytest.mark.anyio
async def test_api_spoken_language_change_refused_while_outputting(world):
    await _set_return("th", "en")
    await _outputting_thai(world)
    async with _client() as c:
        response = await c.put(f"/api/v1/events/{SLUG}/rooms/main", json={"source_language": "fr"}, headers=_auth())
    assert response.status_code == 409
    assert "interpreting into English right now" in response.json()["detail"]
    async with get_session() as s:
        assert (await s.get(Room, world["room"])).floor_language_code == "en"


@pytest.mark.anyio
async def test_legacy_booth_api_refuses_a_return_language(world, monkeypatch):
    await _set_return("th", "en")
    monkeypatch.setattr(settings, "booth_access_token", "")
    monkeypatch.setattr(settings, "debug", True)
    async with _client() as c:
        response = await c.post(
            f"/api/events/{SLUG}/booths", json={"language_code": "en", "room_id": world["room"], "language": "English"}
        )
    assert response.status_code == 409
    assert "The Thai booth interprets back into English" in response.json()["detail"]
