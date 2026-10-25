"""Interpreter, Mission Control and room pages: shared header, names and structure."""

from __future__ import annotations

import json
import os
import re

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
    set_booth_membership,
    set_event_membership,
    set_room_membership,
)
from portal.roles import EVENT_OWNER, INTERPRETER, ROOM_COORDINATOR


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def world():
    async with get_session() as s:
        event = await create_event(s, slug="summit", display_name="Summit 2026")
        hall = await create_room(s, event_id=event.id, display_name="Main Hall")
        side = await create_room(s, event_id=event.id, display_name="Side Room")
        hall.floor_transcription_enabled = True
        hall.floor_transcription_provider = "local"
        de = await create_booth(s, event_id=event.id, room_id=hall.id, language_code="de", language_name="Deutsch")
        await create_booth(s, event_id=event.id, room_id=side.id, language_code="fr", language_name="French")
        people = {}
        for name in ("owner", "coord", "interp", "nobody"):
            people[name] = await create_user(s, email=f"{name}@example.org", display_name=name.title())
        await set_event_membership(s, user_id=people["owner"].id, event_id=event.id, role=EVENT_OWNER)
        await set_room_membership(s, user_id=people["coord"].id, room_id=hall.id, role=ROOM_COORDINATOR)
        await set_booth_membership(s, user_id=people["interp"].id, booth_id=de.id, role=INTERPRETER)
        ids = {"event": event.id, "hall": hall.id, "side": side.id}
        cookies = {
            name: {"user_token": create_user_token(user_id=user.id, email=user.email)} for name, user in people.items()
        }
    return {**ids, "cookies": cookies}


def menu_labels(html: str) -> list[str]:
    return re.findall(r'class="app-user-item[^"]*" href="[^"]+"[^>]*>\s*<svg.*?</svg>\s*<span>([^<]+)</span>', html, re.S)


@pytest.mark.anyio
async def test_my_booths_uses_the_shared_header_and_menu(world):
    async with client() as http:
        res = await http.get("/interpreter", cookies=world["cookies"]["interp"])
    assert res.status_code == 200
    assert 'class="app-header"' in res.text
    assert '<span class="app-area app-area--booths">My booths</span>' in res.text
    assert "Back to Home" not in res.text
    assert menu_labels(res.text) == ["My events", "My booths", "Account", "Logout"]
    assert 'href="/interpreter/summit/' in res.text and "Deutsch" in res.text


@pytest.mark.anyio
async def test_my_booths_entry_only_for_interpreters(world):
    async with client() as http:
        owner = await http.get("/workspace/", cookies=world["cookies"]["owner"])
        coord = await http.get("/workspace/", cookies=world["cookies"]["coord"])
    assert "My booths" not in menu_labels(owner.text)
    assert "My booths" not in menu_labels(coord.text)


@pytest.mark.anyio
async def test_empty_lobby_says_how_to_get_a_booth(world):
    async with client() as http:
        res = await http.get("/interpreter", cookies=world["cookies"]["nobody"])
    assert "You're not in any booth yet" in res.text
    assert "Team page" in res.text


@pytest.mark.anyio
async def test_booth_names_the_language_room_and_event_and_has_the_menu(world):
    async with client() as http:
        res = await http.get(f"/interpreter/summit/{world['hall']}/de", cookies=world["cookies"]["interp"])
    assert res.status_code == 200
    assert "Deutsch booth" in res.text
    assert "Main Hall · Summit 2026" in res.text
    assert "data-user-menu" in res.text
    assert "id='leave-booth'" in res.text
    # A work screen: no public site footer.
    assert "portal-footer" not in res.text
    assert "<title>Deutsch booth — Summit 2026</title>" in res.text


@pytest.mark.anyio
async def test_mission_control_passes_rooms_in_data_not_an_inline_script(world):
    async with client() as http:
        res = await http.get("/mission-control/summit/", cookies=world["cookies"]["coord"])
    assert res.status_code == 200
    assert "MISSION_CONTROL_CONFIG" not in res.text
    assert '<span class="app-area app-area--mission-control">Mission Control</span>' in res.text
    config = json.loads(re.search(r"data-config='([^']*)'", res.text).group(1).replace("&#34;", '"').replace("&#39;", "'"))
    # A Main Hall coordinator sees only Main Hall, with its floor-caption setting.
    assert [room["name"] for room in config["rooms"]] == ["Main Hall"]
    assert config["rooms"][0]["floor_captions"] is True
    assert [b["language_code"] for b in config["booths"]] == ["de"]


@pytest.mark.anyio
async def test_room_page_is_split_into_tabs_for_owners(world):
    async with client() as http:
        owner = await http.get(f"/workspace/events/{world['event']}/rooms/{world['hall']}/", cookies=world["cookies"]["owner"])
    assert owner.status_code == 200
    for tab in ("overview", "source", "captions", "settings"):
        assert f'data-room-tab="{tab}"' in owner.text
        assert f'data-room-panel="{tab}"' in owner.text
    # The pickers moved to a module; the page has no inline script.
    assert "room-detail.js" in owner.text
    assert "initialTransModel" not in owner.text
    assert "data-translation-languages=" in owner.text
    # The booth table shows short links, not raw paths.
    assert "Open booth ↗" in owner.text
    assert 'id="room-panel-overview" role="tabpanel" aria-labelledby="room-tab-overview"' in owner.text

    # For non-owners, tabpanel ARIA attributes are omitted so they do not reference missing tabs.
    async with client() as http:
        coord = await http.get(f"/admin/events/{world['event']}/rooms/{world['hall']}/", cookies=world["cookies"]["coord"])
    assert coord.status_code == 200
    assert 'id="room-panel-overview" data-room-panel="overview"' in coord.text
    assert 'role="tabpanel"' not in coord.text
