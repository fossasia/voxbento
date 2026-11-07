"""The Account page: profile, hosting and developer access, sign-in (#767)."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token, hash_password, verify_password
from portal.database import (
    configure,
    create_event,
    create_hosting_request,
    create_user,
    dispose,
    get_session,
    get_user_by_id,
    init_db,
    set_event_membership,
)
from portal.models import DeveloperAccount

os.environ["BOOTH_ACCESS_TOKEN"] = ""
SAME_ORIGIN = {"origin": "http://test"}


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def make_user(*, password: str | None = "old-password", **kwargs) -> tuple[int, dict[str, str]]:
    async with get_session() as session:
        user = await create_user(
            session,
            email="priya@example.org",
            display_name="Priya",
            password_hash=hash_password(password) if password else None,
            email_verified=True,
            **kwargs,
        )
        user_id = user.id
    return user_id, {"user_token": create_user_token(user_id=user_id, email="priya@example.org")}


async def account_html(cookies: dict[str, str], query: str = "") -> str:
    async with client() as http:
        res = await http.get(f"/account{query}", cookies=cookies)
    assert res.status_code == 200
    return res.text


def section(html: str, title: str) -> str:
    return html.split(f">{title}</h2>")[1].split("</section>")[0]


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_page_uses_shared_header_and_has_the_four_sections():
    _, cookies = await make_user()
    html = await account_html(cookies)
    assert '<span class="app-area app-area--account">Account</span>' in html
    assert '<h1 class="aa-title">Account</h1>' in html
    for title in ("Profile", "Hosting events", "Developer API", "Sign-in"):
        assert f">{title}</h2>" in html


@pytest.mark.anyio
async def test_old_role_badge_status_and_roles_table_are_gone():
    user_id, cookies = await make_user(is_admin=True)
    async with get_session() as session:
        event = await create_event(session, slug="summit", display_name="Summit")
        await set_event_membership(session, user_id=user_id, event_id=event.id, role="event_owner")
    html = await account_html(cookies)
    for gone in ("System Role", "Super Admin", "Your Roles", ">Status<", "Event_owner", "event_owner"):
        assert gone not in html, gone
    content = html.split('<div class="aa-page aa-page--wide">')[1].split("</main>")[0]
    assert "<script" not in content


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_profile_shows_editable_name_and_read_only_email():
    _, cookies = await make_user()
    profile = section(await account_html(cookies), "Profile")
    assert 'name="display_name" type="text" value="Priya"' in profile
    assert "Shown to event owners and on your events." in profile
    assert '<span class="aa-value">priya@example.org</span>' in profile
    assert 'name="email"' not in profile


@pytest.mark.anyio
async def test_display_name_can_be_changed():
    user_id, cookies = await make_user()
    async with client() as http:
        res = await http.post(
            "/account/profile",
            data={"display_name": "  Priya   Sharma "},
            headers=SAME_ORIGIN,
            cookies=cookies,
            follow_redirects=False,
        )
        assert res.headers["location"] == "/account?success=profile_saved"
        page = await http.get(res.headers["location"], cookies=cookies)
    async with get_session() as session:
        assert (await get_user_by_id(session, user_id)).display_name == "Priya Sharma"
    assert "Your display name was saved." in page.text
    assert '<p class="app-user-head-name">Priya Sharma</p>' in page.text


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["", "   ", "x" * 201, "bad\x00name"])
async def test_invalid_display_name_is_refused(name):
    user_id, cookies = await make_user()
    async with client() as http:
        res = await http.post(
            "/account/profile",
            data={"display_name": name},
            headers=SAME_ORIGIN,
            cookies=cookies,
            follow_redirects=False,
        )
        page = await http.get(res.headers["location"], cookies=cookies)
    assert res.headers["location"] == "/account?error=display_name"
    assert "Enter a name of up to 200 characters." in page.text
    async with get_session() as session:
        assert (await get_user_by_id(session, user_id)).display_name == "Priya"


@pytest.mark.anyio
async def test_profile_update_rejects_cross_origin():
    user_id, cookies = await make_user()
    async with client() as http:
        res = await http.post(
            "/account/profile",
            data={"display_name": "Evil"},
            headers={"origin": "https://evil.example"},
            cookies=cookies,
        )
    assert res.status_code == 400
    async with get_session() as session:
        assert (await get_user_by_id(session, user_id)).display_name == "Priya"


# ---------------------------------------------------------------------------
# Hosting events and Developer API states
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_hosting_section_states():
    user_id, cookies = await make_user()
    hosting = section(await account_html(cookies), "Hosting events")
    assert 'href="/account/hosting-request"' in hosting

    async with get_session() as session:
        await create_hosting_request(session, user_id=user_id, organization="A", event_name="B", languages="en")
    hosting = section(await account_html(cookies), "Hosting events")
    assert "Waiting for review" in hosting
    assert "The VoxBento team will get back to you by email." in hosting
    assert "<a " not in hosting

    async with get_session() as session:
        (await get_user_by_id(session, user_id)).can_host = True
    hosting = section(await account_html(cookies), "Hosting events")
    assert "You have hosting access" in hosting
    assert 'href="/workspace/"' in hosting


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "expected", "link"),
    [
        (None, "Apply for developer access", 'href="/developer"'),
        ("pending", "The VoxBento team is reviewing your application.", None),
        ("approved", "Open Developer API", 'href="/developer"'),
        ("rejected", "Application not approved", None),
        ("suspended", "Developer access suspended", None),
    ],
)
async def test_developer_section_states(status, expected, link):
    user_id, cookies = await make_user()
    if status:
        async with get_session() as session:
            session.add(DeveloperAccount(user_id=user_id, status=status, organization_name="Acme"))
    developer = section(await account_html(cookies), "Developer API")
    assert expected in developer
    if link:
        assert link in developer
    else:
        assert "<a " not in developer
    if status == "pending":
        assert "Waiting for review" in developer
        assert "You applied for developer access on" in developer


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_sign_in_section_with_password():
    _, cookies = await make_user()
    signin = section(await account_html(cookies), "Sign-in")
    assert "You can sign in with your password or with a link sent to your email." in signin
    assert "Change password" in signin
    assert "Remove your password and always sign in with a link sent to your email." in signin
    assert "Remove password" in signin
    assert "Set password" not in signin


@pytest.mark.anyio
async def test_sign_in_section_without_password():
    _, cookies = await make_user(password=None)
    signin = section(await account_html(cookies), "Sign-in")
    assert "You sign in with a link sent to your email." in signin
    assert "Set password" in signin
    assert "Change password" not in signin
    assert "Remove password" not in signin


@pytest.mark.anyio
async def test_changing_a_password_needs_the_current_one():
    user_id, cookies = await make_user()
    async with client() as http:
        wrong = await http.post(
            "/api/auth/set-password",
            json={"current_password": "nope", "password": "new-password"},
            cookies=cookies,
        )
        missing = await http.post("/api/auth/set-password", json={"password": "new-password"}, cookies=cookies)
        ok = await http.post(
            "/api/auth/set-password",
            json={"current_password": "old-password", "password": "new-password"},
            cookies=cookies,
        )
    assert wrong.status_code == 403
    assert wrong.json()["detail"] == "Your current password is not correct"
    assert missing.status_code == 403
    assert ok.status_code == 200
    async with get_session() as session:
        assert verify_password("new-password", (await get_user_by_id(session, user_id)).password_hash)


@pytest.mark.anyio
async def test_setting_a_first_password_and_removing_it():
    user_id, cookies = await make_user(password=None)
    async with client() as http:
        short = await http.post("/api/auth/set-password", json={"password": "short"}, cookies=cookies)
        first = await http.post("/api/auth/set-password", json={"password": "first-password"}, cookies=cookies)
        bad_remove = await http.post("/api/auth/remove-password", json={"password": "wrong"}, cookies=cookies)
        removed = await http.post("/api/auth/remove-password", json={"password": "first-password"}, cookies=cookies)
        page = await http.get("/account?success=password_removed", cookies=cookies)
    assert short.status_code == 400
    assert first.status_code == 200
    assert bad_remove.status_code == 403
    assert removed.status_code == 200
    async with get_session() as session:
        assert (await get_user_by_id(session, user_id)).password_hash is None
    assert "Your password was removed." in page.text


@pytest.mark.anyio
async def test_password_api_rejects_non_object_json():
    _, cookies = await make_user()
    async with client() as http:
        res = await http.post("/api/auth/set-password", json=["x"], cookies=cookies)
    assert res.status_code == 400
