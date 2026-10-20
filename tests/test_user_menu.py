"""Shared header, user menu and "My events" on every signed-in page (#766)."""

from __future__ import annotations

import os
import re

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token, hash_password
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
from portal.models import DeveloperAccount, utc_now

os.environ["BOOTH_ACCESS_TOKEN"] = ""

MENU_LABELS = ("My events", "Account", "Developer API", "Admin", "Logout")


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def make_user(email: str, display_name: str, **kwargs) -> tuple[int, dict[str, str]]:
    async with get_session() as session:
        user = await create_user(
            session,
            email=email,
            display_name=display_name,
            password_hash=hash_password("pw123456"),
            email_verified=True,
            **kwargs,
        )
        user_id = user.id
    token = create_user_token(user_id=user_id, email=email, is_admin=kwargs.get("is_admin", False))
    return user_id, {"user_token": token}


def menu_html(html: str) -> str:
    return html.split('<nav aria-label="User menu">')[1].split("</nav>")[0]


def menu_labels(html: str) -> list[str]:
    menu = menu_html(html)
    return [label for label in MENU_LABELS if f"<span>{label}</span>" in menu]


def current_entry(html: str) -> str:
    match = re.search(r'class="app-user-item is-current" href="([^"]+)"', menu_html(html))
    return match.group(1) if match else ""


def area_badge(html: str) -> str:
    match = re.search(r'<span class="app-area app-area--\w+">([^<]+)</span>', html)
    return match.group(1) if match else ""


def header_html(html: str) -> str:
    return html.split('<header class="app-header">')[1].split("</header>")[0]


# ---------------------------------------------------------------------------
# User menu
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_menu_shows_name_email_and_everyone_entries():
    _, cookies = await make_user("priya@example.org", "Priya")
    async with client() as http:
        res = await http.get("/account", cookies=cookies)
    assert res.status_code == 200
    assert 'aria-label="User menu - Priya"' in res.text
    assert '<span class="app-user-name">Priya</span>' in res.text
    assert '<p class="app-user-head-name">Priya</p>' in res.text
    assert '<p class="app-user-head-email">priya@example.org</p>' in res.text
    assert menu_labels(res.text) == ["My events", "Account", "Logout"]


@pytest.mark.anyio
async def test_admin_and_developer_entries_follow_the_table_order():
    user_id, cookies = await make_user("hong@example.org", "Hong", is_admin=True)
    async with get_session() as session:
        session.add(DeveloperAccount(user_id=user_id, status="approved", reviewed_at=utc_now()))
    async with client() as http:
        res = await http.get("/account", cookies=cookies)
    assert menu_labels(res.text) == ["My events", "Account", "Developer API", "Admin", "Logout"]


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["pending", "rejected", "suspended"])
async def test_developer_entry_needs_an_approved_app(status):
    user_id, cookies = await make_user("dev@example.org", "Dev")
    async with get_session() as session:
        session.add(DeveloperAccount(user_id=user_id, status=status))
    async with client() as http:
        res = await http.get("/account", cookies=cookies)
    assert "Developer API" not in menu_labels(res.text)


@pytest.mark.anyio
async def test_header_names_the_area_and_highlights_its_menu_entry():
    user_id, cookies = await make_user("hong@example.org", "Hong", is_admin=True)
    async with get_session() as session:
        session.add(DeveloperAccount(user_id=user_id, status="approved"))
    async with client() as http:
        pages = {
            "/account": ("Account", "/account"),
            "/workspace/": ("My events", "/workspace/"),
            "/admin/": ("Admin", "/admin/"),
            "/admin/users/": ("Admin", "/admin/"),
            "/developer": ("Developer API", "/developer"),
        }
        for path, (area, current) in pages.items():
            res = await http.get(path, cookies=cookies)
            assert res.status_code == 200, path
            assert area_badge(res.text) == area, path
            assert current_entry(res.text) == current, path


@pytest.mark.anyio
async def test_old_personal_links_are_gone_from_every_header():
    user_id, cookies = await make_user("hong@example.org", "Hong", is_admin=True)
    async with client() as http:
        for path in (
            "/account",
            "/workspace/",
            "/admin/",
            "/admin/users/",
            "/admin/hosting-access/",
            "/mission-control/",
        ):
            res = await http.get(path, cookies=cookies)
            assert res.status_code == 200, path
            header = header_html(res.text)
            outside_menu = header.split('<div class="app-user"')[0]
            for text in ("Portal Home", "My Account", 'href="/account"', 'href="/developer"', "logout"):
                assert text not in outside_menu, (path, text)
            assert 'href="/logout"' in menu_html(res.text)


@pytest.mark.anyio
async def test_admin_area_keeps_its_page_links():
    _, cookies = await make_user("hong@example.org", "Hong", is_admin=True)
    async with client() as http:
        res = await http.get("/admin/users/", cookies=cookies)
    nav = header_html(res.text).split('<nav class="app-header-nav"')[1].split("</nav>")[0]
    for href in ('href="/admin/hosting-access/"', 'href="/admin/events/"', 'href="/admin/users/"'):
        assert href in nav


@pytest.mark.anyio
async def test_room_coordinator_on_legacy_admin_room_page_is_in_my_events():
    async with get_session() as session:
        event = await create_event(session, slug="summit", display_name="Summit")
        room = await create_room(session, event_id=event.id, display_name="Room 1")
        event_id, room_id = event.id, room.id
    user_id, cookies = await make_user("coord@example.org", "Coord")
    async with get_session() as session:
        await set_room_membership(session, user_id=user_id, room_id=room_id, role="room_coordinator")
    async with client() as http:
        res = await http.get(f"/admin/events/{event_id}/rooms/{room_id}/", cookies=cookies, follow_redirects=False)
    assert res.status_code == 200
    assert area_badge(res.text) == "My events"
    assert "Admin" not in menu_labels(res.text)


# ---------------------------------------------------------------------------
# My events
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_my_events_requires_sign_in():
    async with client() as http:
        res = await http.get("/workspace/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login?next=/workspace/"


@pytest.mark.anyio
async def test_my_events_without_events_shows_no_events_yet():
    _, cookies = await make_user("priya@example.org", "Priya")
    async with client() as http:
        res = await http.get("/workspace/", cookies=cookies)
    assert res.status_code == 200
    assert '<h1 class="me-title">My events</h1>' in res.text
    assert "Events you own or work in." in res.text
    assert "No events yet" in res.text
    assert 'href="/workspace/setup"' not in res.text


@pytest.mark.anyio
async def test_my_events_lists_each_event_once_with_roles_and_buttons():
    async with get_session() as session:
        owned = await create_event(session, slug="foss", display_name="FOSS")
        worked = await create_event(session, slug="fossasia", display_name="FOSSASIA Summit")
        hall = await create_room(session, event_id=worked.id, display_name="Grand Hall")
        room1 = await create_room(session, event_id=worked.id, display_name="Room 1")
        zh = await create_booth(
            session, event_id=worked.id, room_id=hall.id, language_code="zh", language_name="Chinese"
        )
        await create_booth(session, event_id=worked.id, room_id=hall.id, language_code="en", language_name="English")
        ids = {"owned": owned.id, "worked": worked.id, "hall": hall.id, "room1": room1.id, "zh": zh.id}
    user_id, cookies = await make_user("hong@example.org", "Hong")
    async with get_session() as session:
        await set_event_membership(session, user_id=user_id, event_id=ids["owned"], role="event_owner")
        await set_room_membership(session, user_id=user_id, room_id=ids["room1"], role="room_coordinator")
        await set_booth_membership(session, user_id=user_id, booth_id=ids["zh"], role="interpreter")

    async with client() as http:
        res = await http.get("/workspace/", cookies=cookies)
    html = res.text
    assert res.status_code == 200
    assert html.count('<h2 class="me-card-title">FOSSASIA Summit</h2>') == 1
    assert html.count('<h2 class="me-card-title">FOSS</h2>') == 1
    assert "2 booths · Chinese, English" in html
    assert "0 booths" in html
    assert '<li class="me-role">Event owner</li>' in html
    assert '<li class="me-role">Room coordinator · Room 1</li>' in html
    assert '<li class="me-role">Interpreter · Grand Hall · Chinese</li>' in html
    assert f'href="/workspace/events/{ids["owned"]}/" class="me-action">Manage event</a>' in html
    assert 'href="/mission-control/fossasia/" class="me-action">Open Mission Control</a>' in html
    assert f'href="/interpreter/fossasia/{ids["hall"]}/zh" class="me-action">Enter booth</a>' in html


@pytest.mark.anyio
async def test_new_event_button_for_hosting_access_and_admins_only():
    _, host = await make_user("host@example.org", "Host", can_host=True)
    _, admin = await make_user("admin@example.org", "Admin", is_admin=True)
    _, regular = await make_user("regular@example.org", "Regular")
    async with client() as http:
        for cookies, expected in ((host, True), (admin, True), (regular, False)):
            res = await http.get("/workspace/", cookies=cookies)
            assert ('href="/workspace/setup" class="me-new-event"' in res.text) is expected


@pytest.mark.anyio
async def test_my_events_hides_deleted_events():
    async with get_session() as session:
        event = await create_event(session, slug="gone", display_name="Gone Event")
        event.deleted_at = utc_now()
        event_id = event.id
    user_id, cookies = await make_user("hong@example.org", "Hong")
    async with get_session() as session:
        await set_event_membership(session, user_id=user_id, event_id=event_id, role="event_owner")
    async with client() as http:
        res = await http.get("/workspace/", cookies=cookies)
    assert "Gone Event" not in res.text
    assert "No events yet" in res.text


@pytest.mark.anyio
async def test_menu_button_shows_a_short_name_like_eventyay():
    _, cookies = await make_user("alexandra@example.org", "Alexandrakonstantinou Papadopoulou")
    async with client() as http:
        res = await http.get("/account", cookies=cookies)
    assert '<span class="app-user-name">Alexandrako…</span>' in res.text
    assert '<p class="app-user-head-name">Alexandrakonstantinou Papadopoulou</p>' in res.text
    assert 'aria-label="User menu - Alexandrakonstantinou Papadopoulou"' in res.text


@pytest.mark.anyio
async def test_short_user_label():
    from portal.user_menu import short_user_label

    assert short_user_label("Lukas Becker", "lukas@example.org") == "Lukas"
    assert short_user_label("", "priya.k@example.org") == "priya.k"
    assert short_user_label("Hong", "hong@example.org") == "Hong"


@pytest.mark.anyio
async def test_signed_in_pages_version_their_stylesheets_and_scripts():
    # Static files are cached for hours; a per-deploy version stops browsers
    # pairing new HTML with old CSS (which left My events unstyled).
    from portal.globals import _JS_CACHE_BUST

    _, cookies = await make_user("hong@example.org", "Hong", is_admin=True)
    expected = {
        "/workspace/": ("css/admin.css", "css/app-header.css", "js/user-menu.js"),
        "/admin/": ("css/admin.css", "css/app-header.css", "js/admin.js"),
        "/mission-control/": ("css/admin.css", "css/app-header.css"),
        "/account": ("css/account-area.css", "css/app-header.css", "js/account.js", "css/interpreter.css"),
        "/developer": ("css/admin.css", "css/app-header.css"),
    }
    async with client() as http:
        for path, assets in expected.items():
            res = await http.get(path, cookies=cookies)
            assert res.status_code == 200, path
            for asset in assets:
                assert f"/static/{asset}?v={_JS_CACHE_BUST}" in res.text, (path, asset)
