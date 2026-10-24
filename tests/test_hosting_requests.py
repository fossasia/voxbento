"""Signed-in users request hosting access; Admins grant or decline it (#768)."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from fastapi_app import app
from portal.auth import create_user_token, hash_password
from portal.database import (
    configure,
    create_hosting_request,
    create_user,
    dispose,
    get_session,
    get_user_by_id,
    init_db,
)
from portal.hosting_requests import format_event_dates, parse_hosting_request_form
from portal.models import HostingRequest

os.environ["BOOTH_ACCESS_TOKEN"] = ""

SAME_ORIGIN = {"origin": "http://test"}
VALID_FORM = {
    "organization": "FOSSASIA",
    "event_name": "FOSSASIA Summit 2027",
    "event_starts_on": "2027-03-10",
    "event_ends_on": "2027-03-12",
    "languages": "English, Vietnamese",
    "message": "We run three tracks.",
}


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def make_user(email: str = "priya@example.org", *, display_name: str = "Priya", **kwargs) -> tuple[int, str]:
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
    return user_id, create_user_token(user_id=user_id, email=email, is_admin=kwargs.get("is_admin", False))


async def open_requests() -> list[HostingRequest]:
    async with get_session() as session:
        rows = await session.execute(select(HostingRequest).where(HostingRequest.status == "open"))
        return list(rows.scalars().all())


async def submit(http: AsyncClient, token: str, data: dict[str, str] | None = None):
    with patch("portal.routers.auth.queue_notification", new_callable=AsyncMock) as notify:
        res = await http.post(
            "/account/hosting-request",
            data=VALID_FORM if data is None else data,
            headers=SAME_ORIGIN,
            cookies={"user_token": token},
            follow_redirects=False,
        )
    return res, notify


# ---------------------------------------------------------------------------
# Request form
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_form_requires_sign_in():
    async with client() as http:
        res = await http.get("/account/hosting-request", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login?next=/account/hosting-request"


@pytest.mark.anyio
async def test_form_shows_breadcrumb_and_prefilled_readonly_identity():
    _, token = await make_user()
    async with client() as http:
        res = await http.get("/account/hosting-request", cookies={"user_token": token})
    assert res.status_code == 200
    html = res.text
    assert '<a href="/account">Account</a>' in html
    assert "Request hosting access" in html
    assert 'id="hr-name" type="text" value="Priya" readonly' in html
    assert 'id="hr-email" type="email" value="priya@example.org" readonly' in html
    for name in ("organization", "event_name", "event_starts_on", "event_ends_on", "languages", "message"):
        assert f'name="{name}"' in html
    # Name and email are not submitted, so they can't be changed through the form.
    assert 'name="email"' not in html
    assert "Send request" in html


@pytest.mark.anyio
@pytest.mark.parametrize("flags", [{"can_host": True}, {"is_admin": True}])
async def test_form_redirects_people_who_already_have_hosting_access(flags):
    _, token = await make_user(**flags)
    async with client() as http:
        res = await http.get("/account/hosting-request", cookies={"user_token": token}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/account"


@pytest.mark.anyio
async def test_submit_saves_request_notifies_team_and_shows_waiting_state():
    user_id, token = await make_user()
    async with client() as http:
        res, notify = await submit(http, token)
        assert res.status_code == 303
        assert res.headers["location"] == "/account?success=hosting_request_sent"

        account = await http.get(res.headers["location"], cookies={"user_token": token})

    rows = await open_requests()
    assert len(rows) == 1
    hr = rows[0]
    assert hr.user_id == user_id
    assert hr.organization == "FOSSASIA"
    assert hr.event_name == "FOSSASIA Summit 2027"
    assert hr.event_starts_on == date(2027, 3, 10)
    assert hr.event_ends_on == date(2027, 3, 12)
    assert hr.languages == "English, Vietnamese"
    assert hr.message == "We run three tracks."

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        assert user.hosting_requested_at is not None

    notify.assert_awaited_once()
    assert notify.await_args.args[1] == "hosting_request"
    assert notify.await_args.args[2] == "voxbento.dev@gmail.com"
    context = notify.await_args.args[3]
    assert context["requester_email"] == "priya@example.org"
    assert context["event_dates"] == "Mar 10, 2027 – Mar 12, 2027"
    assert context["review_url"].endswith("/admin/hosting-access/requests/")

    html = account.text
    assert "Your request was sent" in html
    assert "We'll email you at priya@example.org once the VoxBento team has reviewed it." in html
    assert "Request sent" in html
    assert "Waiting for review" in html
    assert 'href="/account/hosting-request"' not in html


@pytest.mark.anyio
async def test_account_without_request_offers_request_access():
    _, token = await make_user()
    async with client() as http:
        res = await http.get("/account", cookies={"user_token": token})
    assert 'href="/account/hosting-request"' in res.text
    assert "Waiting for review" not in res.text
    assert "Your request was sent" not in res.text


@pytest.mark.anyio
async def test_submit_requires_organization_event_name_and_languages():
    _, token = await make_user()
    async with client() as http:
        res, notify = await submit(
            http, token, {"organization": " ", "event_name": "", "languages": "", "message": "hi"}
        )
    assert res.status_code == 400
    assert "Enter your organization." in res.text
    assert "Enter the name of your event." in res.text
    assert "Enter the languages you need." in res.text
    # What the person typed is kept.
    assert ">hi</textarea>" in res.text
    notify.assert_not_awaited()
    assert await open_requests() == []


@pytest.mark.anyio
async def test_submit_rejects_end_before_start():
    _, token = await make_user()
    async with client() as http:
        res, _ = await submit(
            http, token, {**VALID_FORM, "event_starts_on": "2027-03-12", "event_ends_on": "2027-03-10"}
        )
    assert res.status_code == 400
    assert "The event can&#39;t end before it starts." in res.text
    assert await open_requests() == []


@pytest.mark.anyio
async def test_submit_rejects_cross_origin_post():
    _, token = await make_user()
    async with client() as http:
        res = await http.post(
            "/account/hosting-request",
            data=VALID_FORM,
            headers={"origin": "https://evil.example"},
            cookies={"user_token": token},
        )
    assert res.status_code == 400
    assert await open_requests() == []


@pytest.mark.anyio
async def test_only_one_open_request_per_person():
    _, token = await make_user()
    async with client() as http:
        await submit(http, token)
        res, notify = await submit(http, token, {**VALID_FORM, "event_name": "Second"})
        form = await http.get("/account/hosting-request", cookies={"user_token": token}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/account"
    notify.assert_not_awaited()
    assert form.status_code == 303
    rows = await open_requests()
    assert [r.event_name for r in rows] == ["FOSSASIA Summit 2027"]


@pytest.mark.anyio
async def test_database_rejects_a_second_open_request():
    user_id, _ = await make_user()
    async with get_session() as session:
        await create_hosting_request(session, user_id=user_id, organization="A", event_name="A", languages="en")
    with pytest.raises(IntegrityError):
        async with get_session() as session:
            await create_hosting_request(session, user_id=user_id, organization="B", event_name="B", languages="en")


# ---------------------------------------------------------------------------
# Admin: Hosting access page
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_requests_tab_lists_open_requests_with_nav_count():
    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    _, token = await make_user()
    async with client() as http:
        await submit(http, token)
        res = await http.get("/admin/hosting-access/requests/", cookies={"user_token": admin_token})
        people = await http.get("/admin/hosting-access/", cookies={"user_token": admin_token})
    assert res.status_code == 200
    html = res.text
    assert "People with access" in html
    assert "Requests" in html
    assert '<span class="nav-count">1<span class="visually-hidden"> open requests</span></span>' in html
    for text in (
        "Priya",
        "priya@example.org",
        "FOSSASIA Summit 2027",
        "Mar 10, 2027 – Mar 12, 2027",
        "English, Vietnamese",
        "We run three tracks.",
        "Grant access",
        "Decline",
    ):
        assert text in html
    # The count is shown on every Admin page, not only on the Hosting access page.
    assert '<span class="nav-count">1<span class="visually-hidden"> open requests</span></span>' in people.text


@pytest.mark.anyio
async def test_hosting_access_pages_are_admin_only():
    _, token = await make_user(can_host=True)
    async with client() as http:
        for path in ("/admin/hosting-access/", "/admin/hosting-access/requests/"):
            res = await http.get(path, cookies={"user_token": token}, follow_redirects=False)
            assert res.status_code in (302, 303, 403)
        res = await http.post(
            "/admin/hosting-access/requests/1/grant",
            headers=SAME_ORIGIN,
            cookies={"user_token": token},
            follow_redirects=False,
        )
        assert res.status_code in (302, 303, 403)


@pytest.mark.anyio
async def test_grant_gives_access_and_moves_person_to_people_with_access():
    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    user_id, token = await make_user()
    async with client() as http:
        await submit(http, token)
        hr = (await open_requests())[0]
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as granted:
            res = await http.post(
                f"/admin/hosting-access/requests/{hr.id}/grant",
                headers=SAME_ORIGIN,
                cookies={"user_token": admin_token},
                follow_redirects=False,
            )
        assert res.status_code == 303
        assert res.headers["location"] == "/admin/hosting-access/requests/?success=request_granted"
        granted.assert_awaited_once()
        assert granted.await_args.args[2] == "priya@example.org"

        requests_page = await http.get("/admin/hosting-access/requests/", cookies={"user_token": admin_token})
        people_page = await http.get("/admin/hosting-access/", cookies={"user_token": admin_token})
        account = await http.get("/account", cookies={"user_token": token})

    assert await open_requests() == []
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        assert user.can_host is True
        assert user.hosting_requested_at is None
        closed = (await session.execute(select(HostingRequest))).scalar_one()
        assert closed.status == "granted"
        assert closed.decided_at is not None

    assert "No open requests" in requests_page.text
    assert "nav-count" not in requests_page.text
    assert "priya@example.org" in people_page.text
    assert "Has access" in people_page.text
    assert "You have hosting access" in account.text


@pytest.mark.anyio
async def test_decline_requires_reason_then_tells_person_why():
    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    user_id, token = await make_user()
    async with client() as http:
        await submit(http, token)
        hr = (await open_requests())[0]
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as declined:
            no_reason = await http.post(
                f"/admin/hosting-access/requests/{hr.id}/decline",
                data={"reason": "  "},
                headers=SAME_ORIGIN,
                cookies={"user_token": admin_token},
                follow_redirects=False,
            )
            assert no_reason.headers["location"] == "/admin/hosting-access/requests/?error=decline_reason"
            declined.assert_not_awaited()
            assert len(await open_requests()) == 1

            res = await http.post(
                f"/admin/hosting-access/requests/{hr.id}/decline",
                data={"reason": "We can only support events with a confirmed date."},
                headers=SAME_ORIGIN,
                cookies={"user_token": admin_token},
                follow_redirects=False,
            )
        assert res.status_code == 303
        assert res.headers["location"] == "/admin/hosting-access/requests/?success=request_declined"
        declined.assert_awaited_once()
        assert declined.await_args.args[1] == "hosting_request_declined"
        assert declined.await_args.args[2] == "priya@example.org"
        assert declined.await_args.args[3]["reason"] == "We can only support events with a confirmed date."

        account = await http.get("/account", cookies={"user_token": token})

    assert await open_requests() == []
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        assert user.can_host is False
        assert user.hosting_requested_at is None
        closed = (await session.execute(select(HostingRequest))).scalar_one()
        assert closed.status == "declined"
        assert closed.decline_reason == "We can only support events with a confirmed date."

    # The person can send a new request.
    assert 'href="/account/hosting-request"' in account.text


@pytest.mark.anyio
async def test_handled_request_cannot_be_handled_again():
    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    _, token = await make_user()
    async with client() as http:
        await submit(http, token)
        hr = (await open_requests())[0]
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock):
            await http.post(
                f"/admin/hosting-access/requests/{hr.id}/grant",
                headers=SAME_ORIGIN,
                cookies={"user_token": admin_token},
            )
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as declined:
            res = await http.post(
                f"/admin/hosting-access/requests/{hr.id}/decline",
                data={"reason": "Too late"},
                headers=SAME_ORIGIN,
                cookies={"user_token": admin_token},
                follow_redirects=False,
            )
    assert res.headers["location"] == "/admin/hosting-access/requests/?error=request_not_found"
    declined.assert_not_awaited()


@pytest.mark.anyio
async def test_granting_from_user_page_closes_open_request():
    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    user_id, token = await make_user()
    async with client() as http:
        await submit(http, token)
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock):
            await http.post(
                f"/admin/users/{user_id}/hosting",
                data={"action": "grant"},
                headers=SAME_ORIGIN,
                cookies={"user_token": admin_token},
            )
    assert await open_requests() == []


# ---------------------------------------------------------------------------
# Helpers and migration
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_format_event_dates():
    assert format_event_dates(date(2026, 10, 11), date(2026, 10, 13)) == "Oct 11, 2026 – Oct 13, 2026"
    assert format_event_dates(date(2026, 10, 11), date(2026, 10, 11)) == "Oct 11, 2026"
    assert format_event_dates(date(2026, 10, 11), None) == "From Oct 11, 2026"
    assert format_event_dates(None, None) == "Not given"


@pytest.mark.anyio
async def test_parse_form_collapses_line_breaks_and_flags_bad_dates():
    cleaned, errors = parse_hosting_request_form(
        {**VALID_FORM, "organization": "Acme\r\nCorp", "event_starts_on": "not-a-date"}
    )
    assert cleaned.organization == "Acme Corp"
    assert errors == {"event_starts_on": "Enter a valid date."}


@pytest.mark.anyio
async def test_migration_030_creates_and_drops_hosting_requests():
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    mig_path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "030_hosting_requests.py"
    spec = importlib.util.spec_from_file_location("migration_030", mig_path)
    mig_030 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig_030)

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, email VARCHAR(320) NOT NULL)")
        conn.execute("INSERT INTO users (id, email) VALUES (1, 'priya@example.org')")
        conn.commit()
        conn.close()

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as sa_conn:
            with Operations.context(MigrationContext.configure(sa_conn)):
                mig_030.upgrade()
            sa_conn.commit()

        conn = sqlite3.connect(db_path)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(hosting_requests)")}
        assert {
            "user_id",
            "organization",
            "event_name",
            "event_starts_on",
            "event_ends_on",
            "languages",
            "message",
            "status",
            "decline_reason",
            "decided_by_user_id",
            "decided_at",
            "created_at",
        } <= columns
        insert = (
            "INSERT INTO hosting_requests (user_id, organization, event_name, languages, status, created_at) "
            "VALUES (1, 'A', 'A', 'en', ?, '2026-10-11')"
        )
        conn.execute(insert, ("open",))
        conn.execute(insert, ("declined",))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, ("open",))
        conn.commit()
        conn.close()

        with engine.connect() as sa_conn:
            with Operations.context(MigrationContext.configure(sa_conn)):
                mig_030.downgrade()
            sa_conn.commit()
        engine.dispose()

        conn = sqlite3.connect(db_path)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "hosting_requests" not in tables
    finally:
        os.remove(db_path)


@pytest.mark.anyio
async def test_granted_person_can_create_events():
    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    _, token = await make_user()
    async with client() as http:
        before = await http.get("/workspace/setup", cookies={"user_token": token}, follow_redirects=False)
        assert before.status_code == 303
        assert before.headers["location"] == "/account?error=hosting_access_required"

        await submit(http, token)
        hr = (await open_requests())[0]
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock):
            await http.post(
                f"/admin/hosting-access/requests/{hr.id}/grant",
                headers=SAME_ORIGIN,
                cookies={"user_token": admin_token},
            )

        # Same session cookie: access applies without signing out.
        after = await http.get("/workspace/setup", cookies={"user_token": token}, follow_redirects=False)
        assert after.status_code == 200


@pytest.mark.anyio
async def test_grant_refused_for_deactivated_requester():
    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    user_id, token = await make_user()
    async with client() as http:
        await submit(http, token)
    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        user.is_active = False
    hr = (await open_requests())[0]
    async with client() as http:
        with patch("portal.routers.admin.queue_notification", new_callable=AsyncMock) as granted:
            res = await http.post(
                f"/admin/hosting-access/requests/{hr.id}/grant",
                headers=SAME_ORIGIN,
                cookies={"user_token": admin_token},
                follow_redirects=False,
            )
    assert res.headers["location"] == "/admin/hosting-access/requests/?error=inactive_user"
    granted.assert_not_awaited()
    async with get_session() as session:
        assert (await get_user_by_id(session, user_id)).can_host is False
    assert len(await open_requests()) == 1


@pytest.mark.anyio
async def test_deleting_user_removes_their_requests():
    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    user_id, token = await make_user()
    async with client() as http:
        await submit(http, token)
        await http.post(
            f"/admin/users/{user_id}/delete",
            data={"confirm_email": "priya@example.org"},
            headers=SAME_ORIGIN,
            cookies={"user_token": admin_token},
        )
        page = await http.get("/admin/hosting-access/requests/", cookies={"user_token": admin_token})
    assert page.status_code == 200
    assert await open_requests() == []


# ---------------------------------------------------------------------------
# Admin: Dev Accounts count, like the Hosting access count
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_dev_accounts_nav_counts_pending_applications_until_reviewed():
    from portal.models import DeveloperAccount

    _, admin_token = await make_user("admin@example.org", display_name="Hong", is_admin=True)
    applicant_id, applicant_token = await make_user()
    async with get_session() as session:
        account = DeveloperAccount(user_id=applicant_id, status="pending")
        session.add(account)
        await session.flush()
        account_id = account.id
    badge = '<span class="nav-count">1<span class="visually-hidden"> pending applications</span></span>'
    async with client() as http:
        overview = await http.get("/admin/", cookies={"user_token": admin_token})
        applicant_view = await http.get("/account", cookies={"user_token": applicant_token})
        await http.post(f"/api/admin/developer-accounts/{account_id}/approve", cookies={"user_token": admin_token})
        after = await http.get("/admin/", cookies={"user_token": admin_token})
    # Shown on every Admin page, only to Admins, and gone once the application is reviewed.
    assert badge in overview.text
    assert "pending applications" not in applicant_view.text
    assert "pending applications" not in after.text
