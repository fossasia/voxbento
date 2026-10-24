"""Admin Users list and user page (#765): filters, access switches and safeguards."""

from __future__ import annotations

import os
import re
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import create_user_token, hash_password
from portal.database import (
    configure,
    create_auth_token,
    create_booth,
    create_event,
    create_hosting_request,
    create_room,
    create_user,
    dispose,
    get_session,
    get_user_by_id,
    init_db,
    set_booth_membership,
    set_event_membership,
    set_room_membership,
)

os.environ["BOOTH_ACCESS_TOKEN"] = ""
JSON = {"origin": "http://test", "accept": "application/json"}
FORM = {"origin": "http://test"}


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def make_user(email: str, name: str, *, password: bool = True, **kwargs) -> int:
    async with get_session() as session:
        user = await create_user(
            session,
            email=email,
            display_name=name,
            password_hash=hash_password("pw123456") if password else None,
            email_verified=kwargs.pop("email_verified", True),
            **kwargs,
        )
        return user.id


def cookies_for(user_id: int, email: str, is_admin: bool = False) -> dict[str, str]:
    return {"user_token": create_user_token(user_id=user_id, email=email, is_admin=is_admin)}


@pytest.fixture
async def admin():
    user_id = await make_user("hong@example.org", "Hong", is_admin=True)
    return {"id": user_id, "cookies": cookies_for(user_id, "hong@example.org", True)}


def switch(html: str, field: str, user_id: int) -> str:
    """The <button role="switch"> markup for one user's field."""
    pattern = (
        rf'action="/admin/users/{user_id}/access"[^>]*data-field="{field}"[^>]*>.*?'
        r'(<button type="submit" role="switch".*?>)'
    )
    match = re.search(pattern, html, re.S)
    assert match, (field, user_id)
    return match.group(1)


async def set_access(http: AsyncClient, cookies, user_id: int, field: str, value: bool, headers=JSON):
    return await http.post(
        f"/admin/users/{user_id}/access",
        data={"field": field, "value": "true" if value else "false"},
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )


# ---------------------------------------------------------------------------
# Users list
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_layout_search_and_show_filter(admin):
    await make_user("wiki@example.org", "Wikimania team", can_host=True)
    await make_user("lukas@example.org", "Lukas Becker", email_verified=False)
    await make_user("test@example.org", "Test account", is_active=False)
    async with client() as http:
        everyone = await http.get("/admin/users/", cookies=admin["cookies"])
        hosts = await http.get("/admin/users/?show=hosting", cookies=admin["cookies"])
        admins = await http.get("/admin/users/?show=admins", cookies=admin["cookies"])
        off = await http.get("/admin/users/?show=deactivated", cookies=admin["cookies"])
        unverified = await http.get("/admin/users/?show=unverified", cookies=admin["cookies"])
        search = await http.get("/admin/users/?search=becker", cookies=admin["cookies"])
        bogus = await http.get("/admin/users/?show=nope", cookies=admin["cookies"])

    html = everyone.text
    assert "<h1>Users</h1>" in html
    assert "Everyone with a VoxBento account." in html
    assert '<span class="ul-count">4 users</span>' in html
    for header in ("Hosting access", "Admin", "Active", "Verified"):
        assert f'<th scope="col" class="ul-center">{header}</th>' in html
    assert "Email not verified</span>" in html

    assert "wiki@example.org" in hosts.text and "lukas@example.org" not in hosts.text
    assert "hong@example.org" in admins.text and "wiki@example.org" not in admins.text
    assert "test@example.org" in off.text and '<span class="ul-count">1 user</span>' in off.text
    assert "lukas@example.org" in unverified.text and "wiki@example.org" not in unverified.text
    assert "lukas@example.org" in search.text and "wiki@example.org" not in search.text
    assert '<option value="all" selected>' in bogus.text


@pytest.mark.anyio
async def test_switch_states_and_safeguards(admin):
    host = await make_user("wiki@example.org", "Wikimania team", can_host=True)
    stub = await make_user("new@example.org", "New person", password=False, email_verified=False)
    off = await make_user("test@example.org", "Test account", is_active=False)
    async with client() as http:
        html = (await http.get("/admin/users/", cookies=admin["cookies"])).text

    assert 'aria-checked="true"' in switch(html, "hosting", host)
    assert "disabled" not in switch(html, "hosting", host)
    # Admins always have hosting access; the only Admin can't be demoted or deactivated by anyone.
    assert 'aria-checked="true"' in switch(html, "hosting", admin["id"]) and "disabled" in switch(
        html, "hosting", admin["id"]
    )
    assert "You can&#39;t change your own Admin status" in switch(html, "admin", admin["id"])
    assert "You can&#39;t deactivate your own account" in switch(html, "active", admin["id"])
    assert "You can&#39;t unverify your own email address" in switch(html, "verified", admin["id"])
    assert 'aria-checked="true"' in switch(html, "verified", host)
    assert 'aria-checked="false"' in switch(html, "verified", stub)
    # Accounts that aren't set up or are deactivated can't be granted access from the list.
    assert "disabled" in switch(html, "hosting", stub)
    assert "disabled" in switch(html, "hosting", off) and "disabled" in switch(html, "admin", off)
    assert 'aria-checked="false"' in switch(html, "active", off) and "disabled" not in switch(html, "active", off)


@pytest.mark.anyio
async def test_sorting_keeps_search_and_filter(admin):
    async with client() as http:
        res = await http.get("/admin/users/?search=Hong&show=admins", cookies=admin["cookies"])
        empty = await http.get("/admin/users/?search=nobody", cookies=admin["cookies"])
    assert 'href="?sort_by=email&sort_order=asc&search=Hong&show=admins&limit=50" class="sort-link">User' in res.text
    assert "No users match “nobody”." in empty.text


# ---------------------------------------------------------------------------
# Access endpoint
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_hosting_switch_grants_and_removes_access(admin):
    user_id = await make_user("lukas@example.org", "Lukas Becker")
    async with get_session() as session:
        await create_hosting_request(session, user_id=user_id, organization="A", event_name="B", languages="en")
    async with client() as http:
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as granted:
            on = await set_access(http, admin["cookies"], user_id, "hosting", True)
            again = await set_access(http, admin["cookies"], user_id, "hosting", True)
        off = await set_access(http, admin["cookies"], user_id, "hosting", False)
    assert on.json() == {
        "ok": True,
        "field": "hosting",
        "value": True,
        "message": "Lukas Becker can now create events.",
    }
    assert again.json()["ok"] is True
    granted.assert_awaited_once()  # no second email when nothing changed
    assert off.json()["message"] == "Lukas Becker can no longer create events. Their existing events are kept."
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        assert user.can_host is False and user.hosting_revoked_at is not None
        assert user.hosting_requested_at is None


@pytest.mark.anyio
async def test_admin_and_active_switches(admin):
    user_id = await make_user("mai@example.org", "Mai Nguyen")
    user_cookies = cookies_for(user_id, "mai@example.org")
    async with client() as http:
        make_admin = await set_access(http, admin["cookies"], user_id, "admin", True)
        admin_page = await http.get("/admin/users/", cookies=user_cookies)
        remove_admin = await set_access(http, admin["cookies"], user_id, "admin", False)
        deactivate = await set_access(http, admin["cookies"], user_id, "active", False)
        signed_out = await http.get("/account", cookies=user_cookies, follow_redirects=False)
        reactivate = await set_access(http, admin["cookies"], user_id, "active", True)
    assert make_admin.json()["message"] == "Mai Nguyen is now an Admin."
    assert admin_page.status_code == 200
    assert remove_admin.json()["value"] is False
    assert deactivate.json()["message"] == "Mai Nguyen's account was deactivated and signed out."
    assert signed_out.status_code == 303
    assert reactivate.json()["value"] is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("field", "value", "status", "detail"),
    [
        ("admin", False, 403, "You can't change your own Admin status."),
        ("active", False, 403, "You can't deactivate your own account."),
        ("verified", False, 403, "You can't unverify your own email address."),
        ("hosting", True, 400, "Admins can always create events."),
    ],
)
async def test_admin_cannot_lock_themselves_out(admin, field, value, status, detail):
    async with client() as http:
        res = await set_access(http, admin["cookies"], admin["id"], field, value)
    assert res.status_code == status
    assert res.json() == {"ok": False, "detail": detail}
    async with get_session() as session:
        me = await get_user_by_id(session, admin["id"])
        assert me.is_admin and me.is_active


@pytest.mark.anyio
async def test_last_active_admin_is_protected(admin):
    # Through the web an Admin can only be "the last one" when acting on themselves, which the
    # self rules catch first; the rule itself is a backstop for any other caller.
    from portal.user_admin import UserAdminError, delete_user_account, set_user_access

    for field in ("admin", "active", "verified"):
        with pytest.raises(UserAdminError) as exc:
            async with get_session() as session:
                await set_user_access(session, actor_id=None, user_id=admin["id"], field=field, value=False)
        assert exc.value.code == "last_admin"
    with pytest.raises(UserAdminError) as exc:
        async with get_session() as session:
            await delete_user_account(session, actor_id=None, user_id=admin["id"], confirm_email="hong@example.org")
    assert exc.value.code == "last_admin"

    # With a second active Admin, one can be demoted.
    other = await make_user("ops@example.org", "Ops", is_admin=True)
    async with client() as http:
        res = await set_access(http, cookies_for(other, "ops@example.org", True), admin["id"], "admin", False)
    assert res.json()["ok"] is True


@pytest.mark.anyio
async def test_verified_switch(admin):
    user_id = await make_user("unverified@example.org", "Unverified Person", email_verified=False)
    async with client() as http:
        on = await set_access(http, admin["cookies"], user_id, "verified", True)
        detail_after_on = await http.get(f"/admin/users/{user_id}/", cookies=admin["cookies"])
        off = await set_access(http, admin["cookies"], user_id, "verified", False)
        detail_after_off = await http.get(f"/admin/users/{user_id}/", cookies=admin["cookies"])
    assert on.json() == {
        "ok": True,
        "field": "verified",
        "value": True,
        "message": "Unverified Person's email was marked as verified.",
    }
    assert "Mark unverified" in detail_after_on.text
    assert off.json() == {
        "ok": True,
        "field": "verified",
        "value": False,
        "message": "Unverified Person's email was marked as not verified.",
    }
    assert "Mark verified" in detail_after_off.text
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        assert user.email_verified is False


@pytest.mark.anyio
async def test_grants_refused_for_unfinished_and_deactivated_accounts(admin):
    stub = await make_user("new@example.org", "New", password=False)
    off = await make_user("off@example.org", "Off", is_active=False)
    async with client() as http:
        no_account = await set_access(http, admin["cookies"], stub, "hosting", True)
        inactive_host = await set_access(http, admin["cookies"], off, "hosting", True)
        inactive_admin = await set_access(http, admin["cookies"], off, "admin", True)
    assert "Send them an invitation instead" in no_account.json()["detail"]
    assert inactive_host.json()["detail"] == "Reactivate this account first."
    assert inactive_admin.json()["detail"] == "Reactivate this account first."


@pytest.mark.anyio
async def test_access_endpoint_validation(admin):
    user_id = await make_user("x@example.org", "X")
    async with client() as http:
        cross = await set_access(
            http, admin["cookies"], user_id, "admin", True, headers={"origin": "https://evil.example"}
        )
        bad_field = await http.post(
            f"/admin/users/{user_id}/access",
            data={"field": "is_admin", "value": "true"},
            headers=JSON,
            cookies=admin["cookies"],
        )
        bad_value = await http.post(
            f"/admin/users/{user_id}/access",
            data={"field": "admin", "value": "yes"},
            headers=JSON,
            cookies=admin["cookies"],
        )
        missing = await set_access(http, admin["cookies"], 9999, "admin", True)
        not_admin = await set_access(http, cookies_for(user_id, "x@example.org"), user_id, "admin", True)
    assert cross.status_code == 400
    assert bad_field.status_code == 400 and bad_value.status_code == 400
    assert missing.status_code == 404
    assert not_admin.status_code == 403
    async with get_session() as session:
        assert (await get_user_by_id(session, user_id)).is_admin is False


@pytest.mark.anyio
async def test_plain_form_posts_return_to_the_page_with_a_message(admin):
    user_id = await make_user("x@example.org", "X")
    async with client() as http:
        ok = await set_access(
            http,
            admin["cookies"],
            user_id,
            "admin",
            True,
            headers={**FORM, "referer": f"http://test/admin/users/{user_id}/"},
        )
        page = await http.get(ok.headers["location"], cookies=admin["cookies"])
        refused = await set_access(http, admin["cookies"], admin["id"], "admin", False, headers=FORM)
        list_page = await http.get(refused.headers["location"], cookies=admin["cookies"])
    assert ok.headers["location"] == f"/admin/users/{user_id}/?notice=admin_on"
    assert "This person is now an Admin." in page.text
    assert refused.headers["location"] == "/admin/users/?error=self_admin"
    assert "You can&#39;t change your own Admin status." in list_page.text


@pytest.mark.anyio
async def test_legacy_toggles_follow_the_same_rules(admin):
    async with client() as http:
        no_origin = await http.post(f"/admin/users/{admin['id']}/toggle-admin", cookies=admin["cookies"])
        self_demote = await http.post(
            f"/admin/users/{admin['id']}/toggle-admin", headers=FORM, cookies=admin["cookies"], follow_redirects=False
        )
        owner_route = await http.post(
            f"/admin/users/{admin['id']}/events/1/toggle-owner", headers=FORM, cookies=admin["cookies"]
        )
    assert no_origin.status_code == 400
    assert self_demote.headers["location"].endswith("error=self_admin")
    assert owner_route.status_code in (404, 405)
    async with get_session() as session:
        assert (await get_user_by_id(session, admin["id"])).is_admin is True


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_delete_needs_the_email_typed_and_never_self(admin):
    user_id = await make_user("lukas@example.org", "Lukas Becker")
    async with client() as http:
        wrong = await http.post(
            f"/admin/users/{user_id}/delete",
            data={"confirm_email": "lukas@example.com"},
            headers=FORM,
            cookies=admin["cookies"],
            follow_redirects=False,
        )
        self_delete = await http.post(
            f"/admin/users/{admin['id']}/delete",
            data={"confirm_email": "hong@example.org"},
            headers=FORM,
            cookies=admin["cookies"],
            follow_redirects=False,
        )
        ok = await http.post(
            f"/admin/users/{user_id}/delete",
            data={"confirm_email": " LUKAS@example.org "},
            headers=FORM,
            cookies=admin["cookies"],
            follow_redirects=False,
        )
    assert wrong.headers["location"] == f"/admin/users/{user_id}/?error=confirm_email"
    assert self_delete.headers["location"] == f"/admin/users/{admin['id']}/?error=self_delete"
    assert ok.headers["location"] == "/admin/users/?notice=deleted"
    async with get_session() as session:
        assert await get_user_by_id(session, user_id) is None
        assert await get_user_by_id(session, admin["id"]) is not None


# ---------------------------------------------------------------------------
# User page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_user_page_sections_roles_and_owned_events(admin):
    user_id = await make_user("lukas@example.org", "Lukas Becker")
    async with get_session() as session:
        foss = await create_event(session, slug="foss", display_name="FOSS")
        hall = await create_room(session, event_id=foss.id, display_name="Big Hall 2")
        booth = await create_booth(
            session, event_id=foss.id, room_id=hall.id, language_code="de", language_name="Deutsch"
        )
        summit = await create_event(session, slug="summit", display_name="Summit 2026")
        room1 = await create_room(session, event_id=summit.id, display_name="Room 1")
        owned = await create_event(session, slug="mine", display_name="My Conference")
        await set_booth_membership(session, user_id=user_id, booth_id=booth.id, role="interpreter")
        await set_room_membership(session, user_id=user_id, room_id=room1.id, role="room_coordinator")
        await set_event_membership(session, user_id=user_id, event_id=summit.id, role="interpreter")
        await set_event_membership(session, user_id=user_id, event_id=owned.id, role="event_owner")
    async with client() as http:
        html = (await http.get(f"/admin/users/{user_id}/", cookies=admin["cookies"])).text

    assert '<a href="/admin/users/">Users</a>' in html
    assert "<h1>Lukas Becker</h1>" in html and '<span class="ud-pill ud-pill--active">Active</span>' in html
    for title in ("Platform access", "Roles in events", "Events owned", "Account", "Danger zone"):
        assert f">{title}</h2>" in html
    assert "<td>Big Hall 2 · Deutsch booth</td>" in html
    assert "<td>Room 1</td>" in html and "<td>Room coordinator</td>" in html
    assert "<td>Whole event</td>" in html and "<td>Interpreter</td>" in html
    assert f'<a href="/admin/events/{owned.id}/">My Conference</a>' in html
    assert "Event owner" not in html.split(">Roles in events</h2>")[1].split("</section>")[0]
    assert "Grant hosting access" in html and "Make Admin" in html
    assert "Deactivate" in html and 'data-confirm-match="lukas@example.org"' in html
    assert "toggle-owner" not in html


@pytest.mark.anyio
async def test_user_page_hosting_states(admin):
    requester = await make_user("req@example.org", "Req")
    invited = await make_user("inv@example.org", "Inv", password=False)
    async with get_session() as session:
        await create_hosting_request(session, user_id=requester, organization="A", event_name="B", languages="en")
        tok = await create_auth_token(session, user_id=invited, token_type="hosting_invite", expires_in_seconds=3600)
    async with client() as http:
        req_page = (await http.get(f"/admin/users/{requester}/", cookies=admin["cookies"])).text
        inv_page = (await http.get(f"/admin/users/{invited}/", cookies=admin["cookies"])).text
        own_page = (await http.get(f"/admin/users/{admin['id']}/", cookies=admin["cookies"])).text
    assert "Review request" in req_page and 'href="/admin/hosting-access/requests/"' in req_page
    assert "Invitation pending" in inv_page and f"/admin/users/invite/{tok.id}/resend" in inv_page
    assert "Included with Admin" in own_page
    assert "You can't deactivate or delete your own account." in own_page
    assert "ud-delete-dialog" in own_page and 'id="ud-delete-dialog"' not in own_page
