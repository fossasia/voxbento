"""Tests for the admin panel routes (Phase 3, Step 4).

Covers:
- Admin sign-in redirect and logout flow
- require_owner_or_coordinator() guard (403 without cookie)
- CRUD operations for events, rooms, booths
- Dashboard with live status
- Booth detail with WHEP URL and participant roster
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

from datetime import datetime, timedelta, timezone

import pytest

from portal.auth import create_user_token, decode_token, hash_password

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_db():
    """Set up an in-memory database for the FastAPI app."""
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def seeded_admin():
    """Create a real, active site administrator in the database."""
    from portal.database import create_user, get_session

    async with get_session() as s:
        user = await create_user(
            s,
            email="admin@example.com",
            display_name="Admin",
            password_hash=hash_password("test-admin-pass"),
            is_admin=True,
        )
    return user


@pytest.fixture
async def admin_cookie(seeded_admin):
    """Return a dict with the user_token cookie for authenticated admin requests.

    Authorization is checked against the database, so the token must belong to a
    real administrator row rather than a phantom id.
    """
    token = create_user_token(user_id=seeded_admin.id, email="admin@example.com", is_admin=True)
    return {"user_token": token}


@pytest.fixture
async def seed_event():
    """Seed an event, room, and booth."""
    from portal.database import create_booth, create_event, create_room, get_session

    async with get_session() as s:
        event = await create_event(s, slug="testcon", display_name="TestCon 2026")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        booth = await create_booth(
            s,
            event_id=event.id,
            room_id=room.id,
            language_code="en",
            language_name="English",
        )
    return event, room, booth


@pytest.fixture
async def room_scope_events():
    """Two event rooms with an owner scoped only to the first event."""
    from portal.database import (
        create_booth,
        create_event,
        create_invite_token,
        create_room,
        create_user,
        get_session,
        set_booth_membership,
        set_event_membership,
        set_room_membership,
    )

    async with get_session() as s:
        event_a = await create_event(s, slug="scope-a", display_name="Event A")
        event_b = await create_event(s, slug="scope-b", display_name="Event B")
        room_a = await create_room(s, event_id=event_a.id, display_name="A room")
        room_b = await create_room(s, event_id=event_b.id, display_name="B room")
        booth_a = await create_booth(
            s, event_id=event_a.id, room_id=room_a.id, language_code="en", language_name="English"
        )
        booth_b = await create_booth(
            s, event_id=event_b.id, room_id=room_b.id, language_code="fr", language_name="French"
        )
        invite_b = await create_invite_token(s, booth_id=booth_b.id, role="interpreter")
        owner = await create_user(s, email="scope-owner@example.com", display_name="Owner A")
        member = await create_user(s, email="scope-member@example.com", display_name="Interpreter B")
        coordinator = await create_user(s, email="scope-coordinator@example.com", display_name="Coordinator A")
        await set_event_membership(s, user_id=owner.id, event_id=event_a.id, role="event_owner")
        booth_membership = await set_booth_membership(s, user_id=member.id, booth_id=booth_b.id, role="interpreter")
        await set_room_membership(s, user_id=coordinator.id, room_id=room_a.id, role="room_coordinator")

        return {
            "event_a": event_a.id,
            "event_b": event_b.id,
            "room_a": room_a.id,
            "room_b": room_b.id,
            "booth_a": booth_a.id,
            "booth_b": booth_b.id,
            "invite_b": invite_b.token,
            "booth_membership_b": booth_membership.id,
            "owner_cookie": {"user_token": create_user_token(user_id=owner.id, email=owner.email)},
            "coordinator_cookie": {"user_token": create_user_token(user_id=coordinator.id, email=coordinator.email)},
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_relay_attr(html: str) -> str:
    """The relay WHEP URL the interpreter page hands the browser."""
    import re

    m = re.search(r"data-relay-whep-url='([^']*)'", html)
    assert m, "the relay attribute is not in the page at all, so a match proves nothing"
    return m.group(1)


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Login / Logout tests
# ---------------------------------------------------------------------------


class TestAdminLogin:
    """Admins sign in through the shared ``/login`` page (#752)."""

    @pytest.fixture(autouse=True)
    def isolated_login_limits(self, monkeypatch):
        import portal.rate_limit

        monkeypatch.setattr(portal.rate_limit, "_rates", {})

    @pytest.mark.anyio
    async def test_admin_login_page_redirects_to_shared_sign_in(self):
        async with _client() as c:
            resp = await c.get("/admin/login", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login?next=/admin/"

    @pytest.mark.anyio
    async def test_admin_login_form_post_is_gone(self):
        async with _client() as c:
            resp = await c.post(
                "/admin/login",
                data={"email": "admin@example.com", "password": "test-admin-pass"},
                follow_redirects=False,
            )
        assert resp.status_code == 405
        assert "user_token" not in resp.headers.get("set-cookie", "")

    @pytest.mark.anyio
    async def test_admin_signs_in_through_shared_page(self):
        from portal.database import create_user, get_session

        async with get_session() as s:
            admin = await create_user(
                s,
                email="admin@example.com",
                display_name="Admin",
                password_hash=hash_password("test-admin-pass"),
                email_verified=True,
                is_admin=True,
            )

        async with _client() as c:
            resp = await c.post(
                "/login",
                data={"email": "admin@example.com", "password": "test-admin-pass", "next_url": "/admin/"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/admin/"
            token_val = resp.cookies.get("user_token")
            payload = decode_token(token_val)
            assert payload["sub"] == str(admin.id)
            assert payload["is_admin"] is True

            dashboard = await c.get("/admin/", cookies={"user_token": token_val})
        assert dashboard.status_code == 200

    @pytest.mark.anyio
    async def test_login_rejects_unverified_admin(self):
        from portal.database import create_user, get_session

        async with get_session() as session:
            await create_user(
                session,
                email="unverified@example.com",
                display_name="Unverified admin",
                password_hash=hash_password("test-admin-pass"),
                is_admin=True,
                email_verified=False,
            )
        async with _client() as client:
            response = await client.post(
                "/login",
                data={"email": "unverified@example.com", "password": "test-admin-pass", "next_url": "/admin/"},
                follow_redirects=False,
            )
        assert response.status_code == 403
        assert "Invalid email or password." in response.text
        assert "Please verify your email address first." not in response.text
        assert "user_token" not in response.cookies

    @pytest.mark.anyio
    async def test_login_fails_closed_when_legacy_password_is_set(self, monkeypatch, setup_db):
        """A removed shared secret must never regain access through the environment."""
        monkeypatch.setenv("ADMIN_PASSWORD", "legacy-shared-secret")

        async with _client() as c:
            resp = await c.post(
                "/login",
                data={"email": "admin@example.com", "password": "legacy-shared-secret", "next_url": "/admin/"},
                follow_redirects=False,
            )

        assert resp.status_code == 403
        assert "user_token" not in resp.headers.get("set-cookie", "")

    @pytest.mark.anyio
    async def test_login_rejects_inactive_admin(self, setup_db):
        """An administrator whose account has been deactivated cannot sign in."""
        from portal.database import create_user, get_session, update_user_active

        async with get_session() as s:
            user = await create_user(
                s,
                email="inactive-admin@example.com",
                display_name="Inactive Admin",
                password_hash=hash_password("test-admin-pass"),
                email_verified=True,
                is_admin=True,
            )
            await update_user_active(s, user.id, is_active=False)

        async with _client() as c:
            resp = await c.post(
                "/login",
                data={"email": "inactive-admin@example.com", "password": "test-admin-pass", "next_url": "/admin/"},
                follow_redirects=False,
            )
        assert resp.status_code == 403
        assert "user_token" not in resp.headers.get("set-cookie", "")

    @pytest.mark.anyio
    async def test_documented_break_glass_recovery_procedure(self, setup_db):
        """Verify the documented break-glass recovery SQL procedure promotes an account safely."""
        from sqlalchemy import text

        from portal.database import create_user, get_session

        async with get_session() as s:
            await create_user(
                s,
                email="operator@example.com",
                display_name="Operator",
                password_hash=hash_password("operator-pass"),
                email_verified=True,
                is_admin=False,
            )

        async with _client() as c:
            # 1. The operator can sign in, but is not yet allowed into the admin panel
            login = await c.post(
                "/login",
                data={"email": "operator@example.com", "password": "operator-pass", "next_url": "/admin/"},
                follow_redirects=False,
            )
            assert login.status_code == 303
            token_val = login.cookies.get("user_token")
            assert token_val is not None
            denied = await c.get("/admin/", cookies={"user_token": token_val})
            assert denied.status_code == 403

            # 2. Execute documented break-glass recovery SQL from docs/admin-password-removal-migration.md
            async with get_session() as s:
                await s.execute(text("UPDATE users SET is_admin = 1 WHERE email = 'operator@example.com'"))
                await s.commit()

            # 3. Authorization follows the database, so the same session now reaches the panel
            dashboard = await c.get("/admin/", cookies={"user_token": token_val})
            assert dashboard.status_code == 200

    @pytest.mark.anyio
    async def test_login_preserves_password_whitespace(self, setup_db):
        """Passwords are compared verbatim; they must not be stripped."""
        from portal.database import create_user, get_session

        async with get_session() as s:
            await create_user(
                s,
                email="spacey-admin@example.com",
                display_name="Spacey Admin",
                password_hash=hash_password(" padded-pass "),
                email_verified=True,
                is_admin=True,
            )

        async with _client() as c:
            stripped = await c.post(
                "/login",
                data={"email": "spacey-admin@example.com", "password": "padded-pass", "next_url": "/admin/"},
                follow_redirects=False,
            )
            verbatim = await c.post(
                "/login",
                data={"email": "spacey-admin@example.com", "password": " padded-pass ", "next_url": "/admin/"},
                follow_redirects=False,
            )

        # The trimmed password must not match a credential whose spaces are real.
        assert stripped.status_code == 403
        # The exact credential still signs in.
        assert verbatim.status_code == 303
        assert verbatim.headers["location"] == "/admin/"
        assert "user_token" in verbatim.headers.get("set-cookie", "")

    @pytest.mark.anyio
    async def test_logout_clears_current_and_legacy_cookies(self):
        """Logout must clear both the new user_token and the legacy admin_token."""
        async with _client() as c:
            resp = await c.get("/admin/logout", follow_redirects=False)

        assert resp.status_code == 303
        cleared = {header.split("=", 1)[0].strip().lower() for header in resp.headers.get_list("set-cookie")}
        assert "user_token" in cleared
        assert "admin_token" in cleared
        assert "session_token" in cleared

    @pytest.mark.anyio
    async def test_logout_clears_cookie(self):
        async with _client() as c:
            resp = await c.get("/admin/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


class TestStaleAdminToken:
    """Authorization must follow current database state, not the token's claims.

    A ``user_token`` lives for ``jwt_expiry_seconds`` (24h by default), so if the
    ``is_admin`` claim alone were trusted, demoting, deactivating, or deleting an
    administrator would not revoke their access until the token expired.
    """

    @staticmethod
    async def _make_admin(email="stale-admin@example.com"):
        from portal.database import create_user, get_session

        async with get_session() as s:
            user = await create_user(
                s,
                email=email,
                display_name="Stale Admin",
                password_hash=hash_password("test-admin-pass"),
                is_admin=True,
            )
        return user

    @pytest.mark.anyio
    async def test_active_admin_token_allowed(self, setup_db):
        """Baseline: an active admin with a valid token can reach the dashboard."""
        user = await self._make_admin()
        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=True)}

        async with _client() as c:
            resp = await c.get("/admin/", cookies=cookie)
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_demoted_admin_token_denied(self, setup_db):
        """A token issued while admin, then demoted in the DB, must be denied."""
        user = await self._make_admin()
        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=True)}

        from sqlalchemy import update as sa_update

        from portal.database import get_session
        from portal.models import User

        async with get_session() as s:
            await s.execute(sa_update(User).where(User.id == user.id).values(is_admin=False))

        async with _client() as c:
            resp = await c.get("/admin/", cookies=cookie)
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_deactivated_admin_token_denied(self, setup_db):
        """A token issued while admin, then deactivated in the DB, must be denied."""
        user = await self._make_admin()
        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=True)}

        from portal.database import get_session, update_user_active

        async with get_session() as s:
            await update_user_active(s, user.id, is_active=False)

        async with _client() as c:
            resp = await c.get("/admin/", cookies=cookie)
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_deleted_admin_token_denied(self, setup_db):
        """A token for an administrator who no longer exists must be denied."""
        user = await self._make_admin()
        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=True)}

        from portal.database import delete_user, get_session

        async with get_session() as s:
            await delete_user(s, user.id)

        async with _client() as c:
            resp = await c.get("/admin/", cookies=cookie)
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_demoted_admin_token_denied_on_admin_route(self, setup_db):
        """Admin-only routes (e.g. /admin/users/) also honour the demotion."""
        user = await self._make_admin()
        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=True)}

        from sqlalchemy import update as sa_update

        from portal.database import get_session
        from portal.models import User

        async with get_session() as s:
            await s.execute(sa_update(User).where(User.id == user.id).values(is_admin=False))

        async with _client() as c:
            resp = await c.get("/admin/users/", cookies=cookie)
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_event_owner_denied_on_admin_route(self, seed_event):
        """A normal event owner cannot reach Admin-only routes (e.g. /admin/users/)."""
        event, _, _ = seed_event
        from portal.database import create_user, get_session, set_event_membership

        async with get_session() as s:
            user = await create_user(
                s,
                email="event-owner-only@example.com",
                display_name="Event Owner Only",
                password_hash=hash_password("owner-pass"),
                is_admin=False,
            )
            await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")

        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=False)}
        async with _client() as c:
            resp = await c.get("/admin/users/", cookies=cookie)
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Guard tests
# ---------------------------------------------------------------------------


class TestRequireAdmin:
    @pytest.mark.anyio
    async def test_dashboard_requires_auth(self):
        async with _client() as c:
            resp = await c.get("/admin/", follow_redirects=False)
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_event_list_requires_auth(self):
        async with _client() as c:
            resp = await c.get("/admin/events/", follow_redirects=False)
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_dashboard_with_valid_cookie(self, admin_cookie):
        async with _client() as c:
            resp = await c.get("/admin/", cookies=admin_cookie)
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_confirm_popup_is_a_labelled_dialog(self, admin_cookie):
        async with _client() as c:
            resp = await c.get("/admin/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert '<dialog id="custom-confirm-modal"' in resp.text
        assert 'aria-labelledby="custom-confirm-message"' in resp.text
        assert '<div id="custom-confirm-modal"' not in resp.text

    @pytest.mark.anyio
    async def test_invalid_cookie_rejected(self):
        async with _client() as c:
            resp = await c.get("/admin/", cookies={"user_token": "garbage"})
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_room_only_route_allowed_for_event_owner(self, seed_event):
        """An event owner is authorized for room-only routes within their event."""
        event, room, _ = seed_event
        from portal.database import create_user, get_session, set_event_membership

        async with get_session() as s:
            user = await create_user(
                s,
                email="owner@example.com",
                display_name="Event Owner",
                password_hash=hash_password("owner-pass"),
            )
            await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")

        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=False)}
        async with _client() as c:
            resp = await c.get(f"/api/rooms/{room.id}/floor-transcription/status", cookies=cookie)
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_room_only_route_denied_for_other_event_owner(self, seed_event):
        """An event owner of a different event is denied on room-only routes."""
        _, room, _ = seed_event
        from portal.database import create_event, create_user, get_session, set_event_membership

        async with get_session() as s:
            other_event = await create_event(s, slug="othercon", display_name="OtherCon")
            user = await create_user(
                s,
                email="other-owner@example.com",
                display_name="Other Owner",
                password_hash=hash_password("owner-pass"),
            )
            await set_event_membership(s, user_id=user.id, event_id=other_event.id, role="event_owner")

        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=False)}
        async with _client() as c:
            resp = await c.get(f"/api/rooms/{room.id}/floor-transcription/status", cookies=cookie)
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_room_only_route_allowed_for_room_coordinator(self, seed_event):
        """A room coordinator is authorized for room-only routes for their room."""
        _, room, _ = seed_event
        from portal.database import create_user, get_session, set_room_membership

        async with get_session() as s:
            user = await create_user(
                s,
                email="coord@example.com",
                display_name="Room Coord",
                password_hash=hash_password("coord-pass"),
            )
            await set_room_membership(s, user_id=user.id, room_id=room.id, role="room_coordinator")

        cookie = {"user_token": create_user_token(user_id=user.id, email=user.email, is_admin=False)}
        async with _client() as c:
            resp = await c.get(f"/api/rooms/{room.id}/floor-transcription/status", cookies=cookie)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Home page
# ---------------------------------------------------------------------------


class TestHomePage:
    @pytest.mark.anyio
    async def test_home_renders(self):
        async with _client() as c:
            resp = await c.get("/")
        assert resp.status_code == 200
        assert b"VoxBento" in resp.content

    @pytest.fixture
    async def named_event(self):
        from portal.database import create_booth, create_event, create_room, get_session

        async with get_session() as s:
            event = await create_event(s, slug="zephyr-2031", display_name="Zephyr Summit 2031")
            room = await create_room(s, event_id=event.id, display_name="Quartz Hall")
            booth = await create_booth(
                s, event_id=event.id, room_id=room.id, language_code="ja", language_name="Zephyrese"
            )
        return event, booth

    @staticmethod
    def _assert_no_events(html: str):
        for text in ("Zephyr Summit 2031", "Quartz Hall", "Zephyrese", "/listener/zephyr-2031"):
            assert text not in html
        for text in ("Live Dashboard", "Public Events", "Your Assigned Booths", "Enterprise-Ready"):
            assert text not in html

    @pytest.mark.anyio
    async def test_home_hides_events_from_anonymous_visitors(self, named_event):
        async with _client() as c:
            resp = await c.get("/")
        assert resp.status_code == 200
        self._assert_no_events(resp.text)
        assert "Make Every Event Multilingual" in resp.text

    @pytest.mark.anyio
    async def test_home_hides_events_and_booths_from_signed_in_interpreter(self, named_event):
        from portal.database import create_user, get_session, set_booth_membership

        _, booth = named_event
        async with get_session() as s:
            user = await create_user(s, email="interp@example.com", display_name="Interp")
            await set_booth_membership(s, user_id=user.id, booth_id=booth.id, role="interpreter")
        cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}
        async with _client() as c:
            resp = await c.get("/", cookies=cookies)
        assert resp.status_code == 200
        self._assert_no_events(resp.text)

    @pytest.mark.anyio
    async def test_home_dashboard_link_goes_to_interpreter_lobby_for_booth_only_user(self, named_event):
        from portal.database import create_user, get_session, set_booth_membership

        _, booth = named_event
        async with get_session() as s:
            user = await create_user(s, email="interp@example.com", display_name="Interp")
            await set_booth_membership(s, user_id=user.id, booth_id=booth.id, role="interpreter")
        cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}
        async with _client() as c:
            resp = await c.get("/", cookies=cookies)
        assert 'href="/interpreter"' in resp.text
        assert 'href="/account"' in resp.text

    @pytest.mark.anyio
    async def test_home_dashboard_link_stays_on_account_without_memberships(self):
        from portal.database import create_user, get_session

        async with get_session() as s:
            user = await create_user(s, email="plain@example.com", display_name="Plain")
        cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}
        async with _client() as c:
            resp = await c.get("/", cookies=cookies)
        assert 'href="/interpreter"' not in resp.text

    @pytest.mark.anyio
    async def test_home_hides_events_from_event_owner_and_links_to_workspace(self, named_event):
        from portal.database import create_user, get_session, set_event_membership

        event, _ = named_event
        async with get_session() as s:
            user = await create_user(s, email="owner@example.com", display_name="Owner")
            await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")
        cookies = {"user_token": create_user_token(user_id=user.id, email=user.email)}
        async with _client() as c:
            resp = await c.get("/", cookies=cookies)
        assert resp.status_code == 200
        self._assert_no_events(resp.text)
        assert 'href="/workspace/"' in resp.text


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class TestDashboard:
    @pytest.mark.anyio
    async def test_dashboard_empty(self, admin_cookie):
        async with _client() as c:
            resp = await c.get("/admin/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"set up your first event" in resp.content

    @pytest.mark.anyio
    async def test_dashboard_shows_events(self, admin_cookie, seed_event):
        async with _client() as c:
            resp = await c.get("/admin/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"TestCon 2026" in resp.content
        assert b"English" in resp.content


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------


class TestEventCRUD:
    @pytest.mark.anyio
    async def test_event_list(self, admin_cookie, seed_event):
        async with _client() as c:
            resp = await c.get("/admin/events/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"testcon" in resp.content

    @pytest.mark.anyio
    async def test_event_list_shows_readable_created_date(self, admin_cookie, seed_event):
        event, _, _ = seed_event
        async with _client() as c:
            resp = await c.get("/admin/events/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert event.created_at.strftime("%b %d, %Y, %H:%M").encode() in resp.content
        assert event.created_at.strftime("%Y-%m-%d").encode() not in resp.content

    @pytest.mark.anyio
    @pytest.mark.parametrize("search_term", ("SUMMIT", "fossasia"))
    async def test_event_list_searches_by_slug_or_display_name_case_insensitively(
        self, admin_cookie, seed_event, search_term
    ):
        from portal.database import create_event, get_session

        async with get_session() as session:
            await create_event(session, slug="community-summit-2026", display_name="FOSSASIA Community")

        async with _client() as c:
            resp = await c.get("/admin/events/", params={"search": search_term}, cookies=admin_cookie)

        assert resp.status_code == 200
        assert "FOSSASIA Community" in resp.text
        assert "community-summit-2026" in resp.text
        assert "testcon" not in resp.text
        assert f'value="{search_term}"' in resp.text
        assert '<label for="admin-event-search" class="search-label">Search events by name or slug</label>' in resp.text
        assert 'aria-label="Search events by name or slug"' in resp.text

    @pytest.mark.anyio
    @pytest.mark.parametrize("search_term", ("", "   "))
    async def test_event_list_empty_search_shows_all_events(self, admin_cookie, seed_event, search_term):
        from portal.database import create_event, get_session

        async with get_session() as session:
            await create_event(session, slug="unrelated-event", display_name="Unrelated Event")

        async with _client() as c:
            resp = await c.get("/admin/events/", params={"search": search_term}, cookies=admin_cookie)

        assert resp.status_code == 200
        assert "TestCon 2026" in resp.text
        assert "Unrelated Event" in resp.text
        assert 'value=""' in resp.text
        assert 'href="/admin/events/" class="btn">Clear</a>' not in resp.text

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("search_term", "literal_name", "near_match"),
        [
            ("Alpha_Beta", "Alpha_Beta", "AlphaXBeta"),
            ("Alpha%Beta", "Alpha%Beta", "AlphaXBeta"),
            ("Alpha\\Beta", "Alpha\\Beta", "AlphaBeta"),
        ],
    )
    async def test_event_list_search_treats_like_wildcards_literally(
        self, admin_cookie, search_term, literal_name, near_match
    ):
        from portal.database import create_event, get_session

        async with get_session() as session:
            await create_event(session, slug="literal-match", display_name=literal_name)
            await create_event(session, slug="near-match", display_name=near_match)

        async with _client() as c:
            resp = await c.get("/admin/events/", params={"search": search_term}, cookies=admin_cookie)

        assert resp.status_code == 200
        assert "literal-match" in resp.text
        assert "near-match" not in resp.text

    @pytest.mark.anyio
    async def test_event_list_pagination_url_encodes_search(self, admin_cookie):
        from portal.database import create_event, get_session

        search_term = "special&#"
        async with get_session() as session:
            for index in range(21):
                await create_event(
                    session,
                    slug=f"special-search-{index}",
                    display_name=f"{search_term} event {index}",
                )

        async with _client() as c:
            resp = await c.get("/admin/events/", params={"search": search_term}, cookies=admin_cookie)

        assert resp.status_code == 200
        assert 'href="?page=2&amp;search=special%26%23"' in resp.text

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("event_count", "requested_page", "expected_page"),
        [
            pytest.param(1, 2, 1, id="single-result"),
            pytest.param(21, 99, 2, id="last-page"),
        ],
    )
    async def test_event_list_clamps_out_of_range_search_page(
        self, admin_cookie, event_count, requested_page, expected_page
    ):
        from portal.database import create_event, get_session

        search_term = "pagination-target"
        async with get_session() as session:
            for index in range(event_count):
                await create_event(
                    session,
                    slug=f"{search_term}-{index}",
                    display_name=f"Pagination Target {index}",
                )

        async with _client() as c:
            resp = await c.get(
                "/admin/events/",
                params={"search": search_term, "page": requested_page},
                cookies=admin_cookie,
            )

        assert resp.status_code == 200
        assert f"Page {requested_page} is out of range. Showing page {expected_page}." in resp.text
        assert f'href="?page={expected_page}&amp;search={search_term}">View page {expected_page}</a>' in resp.text
        assert f"{search_term}-{event_count - 1}" in resp.text
        assert "No events match search" not in resp.text

    @pytest.mark.anyio
    async def test_event_list_out_of_range_page_with_no_matches_only_shows_empty_state(self, admin_cookie, seed_event):
        async with _client() as c:
            resp = await c.get(
                "/admin/events/",
                params={"search": "no-such-event", "page": 99},
                cookies=admin_cookie,
            )

        assert resp.status_code == 200
        assert "No events match search" in resp.text
        assert "Page 99 is out of range" not in resp.text
        assert "View page" not in resp.text

    @pytest.mark.anyio
    async def test_event_list_search_with_no_matches_shows_clearable_empty_state(self, admin_cookie, seed_event):
        async with _client() as c:
            resp = await c.get("/admin/events/", params={"search": "no-such-event"}, cookies=admin_cookie)

        assert resp.status_code == 200
        assert "No events match search" in resp.text
        assert 'name="search"' in resp.text
        assert 'href="/admin/events/" class="btn">Clear</a>' in resp.text
        assert "testcon" not in resp.text

    @pytest.mark.anyio
    async def test_create_event(self, admin_cookie):
        async with _client() as c:
            resp = await c.post(
                "/admin/events/",
                data={"slug": "newcon", "display_name": "NewCon 2026"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        # Verify it was created
        async with _client() as c:
            resp = await c.get("/admin/events/", cookies=admin_cookie)
        assert b"NewCon 2026" in resp.content

    @pytest.mark.anyio
    async def test_event_detail(self, admin_cookie, seed_event):
        event, _, _ = seed_event
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"TestCon 2026" in resp.content
        assert b"Main Hall" in resp.content

    @pytest.mark.anyio
    async def test_api_settings_update_and_clear_keys(self, admin_cookie, seed_event):
        from portal.crypto import decrypt_val
        from portal.database import get_event_by_id, get_session

        event, _, _ = seed_event
        keys = {
            "openai_api_key": " openai-secret ",
            "deepgram_api_key": "deepgram-secret",
            "nvidia_api_key": "nvidia-secret",
            "elevenlabs_api_key": "elevenlabs-secret",
            "translation_openai_api_key": "translation-openai-secret",
            "openrouter_api_key": "openrouter-secret",
            "gemini_api_key": "gemini-secret",
            "anthropic_api_key": "anthropic-secret",
            "groq_api_key": "groq-secret",
        }
        async with _client() as c:
            response = await c.post(
                f"/admin/events/{event.id}/api-settings",
                cookies=admin_cookie,
                data={"transcription_api_enabled": "1", **keys},
                follow_redirects=False,
            )
        assert response.status_code == 303

        async with get_session() as session:
            updated = await get_event_by_id(session, event.id)
            assert updated is not None
            assert updated.transcription_api_enabled is True
            for form_name, expected in keys.items():
                attribute = f"encrypted_{form_name}"
                assert decrypt_val(getattr(updated, attribute)) == expected.strip()

        async with _client() as c:
            response = await c.post(
                f"/admin/events/{event.id}/api-settings",
                cookies=admin_cookie,
                data={
                    "openai_api_key": "replacement",
                    "clear_openai_api_key": "1",
                    "deepgram_api_key": "   ",
                },
                follow_redirects=False,
            )
        assert response.status_code == 303

        async with get_session() as session:
            updated = await get_event_by_id(session, event.id)
            assert updated is not None
            assert updated.encrypted_openai_api_key is None
            assert decrypt_val(updated.encrypted_deepgram_api_key) == "deepgram-secret"

    @pytest.mark.anyio
    async def test_event_detail_shows_booth_links(self, admin_cookie, seed_event):
        event, _, _ = seed_event
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/", cookies=admin_cookie)
        assert b"/interpreter/testcon/1/en" in resp.content

    @pytest.mark.anyio
    async def test_event_detail_groups_booths_by_room(self, admin_cookie, seed_event):
        """With two rooms sharing a language, each booth row must say which room it is in."""
        event, main_hall, _ = seed_event
        from portal.database import create_booth, create_room, get_session

        async with get_session() as s:
            workshop = await create_room(s, event_id=event.id, display_name="Workshop B")
            await create_booth(s, event_id=event.id, room_id=main_hall.id, language_code="de", language_name="Deutsch")
            await create_booth(s, event_id=event.id, room_id=workshop.id, language_code="de", language_name="Deutsch")
            await create_booth(s, event_id=event.id, room_id=workshop.id, language_code="en", language_name="English")

        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/", cookies=admin_cookie)
        assert resp.status_code == 200
        html = resp.text
        assert "<th>Room</th>" in html
        assert html.count("<td>Main Hall</td>") == 2
        assert html.count("<td>Workshop B</td>") == 2
        # Rows follow the Rooms list order, then language within a room.
        rows = [
            f"/interpreter/testcon/{main_hall.id}/de",
            f"/interpreter/testcon/{main_hall.id}/en",
            f"/interpreter/testcon/{workshop.id}/de",
            f"/interpreter/testcon/{workshop.id}/en",
        ]
        positions = [html.index(f'href="{row}"') for row in rows]
        assert positions == sorted(positions)

    @pytest.mark.anyio
    async def test_delete_event(self, admin_cookie, seed_event):
        event, _, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        # Verify deleted
        async with _client() as c:
            resp = await c.get("/admin/events/", cookies=admin_cookie)
        assert b"testcon" not in resp.content

    @pytest.mark.anyio
    async def test_delete_event_with_a_relay_booth(self, admin_cookie, seed_event):
        """Relay Settings sets rooms.relay_booth_id; the event must still be deletable."""
        event, room, booth = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/edit",
                data={"form_section": "relay", "relay_booth_id": str(booth.id)},
                cookies=admin_cookie,
                follow_redirects=False,
            )
            assert resp.status_code == 303

        # 303 alone does not prove the relay was stored, and without it stored
        # this test would delete an ordinary event and miss the FK cycle.
        from portal.database import get_room_by_id, get_session

        async with get_session() as s:
            assert (await get_room_by_id(s, room.id)).relay_booth_id == booth.id

        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with _client() as c:
            resp = await c.get("/admin/events/", cookies=admin_cookie)
        assert b"testcon" not in resp.content

    @pytest.mark.anyio
    async def test_delete_event_rejects_a_room_coordinator(self, admin_cookie, seed_event):
        """require_owner_or_coordinator admits a room_coordinator for the whole event, and deleting one
        now destroys transcripts and OAuth grant state. Match the API-key routes and
        require an event owner."""
        from portal.auth import create_user_token
        from portal.database import create_room, create_user, get_session
        from portal.models import RoomMembership

        event, room, _ = seed_event
        async with get_session() as s:
            side = await create_room(s, event_id=event.id, display_name="Side Room")
            carol = await create_user(s, email="carol@example.com", display_name="Carol")
            s.add(RoomMembership(user_id=carol.id, room_id=side.id, role="room_coordinator"))
            carol_id = carol.id

        cookie = {"user_token": create_user_token(user_id=carol_id, email="carol@example.com")}
        async with _client() as c:
            # she really is a coordinator: her own room's page is allowed
            allowed = await c.get(f"/admin/events/{event.id}/rooms/{side.id}/", cookies=cookie)
            resp = await c.post(f"/admin/events/{event.id}/delete", cookies=cookie, follow_redirects=False)
        assert allowed.status_code == 200, "fixture is wrong; she is not a coordinator"
        assert resp.status_code == 403

        from portal.database import get_event_by_id

        async with get_session() as s:
            assert await get_event_by_id(s, event.id) is not None

    @pytest.mark.anyio
    async def test_delete_event_allows_an_event_owner(self, seed_event):
        """The tightened guard must still admit the event's own owner."""
        from portal.auth import create_user_token
        from portal.database import create_user, get_event_by_id, get_session
        from portal.models import EventMembership

        event, _, _ = seed_event
        async with get_session() as s:
            owner = await create_user(s, email="owner@example.com", display_name="Owner")
            s.add(EventMembership(user_id=owner.id, event_id=event.id, role="event_owner"))
            owner_id = owner.id

        cookie = {"user_token": create_user_token(user_id=owner_id, email="owner@example.com")}
        async with _client() as c:
            legacy = await c.post(f"/admin/events/{event.id}/delete", cookies=cookie, follow_redirects=False)
            assert legacy.status_code == 307
            assert legacy.headers["location"] == f"/workspace/events/{event.id}/delete"
            resp = await c.post(legacy.headers["location"], cookies=cookie, follow_redirects=False)
        assert resp.status_code == 303
        async with get_session() as s:
            assert await get_event_by_id(s, event.id) is None

    @pytest.mark.anyio
    async def test_event_not_found(self, admin_cookie):
        async with _client() as c:
            resp = await c.get("/admin/events/99999/", cookies=admin_cookie)
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_delete_event_not_found(self, admin_cookie):
        async with _client() as c:
            resp = await c.post(
                "/admin/events/99999/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Event not found."

    @pytest.mark.anyio
    async def test_delete_event_database_error(self, admin_cookie, seed_event, monkeypatch):
        from sqlalchemy.exc import SQLAlchemyError

        from portal.database import delete_event as real_delete_event

        event, _, _ = seed_event

        async def mock_delete_event(session, event_id):
            async def fail_flush():
                raise SQLAlchemyError("Simulated DB failure")

            monkeypatch.setattr(session, "flush", fail_flush)
            return await real_delete_event(session, event_id)

        monkeypatch.setattr("portal.routers.admin.delete_event", mock_delete_event)

        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/events/?error=delete_failed"

        # Verify redirect page renders error message and event was not deleted
        import re

        async with _client() as c:
            resp = await c.get("/admin/events/?error=delete_failed", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"Failed to delete event" in resp.content
        assert b"The event was not deleted." in resp.content
        assert b"testcon" in resp.content
        assert not re.search(rb"\sstyle\s*=", resp.content, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------


class TestBreadcrumbs:
    @pytest.mark.anyio
    @pytest.mark.parametrize("path", ["/admin/events/"])
    async def test_breadcrumb_marks_current_page_and_uses_slash_separator(self, admin_cookie, path):
        async with _client() as c:
            resp = await c.get(path, cookies=admin_cookie)
        assert resp.status_code == 200
        start = resp.text.index('<nav class="breadcrumb">')
        nav = resp.text[start : resp.text.index("</nav>", start)]
        assert 'class="breadcrumb-current" aria-current="page"' in nav
        assert "›" not in nav
        assert "<span>/</span>" in nav


class TestRoomCRUD:
    @pytest.mark.anyio
    async def test_room_list(self, admin_cookie, seed_event):
        event, _, _ = seed_event
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/rooms/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"Main Hall" in resp.content

    @pytest.mark.anyio
    async def test_room_list_search(self, admin_cookie, seed_event):
        event, _, _ = seed_event
        # Create an additional room to test search filtering
        async with _client() as c:
            await c.post(
                f"/admin/events/{event.id}/rooms/",
                data={"display_name": "Workshop Room"},
                cookies=admin_cookie,
                follow_redirects=False,
            )

        # Search for "Workshop" -> should return "Workshop Room" link and hide "Main Hall" link
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/rooms/?search=workshop", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b">Workshop Room</a>" in resp.content
        assert b">Main Hall</a>" not in resp.content

        # Search for non-existent room -> empty state message
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/rooms/?search=NonExistent", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"No rooms match search" in resp.content

        # Searching for literal "%" or "_" when no room names contain them should return empty match, not all rooms
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/rooms/?search=%25", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"No rooms match search" in resp.content

        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/rooms/?search=_", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"No rooms match search" in resp.content

        # Create a room with display_name containing a literal backslash
        async with _client() as c:
            await c.post(
                f"/admin/events/{event.id}/rooms/",
                data={"display_name": "Backslash \\ Room"},
                cookies=admin_cookie,
                follow_redirects=False,
            )

        # Search using the URL-encoded backslash
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/rooms/?search=%5C", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b">Backslash \\ Room</a>" in resp.content

        # Whitespace-only search query should be ignored and render all rooms without active search state
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/rooms/?search=%20%20", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b">Main Hall</a>" in resp.content
        assert b">Workshop Room</a>" in resp.content
        assert b"No rooms match search" not in resp.content

    @pytest.mark.anyio
    async def test_create_room(self, admin_cookie, seed_event):
        event, _, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/",
                data={"display_name": "Track B"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with _client() as c:
            resp = await c.get(f"/admin/events/{event.id}/rooms/", cookies=admin_cookie)
        assert b"Track B" in resp.content

    @pytest.mark.anyio
    async def test_room_detail(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 200
        assert b"Main Hall" in resp.content
        assert b"English" in resp.content

    @pytest.mark.anyio
    async def test_room_detail_shows_interpreter_urls(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/",
                cookies=admin_cookie,
            )
        assert b"/interpreter/testcon/1/en" in resp.content
        assert b"/listener/testcon" in resp.content

    @pytest.mark.anyio
    async def test_room_detail_shows_audio_delay_setting(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 200
        assert b"Audio Synchronization Delay (ms)" in resp.content
        assert b'name="audio_delay_ms"' in resp.content

    @pytest.mark.anyio
    async def test_update_room_ai_interpretation_settings(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/edit",
                data={
                    "form_section": "translation",
                    "floor_translation_provider": "openai",
                    "floor_translation_model": "gpt-4o-mini",
                    "floor_ai_interpreter_persona": "Technical conference interpreter",
                    "floor_ai_interpretation_style": "Formal and concise",
                    "floor_ai_vocabulary_enabled": "on",
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

        from portal.database import get_room_by_id, get_session

        async with get_session() as session:
            updated = await get_room_by_id(session, room.id)
            assert updated.floor_ai_interpreter_persona == "Technical conference interpreter"
            assert updated.floor_ai_interpretation_style == "Formal and concise"
            assert updated.floor_ai_vocabulary_enabled is True

    @pytest.mark.anyio
    async def test_room_vocabulary_upload_preview_and_export(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        csv_content = (
            "source_term,target_language,target_term,description,case_sensitive,match_type,priority\n"
            "Voxbento,all,Voxbento,Product name,true,exact,100\n"
            "WebRTC,de,WebRTC,Protocol,false,phrase,90\n"
            "US,de,US,Country,true,exact,80\n"
            "us,de,uns,Pronoun,true,exact,70\n"
        )
        async with _client() as c:
            upload = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/ai-vocabulary/upload",
                data={"import_mode": "append"},
                files={"vocabulary_file": ("glossary.csv", csv_content, "text/csv")},
                cookies=admin_cookie,
                follow_redirects=False,
            )
            assert upload.status_code == 303
            assert "vocab_imported=4" in upload.headers["location"]

            page = await c.get(upload.headers["location"], cookies=admin_cookie)
            assert page.status_code == 200
            assert b"Imported 4 rows" in page.content
            assert b"Voxbento" in page.content
            assert b"WebRTC" in page.content

            overlapping_upload = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/ai-vocabulary/upload",
                data={"import_mode": "append"},
                files={
                    "vocabulary_file": (
                        "overlap.csv",
                        "source_term,target_language,target_term,case_sensitive\nuS,de,wir,false\n",
                        "text/csv",
                    )
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
            assert overlapping_upload.status_code == 303
            assert "vocab_imported=0" in overlapping_upload.headers["location"]
            assert "Existing+duplicate" in overlapping_upload.headers["location"]

            export = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/ai-vocabulary/export",
                cookies=admin_cookie,
            )
        assert export.status_code == 200
        assert export.headers["content-type"].startswith("text/csv")
        assert "Voxbento,all,Voxbento" in export.text
        assert "US,de,US" in export.text
        assert "us,de,uns" in export.text

    @pytest.mark.anyio
    async def test_room_vocabulary_upload_rejects_cross_event_room(self, admin_cookie, seed_event):
        """A room belonging to a different event must not accept this event's vocabulary."""
        from portal.database import create_event, create_room, get_session

        event, _, _ = seed_event
        async with get_session() as s:
            other_event = await create_event(s, slug="othercon", display_name="OtherCon")
            other_room = await create_room(s, event_id=other_event.id, display_name="Other Hall")

        async with _client() as c:
            upload = await c.post(
                f"/admin/events/{event.id}/rooms/{other_room.id}/ai-vocabulary/upload",
                data={"import_mode": "append"},
                files={
                    "vocabulary_file": (
                        "x.csv",
                        "source_term,target_language,target_term\nVoxbento,all,Voxbento\n",
                        "text/csv",
                    )
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert upload.status_code == 404

    @pytest.mark.anyio
    async def test_vocabulary_export_delete_reject_cross_event_room(self, admin_cookie, seed_event):
        """Export and delete must 404 for a room belonging to another event."""
        from portal.database import create_event, create_room, get_session

        event, _, _ = seed_event
        async with get_session() as s:
            other_event = await create_event(s, slug="xcon", display_name="XCon")
            other_room = await create_room(s, event_id=other_event.id, display_name="X Hall")

        async with _client() as c:
            export = await c.get(
                f"/admin/events/{event.id}/rooms/{other_room.id}/ai-vocabulary/export",
                cookies=admin_cookie,
            )
            delete = await c.post(
                f"/admin/events/{event.id}/rooms/{other_room.id}/ai-vocabulary/delete",
                data={"entry_id": "all"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert export.status_code == 404
        assert delete.status_code == 404

    @pytest.mark.anyio
    async def test_validate_vocabulary_scope_enforces_hierarchy(self, seed_event):
        """The scope validator rejects a booth that belongs to another room/event."""
        from portal.database import create_booth, create_event, create_room, get_session
        from portal.translations.vocabulary import VocabularyScopeError, validate_vocabulary_scope

        event, room, booth = seed_event
        async with get_session() as s:
            other_event = await create_event(s, slug="othercon2", display_name="OtherCon2")
            other_room = await create_room(s, event_id=other_event.id, display_name="Other Hall")
            other_booth = await create_booth(
                s, event_id=other_event.id, room_id=other_room.id, language_code="fr", language_name="French"
            )

        async with get_session() as s:
            await validate_vocabulary_scope(s, event.id, room_id=room.id, booth_id=booth.id)
            with pytest.raises(VocabularyScopeError):
                await validate_vocabulary_scope(s, event.id, room_id=other_room.id)
            with pytest.raises(VocabularyScopeError):
                await validate_vocabulary_scope(s, event.id, room_id=room.id, booth_id=other_booth.id)

    @pytest.mark.anyio
    async def test_room_vocabulary_upload_reports_invalid_rows(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        csv_content = "source_term,target_language,target_term,match_type\nBad,de,Term,regex\n"
        async with _client() as c:
            upload = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/ai-vocabulary/upload",
                files={"vocabulary_file": ("glossary.csv", csv_content, "text/csv")},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert upload.status_code == 303
        assert "vocab_imported=0" in upload.headers["location"]
        assert "unsupported+match_type" in upload.headers["location"]

    @pytest.mark.anyio
    async def test_update_room_audio_delay(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/edit",
                data={
                    "display_name": "Main Hall",
                    "jitsi_url": "",
                    "relay_booth_id": "none",
                    "audio_delay_ms": "2500",
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

        from portal.database import get_room_by_id, get_session

        async with get_session() as s:
            updated = await get_room_by_id(s, room.id)
            assert updated is not None
            assert updated.audio_delay_ms == 2500

    @pytest.mark.anyio
    async def test_update_room_audio_delay_rejects_out_of_range(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/edit",
                data={
                    "display_name": "Main Hall",
                    "jitsi_url": "",
                    "relay_booth_id": "none",
                    "audio_delay_ms": "10001",
                },
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 400

    @pytest.mark.anyio
    async def test_listener_page_includes_room_audio_delay(self, seed_event):
        event, room, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_room = await s.get(type(room), room.id)
            assert db_event is not None
            assert db_room is not None
            db_event.listener_join_code = "ROOM42"
            db_room.audio_delay_ms = 5000

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        assert b'"audio_delay_ms": 5000' in resp.content

    @pytest.mark.anyio
    async def test_listener_room_audio_delay_endpoint_reflects_updates(self, seed_event):
        event, room, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_room = await s.get(type(room), room.id)
            assert db_event is not None
            assert db_room is not None
            db_event.listener_join_code = "ROOM42"
            db_room.audio_delay_ms = 2000

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}/rooms/{room.id}/audio-delay?code=ROOM42")

        assert resp.status_code == 200
        assert resp.json() == {"audio_delay_ms": 2000}

    @pytest.mark.anyio
    async def test_delete_room(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

    @pytest.mark.anyio
    async def test_delete_room_rejects_cross_event_target_without_deleting_dependents(self, room_scope_events):
        from portal.database import get_room_by_id, get_session
        from portal.models import BoothMembership, DBBooth, InviteToken

        scope = room_scope_events
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_b']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            assert await get_room_by_id(s, scope["room_b"]) is not None
            assert await s.get(DBBooth, scope["booth_b"]) is not None
            assert await s.get(InviteToken, scope["invite_b"]) is not None
            assert await s.get(BoothMembership, scope["booth_membership_b"]) is not None

    @pytest.mark.anyio
    async def test_delete_room_allows_own_event_and_preserves_other_event(self, room_scope_events):
        from portal.database import get_room_by_id, get_session
        from portal.models import DBBooth

        scope = room_scope_events
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/{scope['room_a']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/workspace/events/{scope['event_a']}/rooms/"
        async with get_session() as s:
            assert await get_room_by_id(s, scope["room_a"]) is None
            assert await s.get(DBBooth, scope["booth_a"]) is None
            assert await get_room_by_id(s, scope["room_b"]) is not None
            assert await s.get(DBBooth, scope["booth_b"]) is not None

    @pytest.mark.anyio
    async def test_delete_room_rejects_foreign_event_owner(self, room_scope_events):
        from portal.database import get_room_by_id, get_session

        scope = room_scope_events
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_b']}/rooms/{scope['room_b']}/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 403
        async with get_session() as s:
            assert await get_room_by_id(s, scope["room_b"]) is not None

    @pytest.mark.anyio
    async def test_delete_room_returns_404_for_nonexistent_room(self, room_scope_events):
        scope = room_scope_events
        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{scope['event_a']}/rooms/99999/delete",
                cookies=scope["owner_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_delete_room_rejects_admin_event_mismatch(self, admin_cookie, room_scope_events):
        from portal.database import get_room_by_id, get_session

        scope = room_scope_events
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{scope['event_a']}/rooms/{scope['room_b']}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 404
        async with get_session() as s:
            assert await get_room_by_id(s, scope["room_b"]) is not None

    @pytest.mark.anyio
    async def test_room_coordinator_cannot_delete_room_on_legacy_path(self, room_scope_events):
        # Managing rooms is an Event owner permission (#757), on /admin/ paths too.
        from portal.database import get_room_by_id, get_session

        scope = room_scope_events
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{scope['event_a']}/rooms/{scope['room_a']}/delete",
                cookies=scope["coordinator_cookie"],
                follow_redirects=False,
            )
        assert resp.status_code == 403
        async with get_session() as s:
            assert await get_room_by_id(s, scope["room_a"]) is not None


# ---------------------------------------------------------------------------
# Booth CRUD
# ---------------------------------------------------------------------------


class TestBoothCRUD:
    @pytest.mark.anyio
    async def test_booth_list(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 200
        assert b"English" in resp.content

    @pytest.mark.anyio
    async def test_create_booth(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/",
                data={"language_code": "fr", "language_name": "French"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/",
                cookies=admin_cookie,
            )
        assert b"French" in resp.content

    @pytest.mark.anyio
    async def test_create_duplicate_booth_shows_error(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/",
                data={"language_code": "en", "language_name": "English"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"].endswith("/booths/?error=duplicate_language")
            page = await c.get(resp.headers["location"], cookies=admin_cookie)
        assert "Main Hall already has a booth for that language." in page.text

    @pytest.mark.anyio
    async def test_setup_wizard_duplicate_booth_shows_error(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/setup/booths",
                data={"room_id": str(room.id), "language_code": "en"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"].endswith(f"/setup/booths?room_id={room.id}&error=duplicate_language")
            page = await c.get(resp.headers["location"], cookies=admin_cookie)
        assert "That room already has a booth for that language." in page.text

    @pytest.mark.parametrize("wizard", [False, True])
    @pytest.mark.anyio
    async def test_duplicate_booth_inserted_concurrently_shows_error(
        self, admin_cookie, seed_event, monkeypatch, wizard
    ):
        from portal.routers import admin

        event, room, _ = seed_event
        real_check = admin._room_has_booth_language
        calls = []

        async def check_after_concurrent_insert(session, room_id, language_code):
            calls.append(room_id)
            if len(calls) == 1:
                return False
            return await real_check(session, room_id, language_code)

        monkeypatch.setattr(admin, "_room_has_booth_language", check_after_concurrent_insert)
        if wizard:
            url = f"/admin/events/{event.id}/setup/booths"
            data = {"room_id": str(room.id), "language_code": "en"}
        else:
            url = f"/admin/events/{event.id}/rooms/{room.id}/booths/"
            data = {"language_code": "en", "language_name": "English"}
        async with _client() as c:
            resp = await c.post(url, data=data, cookies=admin_cookie, follow_redirects=False)
        assert resp.status_code == 303
        assert "error=duplicate_language" in resp.headers["location"]
        assert len(calls) == 2

    @pytest.mark.anyio
    async def test_create_booth_in_missing_room_is_not_reported_as_duplicate(self, admin_cookie, seed_event):
        event, _, _ = seed_event
        async with _client() as c:
            booths_page = await c.post(
                f"/admin/events/{event.id}/rooms/9999/booths/",
                data={"language_code": "en", "language_name": "English"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
            wizard = await c.post(
                f"/admin/events/{event.id}/setup/booths",
                data={"room_id": "9999", "language_code": "en"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        # A room that isn't part of the event is not found (#757), never reported as a duplicate.
        assert booths_page.status_code == 404
        assert wizard.status_code == 303
        assert "duplicate_language" not in wizard.headers["location"]

    @pytest.mark.anyio
    async def test_booth_detail(self, admin_cookie, seed_event):
        event, room, booth = seed_event
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 200
        assert b"English" in resp.content
        assert b"whep" in resp.content.lower()

    @pytest.mark.anyio
    async def test_booth_detail_shows_whep_url(self, admin_cookie, seed_event):
        event, room, booth = seed_event
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/",
                cookies=admin_cookie,
            )
        assert b"testcon/1/en/whep" in resp.content

    @pytest.mark.anyio
    async def test_booth_detail_shows_mediamtx_path(self, admin_cookie, seed_event):
        event, room, booth = seed_event
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/",
                cookies=admin_cookie,
            )
        assert b"testcon/1/en" in resp.content

    @pytest.mark.anyio
    async def test_team_page_shows_booth_invite_links(self, admin_cookie, seed_event):
        # Invite links moved from the booth page to the event's Team page (#757).
        event, room, booth = seed_event
        from portal.database import create_invite_token, get_session

        async with get_session() as s:
            await create_invite_token(s, booth_id=booth.id, role="interpreter", label="Alice")

        async with _client() as c:
            booth_page = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/",
                cookies=admin_cookie,
            )
            team_page = await c.get(f"/admin/events/{event.id}/team/", cookies=admin_cookie)
        assert b"Alice" not in booth_page.content
        assert b"/team/" in booth_page.content
        assert b"Alice" in team_page.content
        assert b"Interpreter" in team_page.content

    @pytest.mark.anyio
    async def test_delete_booth(self, admin_cookie, seed_event):
        event, room, booth = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303

    @pytest.mark.anyio
    async def test_delete_relay_booth_clears_the_rooms_pointer(self, admin_cookie, seed_event):
        """The room page shows Relay Booth as None after the delete; the row must agree."""
        from portal.database import get_room_by_id, get_session

        event, room, booth = seed_event
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/edit",
                data={"form_section": "relay", "relay_booth_id": str(booth.id)},
                cookies=admin_cookie,
                follow_redirects=False,
            )
            assert resp.status_code == 303
        async with get_session() as s:
            assert (await get_room_by_id(s, room.id)).relay_booth_id == booth.id

        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{booth.id}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            assert (await get_room_by_id(s, room.id)).relay_booth_id is None

    @pytest.mark.anyio
    async def test_deleted_relay_booth_is_not_resurrected_by_a_reused_id(self, admin_cookie, seed_event):
        """A new booth reusing the deleted booth's rowid must not inherit the relay slot.

        Without the fix the interpreter is handed the new booth's WHEP URL, which can
        be another room in another language, while the room page still shows None.
        """
        from portal.auth import create_participant_token
        from portal.database import (
            create_booth,
            create_room,
            get_room_by_id,
            get_session,
            list_booths_for_room,
        )

        # seed_event's booth is where the interpreter sits, so the relay booth is created
        # after it and holds the highest booth id. Deleting the highest id is what frees it
        # for reuse; deleting a lower one leaves max() untouched and proves nothing.
        event, room, own = seed_event
        async with get_session() as s:
            relay_booth = await create_booth(
                s, event_id=event.id, room_id=room.id, language_code="es", language_name="Spanish"
            )
            other_room = await create_room(s, event_id=event.id, display_name="Hall B")
            own_id, other_room_id, relay_id = own.id, other_room.id, relay_booth.id
        assert relay_id > own_id

        async with _client() as c:
            await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/edit",
                data={"form_section": "relay", "relay_booth_id": str(relay_id)},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        session_cookie = {
            "session_token": create_participant_token(
                booth_id=own_id,
                role="interpreter",
                event_slug=event.slug,
                room_id=room.id,
                language_code="en",
            )
        }
        async with _client() as c:
            page = await c.get(f"/interpreter/{event.slug}/{room.id}/en", cookies=session_cookie)
        assert resolve_relay_attr(page.text) != "", "relay was never configured, so nothing is proven"

        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/{relay_id}/delete",
                cookies=admin_cookie,
                follow_redirects=False,
            )
            assert resp.status_code == 303
            # a booth added to the other room can take the freed rowid
            await c.post(
                f"/admin/events/{event.id}/rooms/{other_room_id}/booths/",
                data={"language_code": "fr", "language_name": "French"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        async with get_session() as s:
            new_booth = (await list_booths_for_room(s, other_room_id))[0]
            assert (await get_room_by_id(s, room.id)).relay_booth_id is None
        # without this the replacement never occupies the freed id and the stale-pointer
        # path is not exercised at all
        assert new_booth.id == relay_id, f"no rowid reuse: {new_booth.id} != {relay_id}"

        async with _client() as c:
            page = await c.get(f"/interpreter/{event.slug}/{room.id}/en", cookies=session_cookie)
        after = resolve_relay_attr(page.text)
        assert f"/{other_room_id}/{new_booth.language_code}/" not in after, after
        assert not after.startswith("http"), f"still handed a relay stream: {after}"

    @pytest.mark.anyio
    async def test_booth_not_found(self, admin_cookie, seed_event):
        event, room, _ = seed_event
        async with _client() as c:
            resp = await c.get(
                f"/admin/events/{event.id}/rooms/{room.id}/booths/99999/",
                cookies=admin_cookie,
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Auth unit tests
# ---------------------------------------------------------------------------


class TestAdminToken:
    @pytest.mark.anyio
    async def test_create_user_token_has_admin_claim(self, setup_db):
        token = create_user_token(user_id=1, email="admin@example.com", is_admin=True)
        payload = decode_token(token)
        assert payload["is_admin"] is True
        assert payload["user"] is True
        assert "exp" in payload
        assert "iat" in payload

    @pytest.mark.anyio
    async def test_admin_user_token_is_valid_jwt(self, setup_db):
        token = create_user_token(user_id=1, email="admin@example.com", is_admin=True)
        payload = decode_token(token)
        assert isinstance(payload, dict)


# ---------------------------------------------------------------------------
# API Key Tests
# ---------------------------------------------------------------------------


class TestAPIKeyBOLA:
    @pytest.mark.anyio
    async def test_api_key_bola_protection(self, setup_db):
        from portal.auth import create_user_token
        from portal.database import create_api_key, create_event, create_user, get_session, set_event_membership

        async with get_session() as session:
            # Create two events
            event1 = await create_event(session, slug="event1", display_name="Event 1")
            event2 = await create_event(session, slug="event2", display_name="Event 2")

            # Create a user who is only admin of Event 1
            user1 = await create_user(session, email="user1@example.com", display_name="User 1")
            await set_event_membership(session, user_id=user1.id, event_id=event1.id, role="event_owner")

            # Create an API key in Event 2
            api_key2, _ = await create_api_key(session, event2.id, name="Event 2 Key")
            key_id = api_key2.id

        token = create_user_token(user_id=user1.id, email=user1.email)

        async with _client() as c:
            c.cookies.set("user_token", token)

            # Try to access Event 2's API Keys
            res_list = await c.get(f"/admin/api/events/{event2.id}/api-keys")
            assert res_list.status_code == 403

            res_post = await c.post(f"/admin/api/events/{event2.id}/api-keys", json={"name": "hacked"})
            assert res_post.status_code == 403

            res_delete = await c.delete(f"/admin/api/events/{event2.id}/api-keys/{key_id}")
            assert res_delete.status_code == 403

    @pytest.mark.anyio
    async def test_api_key_room_coordinator_blocked(self, setup_db):
        from portal.auth import create_user_token
        from portal.database import (
            create_api_key,
            create_event,
            create_room,
            create_user,
            get_session,
            set_room_membership,
        )

        async with get_session() as session:
            # Create an event and a room
            event = await create_event(session, slug="event-coordinator", display_name="Event Coordinator")
            room = await create_room(session, event_id=event.id, display_name="Room 1")

            # Create a user who is only a room coordinator for Room 1
            user = await create_user(session, email="coordinator@example.com", display_name="Coordinator")
            await set_room_membership(session, user_id=user.id, room_id=room.id, role="room_coordinator")

            # Create an API key in the event to test deletion
            api_key, _ = await create_api_key(session, event.id, name="Event Key")
            key_id = api_key.id
            event_id = event.id

        token = create_user_token(user_id=user.id, email=user.email)

        async with _client() as c:
            c.cookies.set("user_token", token)

            # Try to list keys
            res_list = await c.get(f"/admin/api/events/{event_id}/api-keys")
            assert res_list.status_code == 403

            # Try to create key
            res_post = await c.post(f"/admin/api/events/{event_id}/api-keys", json={"name": "hacked"})
            assert res_post.status_code == 403

            # Try to delete key
            res_delete = await c.delete(f"/admin/api/events/{event_id}/api-keys/{key_id}")
            assert res_delete.status_code == 403


class TestAPIKeyCRUD:
    @pytest.mark.anyio
    async def test_api_key_lifecycle(self, setup_db):
        from portal.auth import create_user_token
        from portal.database import create_event, create_user, get_session, set_event_membership

        async with get_session() as session:
            event = await create_event(session, slug="event-api", display_name="Event API")
            user = await create_user(session, email="apiadmin@example.com", display_name="API Admin")
            await set_event_membership(session, user_id=user.id, event_id=event.id, role="event_owner")
            event_id = event.id
            user_id = user.id
            user_email = user.email

        token = create_user_token(user_id=user_id, email=user_email)

        async with _client() as c:
            c.cookies.set("user_token", token)

            # Initially empty
            res = await c.get(f"/workspace/api/events/{event_id}/api-keys")
            assert res.status_code == 200
            assert res.json() == []

            # Create API key
            res = await c.post(f"/workspace/api/events/{event_id}/api-keys", json={"name": "Integration Key"})
            assert res.status_code == 200
            data = res.json()
            assert data["name"] == "Integration Key"
            assert "raw_key" in data
            assert data["raw_key"].startswith("vb_")

            key_id = data["id"]

            # Prevent duplicate name
            res_dup = await c.post(f"/workspace/api/events/{event_id}/api-keys", json={"name": "Integration Key"})
            assert res_dup.status_code == 400
            assert "already exists" in res_dup.json()["detail"]

            # Prevent blank name
            res_blank = await c.post(f"/workspace/api/events/{event_id}/api-keys", json={"name": "   "})
            assert res_blank.status_code == 400
            assert "cannot be blank" in res_blank.json()["detail"]

            # List keys (should contain 1)
            res = await c.get(f"/workspace/api/events/{event_id}/api-keys")
            assert res.status_code == 200
            keys = res.json()
            assert len(keys) == 1
            assert keys[0]["id"] == key_id
            assert keys[0]["name"] == "Integration Key"
            assert "raw_key" not in keys[0]

            # Revoke key
            res_del = await c.delete(f"/workspace/api/events/{event_id}/api-keys/{key_id}")
            assert res_del.status_code == 200
            assert res_del.json() == {"success": True}

            # Repeated revoke of the same key returns 404
            res_del_again = await c.delete(f"/workspace/api/events/{event_id}/api-keys/{key_id}")
            assert res_del_again.status_code == 404
            assert res_del_again.json()["detail"] == "API Key not found or already revoked"

            # List keys (should be empty again)
            res = await c.get(f"/workspace/api/events/{event_id}/api-keys")
            assert res.status_code == 200
            assert res.json() == []

            # Duplicate name is now allowed since the old one is revoked
            res_remake = await c.post(f"/workspace/api/events/{event_id}/api-keys", json={"name": "Integration Key"})
            assert res_remake.status_code == 200
            assert res_remake.json()["name"] == "Integration Key"

    @pytest.mark.anyio
    async def test_concurrent_api_key_revocation(self, setup_db):
        import asyncio

        from portal.auth import create_user_token
        from portal.database import create_event, create_user, get_session, set_event_membership

        async with get_session() as session:
            event = await create_event(session, slug="event-concur-api", display_name="Event Concur API")
            user = await create_user(session, email="concuradmin@example.com", display_name="Concur Admin")
            await set_event_membership(session, user_id=user.id, event_id=event.id, role="event_owner")
            event_id = event.id
            user_id = user.id
            user_email = user.email

        token = create_user_token(user_id=user_id, email=user_email)

        async with _client() as c:
            c.cookies.set("user_token", token)

            # Create an active key
            res = await c.post(f"/workspace/api/events/{event_id}/api-keys", json={"name": "Race Key"})
            assert res.status_code == 200
            key_id = res.json()["id"]

            # Overlapping concurrent revoke requests
            req1 = c.delete(f"/workspace/api/events/{event_id}/api-keys/{key_id}")
            req2 = c.delete(f"/workspace/api/events/{event_id}/api-keys/{key_id}")
            res1, res2 = await asyncio.gather(req1, req2)

            status_codes = sorted([res1.status_code, res2.status_code])
            assert status_codes == [200, 404]

            success_res = res1 if res1.status_code == 200 else res2
            error_res = res1 if res1.status_code == 404 else res2
            assert success_res.json() == {"success": True}
            assert error_res.json()["detail"] == "API Key not found or already revoked"

    @pytest.mark.anyio
    async def test_hmac_sha256_hashing(self, setup_db):
        import hashlib
        import hmac

        from portal.config import settings
        from portal.database import create_api_key, create_event, get_session, verify_api_key

        async with get_session() as session:
            event = await create_event(session, slug="hmac-event", display_name="HMAC Event")
            api_key, raw_key = await create_api_key(session, event.id, "HMAC Test Key")

            # Verify the key using the helper function
            verified_key = await verify_api_key(session, raw_key)
            assert verified_key is not None
            assert verified_key.id == api_key.id

            # Manually verify that it uses HMAC-SHA256 with the server secret
            expected_hash = hmac.new(
                settings.effective_jwt_secret.encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256
            ).hexdigest()

            assert verified_key.key_hash == expected_hash

            # Verify that plain SHA256 does NOT match the stored hash
            plain_sha256 = hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
            assert verified_key.key_hash != plain_sha256


class TestListenerTokenAPI:
    @pytest.mark.anyio
    async def test_provision_listener_token_success(self, setup_db):
        from httpx import AsyncClient
        from sqlalchemy import select

        from portal.database import UsageMetric, create_api_key, create_event, get_session

        async with get_session() as session:
            event = await create_event(session, slug="api-test-event", display_name="API Test Event")
            api_key, raw_key = await create_api_key(session, event.id, "Test Key")

        async with _client() as client:
            response = await client.post("/api/v1/tokens/listener", headers={"Authorization": f"Bearer {raw_key}"})
            assert response.status_code == 201
            data = response.json()
            assert "token" in data
            assert isinstance(data["token"], str)

        # Verify usage metric was logged
        async with get_session() as session:
            stmt = select(UsageMetric).where(
                UsageMetric.event_id == event.id, UsageMetric.metric_name == "listener_token_issued"
            )
            result = await session.execute(stmt)
            metric = result.scalar_one_or_none()
            assert metric is not None
            assert metric.value == 1

    @pytest.mark.anyio
    async def test_provision_listener_token_invalid_key(self, setup_db):
        from httpx import AsyncClient

        from portal.database import create_api_key, create_event, get_session

        async with get_session() as session:
            event = await create_event(session, slug="api-test-event-invalid", display_name="API Test Event Invalid")
            api_key, raw_key = await create_api_key(session, event.id, "Test Key")

            # Revoke the key
            api_key.active = False
            await session.commit()

        async with _client() as client:
            # Test revoked key
            response1 = await client.post("/api/v1/tokens/listener", headers={"Authorization": f"Bearer {raw_key}"})
            assert response1.status_code == 401
            assert response1.json() == {"detail": "Invalid API Key"}

            # Test fake key
            response2 = await client.post(
                "/api/v1/tokens/listener", headers={"Authorization": "Bearer vb_fakekeythatdoesntexist"}
            )
            assert response2.status_code == 401
            assert response2.json() == {"detail": "Invalid API Key"}

            # Response bodies must be identical for revoked and non-existent keys
            assert response1.json() == response2.json()

    @pytest.mark.anyio
    async def test_provision_listener_token_missing_header(self, setup_db):
        from httpx import AsyncClient

        async with _client() as client:
            response = await client.post("/api/v1/tokens/listener")
            assert response.status_code == 401
            assert response.json() == {"detail": "Missing or invalid Bearer token"}

            response = await client.post("/api/v1/tokens/listener", headers={"Authorization": "Basic something"})
            assert response.status_code == 401
            assert response.json() == {"detail": "Missing or invalid Bearer token"}

    @pytest.mark.anyio
    async def test_provision_listener_token_rate_limit(self, setup_db):
        from portal.rate_limit import _rates

        _rates.clear()
        from httpx import AsyncClient

        from portal.database import create_api_key, create_event, get_session

        async with get_session() as session:
            event = await create_event(session, slug="api-test-event-rate", display_name="API Test Event Rate")
            api_key, raw_key = await create_api_key(session, event.id, "Test Key Rate")

        async with _client() as client:
            # Hit the endpoint 60 times, should all be 201 (since max_requests=60)
            for _ in range(60):
                response = await client.post("/api/v1/tokens/listener", headers={"Authorization": f"Bearer {raw_key}"})
                assert response.status_code == 201

            # The 61st request should be rate limited
            response = await client.post("/api/v1/tokens/listener", headers={"Authorization": f"Bearer {raw_key}"})
            assert response.status_code == 429
            assert response.json() == {"detail": "Too many requests"}

    @pytest.mark.anyio
    async def test_listener_token_cross_event_ws_captions_rejected(self, setup_db):
        """A listener JWT scoped to event-a must not be accepted on event-b's captions WS"""
        import os

        from httpx import ASGITransport, AsyncClient
        from starlette.testclient import TestClient

        from portal.auth import create_listener_token
        from portal.config import settings

        os.environ["BOOTH_ACCESS_TOKEN"] = "test-booth-token"
        settings.booth_access_token = "test-booth-token"

        try:
            # Mint a listener token scoped to event-a
            token_event_a = create_listener_token(event_slug="event-a")

            from fastapi_app import app as fastapi_app

            tc = TestClient(app=fastapi_app)
            from starlette.websockets import WebSocketDisconnect

            booth_id_event_b = "event-b-english"
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with tc.websocket_connect(
                    f"/ws/captions/{booth_id_event_b}?token={token_event_a}",
                ) as ws:
                    ws.receive_text()
            assert exc_info.value.code == 4003
        finally:
            os.environ["BOOTH_ACCESS_TOKEN"] = ""
            settings.booth_access_token = ""

    @pytest.mark.anyio
    async def test_listener_token_correct_event_ws_captions_accepted(self, setup_db):
        """A listener JWT scoped to event-a is accepted on event-a's captions WS."""
        import os

        from starlette.testclient import TestClient

        from portal.auth import create_listener_token
        from portal.config import settings

        os.environ["BOOTH_ACCESS_TOKEN"] = "test-booth-token"
        settings.booth_access_token = "test-booth-token"

        try:
            token_event_a = create_listener_token(event_slug="event-a")

            from fastapi_app import app as fastapi_app

            tc = TestClient(app=fastapi_app)
            booth_id_event_a = "event-a-english"
            with tc.websocket_connect(
                f"/ws/captions/{booth_id_event_a}?token={token_event_a}",
            ) as ws:
                # Connection accepted — no exception raised, just disconnect cleanly
                ws.close()
        finally:
            os.environ["BOOTH_ACCESS_TOKEN"] = ""
            settings.booth_access_token = ""

    @pytest.mark.anyio
    async def test_listener_token_rejected_from_booth_management_ws(self, setup_db):
        """Listener tokens must be rejected from /ws/booth/{booth_id} entirely."""
        import os

        from starlette.testclient import TestClient

        from portal.auth import create_listener_token
        from portal.config import settings

        os.environ["BOOTH_ACCESS_TOKEN"] = "test-booth-token"
        settings.booth_access_token = "test-booth-token"

        try:
            token = create_listener_token(event_slug="event-a")

            from fastapi_app import app as fastapi_app

            tc = TestClient(app=fastapi_app)
            from starlette.websockets import WebSocketDisconnect

            with pytest.raises(WebSocketDisconnect) as exc_info:
                with tc.websocket_connect(
                    f"/ws/booth/event-a-english?token={token}",
                ) as ws:
                    ws.receive_text()
            assert exc_info.value.code == 4003
        finally:
            os.environ["BOOTH_ACCESS_TOKEN"] = ""
            settings.booth_access_token = ""


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/admin/",
        "/admin/events/",
        "/admin/users/",
        "/admin/events/{event}/rooms/",
        "/admin/events/{event}/rooms/{room}/booths/",
    ],
)
async def test_admin_list_pages_have_no_inline_styles(path, admin_cookie, seed_event):
    import re

    event, room, _ = seed_event

    async with _client() as c:
        resp = await c.get(path.format(event=event.id, room=room.id), cookies=admin_cookie)

    assert resp.status_code == 200
    assert not re.search(rb"\sstyle\s*=", resp.content, re.IGNORECASE)


async def _seed_users_with_join_dates():
    from portal.database import create_user, get_session

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    async with get_session() as s:
        for i, email in enumerate(["oldest@example.com", "middle@example.com", "newest@example.com"]):
            user = await create_user(s, email=email, display_name=email.split("@")[0])
            user.created_at = base + timedelta(days=i)
        await s.flush()


@pytest.mark.anyio
async def test_user_list_defaults_to_newest_first(admin_cookie):
    import re

    await _seed_users_with_join_dates()

    async with _client() as c:
        resp = await c.get("/admin/users/", cookies=admin_cookie)

    assert resp.status_code == 200
    body = resp.text
    assert body.index("newest@example.com") < body.index("middle@example.com") < body.index("oldest@example.com")
    # Joined header shows the descending indicator and toggles to ascending on click.
    assert re.search(r'<a href="\?sort_by=created_at&sort_order=asc[^"]*" class="sort-link">', body)
    assert re.search(r'Joined\s*<span class="sort-indicator">▼</span>', body)


@pytest.mark.anyio
async def test_user_list_explicit_ascending_sort_still_works(admin_cookie):
    import re

    await _seed_users_with_join_dates()

    async with _client() as c:
        resp = await c.get("/admin/users/?sort_by=created_at&sort_order=asc", cookies=admin_cookie)

    assert resp.status_code == 200
    body = resp.text
    assert body.index("oldest@example.com") < body.index("middle@example.com") < body.index("newest@example.com")
    # Header flips to the ascending indicator and the next click goes back to descending.
    assert re.search(r'Joined\s*<span class="sort-indicator">▲</span>', body)
    assert re.search(r'<a href="\?sort_by=created_at&sort_order=desc[^"]*" class="sort-link">', body)


@pytest.mark.anyio
async def test_user_list_sort_links_encode_the_search_term(admin_cookie):
    from portal.database import create_user, get_session

    async with get_session() as s:
        await create_user(s, email="rnd@example.com", display_name="Team %26 a&b# R&D")

    async with _client() as c:
        resp = await c.get("/admin/users/", params={"search": "%26 a&b#"}, cookies=admin_cookie)

    assert resp.status_code == 200
    assert 'href="?sort_by=email&sort_order=asc&search=%2526%20a%26b%23&limit=' in resp.text
    assert "&search=%26 a&amp;b#" not in resp.text


@pytest.mark.anyio
async def test_user_list_only_marks_the_active_sort_column(admin_cookie):
    import re

    await _seed_users_with_join_dates()

    async with _client() as c:
        resp = await c.get("/admin/users/", cookies=admin_cookie)

    assert resp.status_code == 200
    active = re.findall(r'<span class="sort-indicator">[▲▼]</span>', resp.text)
    assert active == ['<span class="sort-indicator">▼</span>']


@pytest.mark.anyio
async def test_user_list_orders_same_join_time_by_newest_id(admin_cookie):
    from portal.database import create_user, get_session

    same_moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
    emails = ["tie-a@example.com", "tie-b@example.com", "tie-c@example.com"]
    async with get_session() as s:
        for email in emails:
            user = await create_user(s, email=email, display_name=email.split("@")[0])
            user.created_at = same_moment
        await s.flush()

    async with _client() as c:
        resp = await c.get("/admin/users/", cookies=admin_cookie)

    assert resp.status_code == 200
    body = resp.text
    # Created in a-b-c order, so ids ascend a-b-c; newest-first means c-b-a.
    assert body.index("tie-c@example.com") < body.index("tie-b@example.com") < body.index("tie-a@example.com")


@pytest.mark.anyio
@pytest.mark.parametrize("sort_field", ["__repr__", "unknown_field"])
async def test_user_list_unsupported_sort_by_falls_back_to_default_ordering(admin_cookie, sort_field):
    await _seed_users_with_join_dates()

    async with _client() as c:
        resp = await c.get(f"/admin/users/?sort_by={sort_field}", cookies=admin_cookie)

    assert resp.status_code == 200
    body = resp.text
    assert body.index("newest@example.com") < body.index("middle@example.com") < body.index("oldest@example.com")


@pytest.mark.anyio
async def test_user_list_valid_sort_by_orders_correctly(admin_cookie):
    await _seed_users_with_join_dates()

    async with _client() as c:
        resp = await c.get("/admin/users/?sort_by=email&sort_order=asc", cookies=admin_cookie)

    assert resp.status_code == 200
    body = resp.text
    assert body.index("middle@example.com") < body.index("newest@example.com") < body.index("oldest@example.com")


@pytest.mark.anyio
async def test_user_list_badge_shows_total_across_pages(admin_cookie):
    from portal.database import create_user, get_session

    async with get_session() as s:
        for i in range(3):
            await create_user(s, email=f"user{i}@example.com", display_name=f"User {i}")

    async with _client() as c:
        resp = await c.get("/admin/users/?limit=2", cookies=admin_cookie)

    assert resp.status_code == 200
    # 3 seeded users plus the fixture's own administrator row.
    assert '<span class="ul-count">4 users</span>' in resp.text
    assert "displayed" not in resp.text


@pytest.mark.anyio
async def test_user_list_badge_uses_singular_for_one_user(admin_cookie):
    from portal.database import create_user, get_session

    async with get_session() as s:
        await create_user(s, email="solo@example.com", display_name="Solo")

    async with _client() as c:
        resp = await c.get("/admin/users/", cookies=admin_cookie)

    assert resp.status_code == 200
    # The fixture's administrator row plus the one seeded user: plural "users".
    assert '<span class="ul-count">2 users</span>' in resp.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/admin/setup",
        "/admin/events/{event}/setup/rooms",
        "/admin/events/{event}/setup/booths",
        "/admin/events/{event}/setup/invite",
    ],
)
async def test_setup_wizard_pages_have_no_inline_styles(path, admin_cookie, seed_event):
    import re

    event, _, _ = seed_event

    async with _client() as c:
        resp = await c.get(path.format(event=event.id), cookies=admin_cookie)

    assert resp.status_code == 200
    assert not re.search(rb"\sstyle\s*=", resp.content, re.IGNORECASE)


@pytest.mark.anyio
async def test_event_detail_listener_link_has_copy_button(admin_cookie, seed_event):
    from portal.database import get_session

    event, _, _ = seed_event
    async with get_session() as s:
        db_event = await s.get(type(event), event.id)
        db_event.listener_join_code = "ROOM42"

    async with _client() as c:
        resp = await c.get(f"/admin/events/{event.id}/", cookies=admin_cookie)

    assert resp.status_code == 200
    assert 'id="listener-link"' in resp.text
    assert 'data-copy-target="listener-link"' in resp.text


@pytest.mark.anyio
async def test_admin_pages_have_a_toast_live_region(admin_cookie, seed_event):
    """The copy-to-clipboard success/failure feedback in admin.js needs the
    aria-live toast container from admin/base.html on every admin page."""
    event, _, _ = seed_event

    async with _client() as c:
        resp = await c.get(f"/admin/events/{event.id}/", cookies=admin_cookie)

    assert resp.status_code == 200
    assert 'id="toast-container"' in resp.text
    assert 'aria-live="polite"' in resp.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/admin/events/{event}/",
        "/admin/events/{event}/team/",
        "/admin/events/{event}/api-settings/",
        "/admin/events/{event}/rooms/{room}/booths/{booth}/",
        "/admin/events/{event}/rooms/{room}/",
        "/admin/events/{event}/rooms/{room}/transcripts/",
        "/admin/users/{user}/",
    ],
)
async def test_admin_detail_pages_have_no_inline_styles(path, admin_cookie, seed_event):
    import re

    from portal.auth import hash_password
    from portal.database import create_user, get_session

    event, room, booth = seed_event
    async with get_session() as s:
        user = await create_user(
            s,
            email="detail@test.com",
            display_name="Detail User",
            password_hash=hash_password("securepass123"),
            email_verified=True,
        )

    async with _client() as c:
        resp = await c.get(
            path.format(event=event.id, room=room.id, booth=booth.id, user=user.id),
            cookies=admin_cookie,
        )

    assert resp.status_code == 200
    # The API key modals keep style="display: none", which admin.js toggles.
    body = resp.content.replace(b'style="display: none;"', b"")
    assert not re.search(rb"\sstyle\s*=", body, re.IGNORECASE)


@pytest.mark.anyio
@pytest.mark.parametrize("seed_accounts", [False, True], ids=["empty", "all-statuses"])
async def test_developer_accounts_page_has_no_inline_styles(seed_accounts):
    import re

    from portal.auth import create_user_token, hash_password
    from portal.database import create_user, get_session
    from portal.models import DeveloperAccount

    async with get_session() as s:
        admin = await create_user(
            s, email="super@test.com", display_name="Super", password_hash=hash_password("securepass123")
        )
        admin.is_admin = True
        if seed_accounts:
            for status in ("pending", "approved", "suspended", "rejected"):
                applicant = await create_user(
                    s,
                    email=f"{status}@test.com",
                    display_name=status,
                    password_hash=hash_password("securepass123"),
                )
                s.add(DeveloperAccount(user_id=applicant.id, status=status, organization_name=f"{status} org"))
        await s.commit()
        admin_id, admin_email = admin.id, admin.email

    cookie = {"user_token": create_user_token(user_id=admin_id, email=admin_email, is_admin=True)}
    async with _client() as c:
        resp = await c.get("/admin/developer-accounts", cookies=cookie)

    assert resp.status_code == 200
    if seed_accounts:
        # Every status branch rendered, so the assertion below covers all of them.
        for label in (b"Approve", b"Reject", b"Suspend", b"Restore"):
            assert label in resp.content
    else:
        assert b"No developer applications" in resp.content
    assert not re.search(rb"\sstyle\s*=", resp.content, re.IGNORECASE)
