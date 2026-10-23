"""Comprehensive tests for Event Hosting Access, organizer invitations, and workspace gating."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import secrets
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from fastapi_app import app
from portal import database
from portal.auth import create_user_token, get_access_flags, hash_password, verify_password
from portal.database import (
    configure,
    create_auth_token,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    get_user_by_id,
    init_db,
    set_event_membership,
    set_room_membership,
)
from portal.models import AuthToken, User, utc_now
from portal.workspace_routing import management_template_context

os.environ["BOOTH_ACCESS_TOKEN"] = ""


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def create_hosting_invite(session, user_id: int, expires_in_seconds: int = 3600) -> tuple[str, AuthToken]:
    """Helper to create a hosting invite token with SHA-256 digest in DB and return (raw_token, auth_token)."""
    raw_token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    tok = await create_auth_token(
        session,
        user_id=user_id,
        token_type="hosting_invite",
        jti=digest,
        expires_in_seconds=expires_in_seconds,
    )
    return raw_token, tok


def create_mock_request(path: str = "/admin/", cookies: dict | None = None, headers: dict | None = None) -> Request:
    raw_headers = []
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        raw_headers.append((b"cookie", cookie_str.encode("latin-1")))
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode("latin-1"), v.encode("latin-1")))
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "headers": raw_headers,
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
        "scheme": "http",
        "state": {},
    }
    return Request(scope)


@pytest.mark.anyio
async def test_sqlite_busy_timeout_before_wal():
    """Verify that PRAGMA busy_timeout=5000 is executed before PRAGMA journal_mode=WAL."""
    source = inspect.getsource(database)
    idx_busy = source.find("PRAGMA busy_timeout=5000")
    idx_wal = source.find("PRAGMA journal_mode=WAL")
    assert idx_busy != -1
    assert idx_wal != -1
    assert idx_busy < idx_wal


@pytest.mark.anyio
async def test_get_admin_flags_room_owner_and_coordinator():
    """Review 7 A1: Owner and coordinator of the same room on a route with only room_id."""
    async with get_session() as session:
        owner = await create_user(
            session,
            email="owner_coord@example.com",
            display_name="Owner Coord",
            password_hash=hash_password("pw123"),
            email_verified=True,
        )
        event = await create_event(session, slug="shared-ev", display_name="Shared")
        room = await create_room(session, event_id=event.id, display_name="Main Room")
        await set_event_membership(session, user_id=owner.id, event_id=event.id, role="event_owner")
        await set_room_membership(session, user_id=owner.id, room_id=room.id, role="room_coordinator")

    token = create_user_token(user_id=owner.id, email=owner.email)
    req = create_mock_request("/admin/rooms/1/", cookies={"user_token": token})

    # Route with only room_id passed:
    flags = await get_access_flags(req, event_id=None, room_id=room.id)
    assert flags["is_room_coordinator"] is True
    assert flags["is_event_owner"] is True
    assert flags["is_admin"] is False


@pytest.mark.anyio
async def test_template_context_does_not_overwrite_event_scoped_owner_flag():
    """Review 7 A2: Context processor does not inject is_event_owner and overwrite route flags."""
    async with get_session() as session:
        user = await create_user(
            session,
            email="host_user@example.com",
            display_name="Host User",
            password_hash=hash_password("pw123"),
            can_host=True,
        )
    token = create_user_token(user_id=user.id, email=user.email)
    req = create_mock_request("/workspace/events/1/", cookies={"user_token": token})

    ctx = management_template_context(req)
    assert "is_event_owner" not in ctx
    assert ctx["can_host"] is False
    assert ctx["is_workspace"] is False

    req.state.principal = {"can_host": True, "is_admin": False}
    req.state.is_workspace = True
    ctx_resolved = management_template_context(req)
    assert ctx_resolved["can_host"] is True
    assert ctx_resolved["is_workspace"] is True


@pytest.mark.anyio
async def test_invite_acceptance_flow_and_primitive_copying():
    """Review 7 A3: Acceptance flow copies primitives safely, sets single cookie, marks token used."""
    async with get_session() as session:
        user = await create_user(
            session,
            email="invited@example.com",
            display_name="Invited Stub",
            password_hash=None,
            email_verified=False,
            can_host=False,
        )
        raw_token, tok = await create_hosting_invite(session, user.id, 3600)

    async with client() as http:
        # GET form
        resp_get = await http.get(f"/invite/accept/{raw_token}")
        assert resp_get.status_code == 200
        assert "Create Account & Go to Workspace" in resp_get.text

        # POST accept
        resp_post = await http.post(
            f"/invite/accept/{raw_token}",
            data={
                "display_name": "Invited Organizer",
                "password": "ValidPassword123!",
                "password_confirm": "ValidPassword123!",
            },
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp_post.status_code == 303
        assert resp_post.headers["location"] == "/workspace/"
        assert "user_token" in resp_post.cookies

        # Verify DB user state
        async with get_session() as session:
            updated_user = await get_user_by_id(session, user.id)
            assert updated_user.can_host is True
            assert updated_user.email_verified is True
            assert updated_user.password_hash is not None
            assert updated_user.display_name == "Invited Organizer"

            # Token marked used and stored only as digest
            used_tok = await session.get(AuthToken, tok.id)
            assert used_tok.is_used is True
            assert used_tok.jti != raw_token
            assert used_tok.jti == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

        # Re-using the token fails (GET shows invalid/expired message, POST returns 400)
        resp_reuse = await http.get(f"/invite/accept/{raw_token}")
        assert resp_reuse.status_code == 200
        assert "invalid or has expired" in resp_reuse.text.lower()

        resp_reuse_post = await http.post(
            f"/invite/accept/{raw_token}",
            data={"password": "AnotherPassword123!", "password_confirm": "AnotherPassword123!"},
            headers={"Origin": "http://test"},
        )
        assert resp_reuse_post.status_code == 400


@pytest.mark.anyio
async def test_revoked_user_accepts_invite_restores_access():
    """When a user with revoked hosting accepts an invitation, revoked_at and requested_at are cleared and event access is restored."""
    async with get_session() as session:
        user = await create_user(
            session,
            email="was_revoked@example.com",
            display_name="Previously Revoked",
            password_hash=hash_password("ValidPassword123!"),
            email_verified=True,
            can_host=False,
            hosting_revoked_at=utc_now(),
            hosting_requested_at=utc_now(),
        )
        ev = await create_event(session, slug="rev-restored-ev", display_name="Restored Event")
        await set_event_membership(session, user_id=user.id, event_id=ev.id, role="event_owner")
        raw_token, tok = await create_hosting_invite(session, user.id, 3600)
        user_id = user.id
        event_id = ev.id

    async with client() as http:
        # POST accept invitation with existing password
        resp = await http.post(
            f"/invite/accept/{raw_token}",
            data={"password": "ValidPassword123!"},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/workspace/"
        cookie = resp.cookies.get("user_token")
        assert cookie is not None

        # Verify DB state: can_host is True, hosting_revoked_at is None, hosting_requested_at is None
        async with get_session() as session:
            updated = await get_user_by_id(session, user_id)
            assert updated.can_host is True
            assert updated.hosting_revoked_at is None
            assert updated.hosting_requested_at is None

        # User can now access their event without being redirected with hosting_revoked
        ev_resp = await http.get(f"/workspace/events/{event_id}/", cookies={"user_token": cookie}, follow_redirects=False)
        assert ev_resp.status_code == 200


@pytest.mark.anyio
async def test_inactive_user_cannot_be_invited_or_redeem():
    """Inactive users cannot be invited or redeem an invite."""
    async with get_session() as session:
        admin = await create_user(session, email="admin@example.com", display_name="Admin", is_admin=True)
        inactive = await create_user(
            session,
            email="inactive@example.com",
            display_name="Inactive",
            password_hash=hash_password("pw123"),
            is_active=False,
        )
        raw_token, tok = await create_hosting_invite(session, inactive.id, 3600)

    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)

    async with client() as http:
        # Attempt to invite inactive user -> 400
        res_invite = await http.post(
            "/admin/users/invite",
            data={"email": "inactive@example.com"},
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert res_invite.status_code == 400
        assert "inactive" in res_invite.text.lower() or "reactivate" in res_invite.text.lower()

        # Inactive user attempts redemption -> 403
        res_redeem = await http.post(
            f"/invite/accept/{raw_token}",
            data={"password": "ValidPassword123!", "password_confirm": "ValidPassword123!"},
            headers={"Origin": "http://test"},
        )
        assert res_redeem.status_code == 403
        assert "deactivated" in res_redeem.text.lower()

        # Granting hosting to inactive user is blocked -> 400
        res_grant = await http.post(
            f"/admin/users/{inactive.id}/hosting",
            data={"action": "grant"},
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert res_grant.status_code == 400
        assert "reactivate this account first" in res_grant.text.lower()

        # Revoking hosting from inactive user who has can_host is permitted -> 303
        async with get_session() as session:
            await session.execute(
                User.__table__.update().where(User.id == inactive.id).values(can_host=True)
            )
        res_revoke = await http.post(
            f"/admin/users/{inactive.id}/hosting",
            data={"action": "revoke"},
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert res_revoke.status_code == 303
        async with get_session() as session:
            rechecked = await get_user_by_id(session, inactive.id)
            assert rechecked.can_host is False
            assert rechecked.hosting_revoked_at is not None


@pytest.mark.anyio
async def test_toggle_hosting_on_stub_user_rejected():
    """Directly granting hosting to stub user via toggle endpoint is rejected."""
    async with get_session() as session:
        admin = await create_user(session, email="admin@example.com", display_name="Admin", is_admin=True)
        stub = await create_user(session, email="stub@example.com", display_name="Stub", password_hash=None)
        stub_id = stub.id

    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)
    async with client() as http:
        resp = await http.post(
            f"/admin/users/{stub_id}/hosting",
            data={"action": "grant"},
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert resp.status_code == 400
        assert "finished setting up their account" in resp.text.lower()


@pytest.mark.anyio
async def test_toggle_hosting_deletes_pending_tokens():
    """Toggling hosting on registered user deletes any pending invite tokens."""
    async with get_session() as session:
        admin = await create_user(session, email="admin@example.com", display_name="Admin", is_admin=True)
        user = await create_user(
            session,
            email="reg@example.com",
            display_name="Registered",
            password_hash=hash_password("pw123"),
            can_host=False,
        )
        await create_auth_token(session, user_id=user.id, token_type="hosting_invite", expires_in_seconds=3600)
        user_id = user.id

    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)
    async with client() as http:
        resp = await http.post(
            f"/admin/users/{user_id}/hosting",
            data={"action": "grant"},
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert resp.status_code == 303

    async with get_session() as session:
        u = await get_user_by_id(session, user_id)
        assert u.can_host is True
        tokens = (
            await session.execute(
                AuthToken.__table__.select().where(
                    AuthToken.user_id == user_id,
                    AuthToken.token_type == "hosting_invite",
                )
            )
        ).fetchall()
        assert len(tokens) == 0


@pytest.mark.anyio
async def test_invite_cancel_and_resend():
    """Cancel and resend invite endpoints work and require same-origin."""
    async with get_session() as session:
        admin = await create_user(session, email="admin@example.com", display_name="Admin", is_admin=True)
        user = await create_user(session, email="pending@example.com", display_name="Pending", password_hash=None)
        tok = await create_auth_token(session, user_id=user.id, token_type="hosting_invite", expires_in_seconds=10)
        token_id = tok.id

    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)

    async with client() as http:
        # Cross origin rejected
        resp_bad_origin = await http.post(
            f"/admin/users/invite/{token_id}/resend",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://evil.com"},
        )
        assert resp_bad_origin.status_code == 400

        # Resend works with same-origin
        resp_resend = await http.post(
            f"/admin/users/invite/{token_id}/resend",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert resp_resend.status_code == 303

        # Cancel works
        resp_cancel = await http.post(
            f"/admin/users/invite/{token_id}/cancel",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert resp_cancel.status_code == 303

    async with get_session() as session:
        tok_in_db = await session.get(AuthToken, token_id)
        assert tok_in_db is None


@pytest.mark.anyio
async def test_invite_redirect_context_and_fallback():
    """Verify resend and cancel redirects preserve user detail and hosting access referrers or fall back."""
    from sqlalchemy import select

    async with get_session() as session:
        admin = await create_user(session, email="admin_redir@example.com", display_name="Admin Redir", is_admin=True)
        user = await create_user(
            session, email="pending_redir@example.com", display_name="Pending Redir", password_hash=None
        )
        tok = await create_auth_token(session, user_id=user.id, token_type="hosting_invite", expires_in_seconds=3600)
        token_id = tok.id
        user_id = user.id

    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)

    async with client() as http:
        # 1. Resend from user detail page -> returns to user detail with success=invite_resent
        resp_resend_user = await http.post(
            f"/admin/users/invite/{token_id}/resend",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test", "Referer": f"http://test/admin/users/{user_id}/"},
        )
        assert resp_resend_user.status_code == 303
        assert resp_resend_user.headers["location"] == f"/admin/users/{user_id}/?success=invite_resent"

    # Get the regenerated token for further tests
    async with get_session() as session:
        tok_stmt = select(AuthToken).where(AuthToken.user_id == user_id, AuthToken.token_type == "hosting_invite")
        new_tok = (await session.execute(tok_stmt)).scalar_one()
        new_token_id = new_tok.id

    async with client() as http:
        # 2. Resend fallback: missing Referer -> falls back to /admin/users/?success=invite_resent
        resp_resend_fallback = await http.post(
            f"/admin/users/invite/{new_token_id}/resend",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert resp_resend_fallback.status_code == 303
        assert resp_resend_fallback.headers["location"] == "/admin/users/?success=invite_resent"

    # Get another regenerated token
    async with get_session() as session:
        tok_stmt = select(AuthToken).where(AuthToken.user_id == user_id, AuthToken.token_type == "hosting_invite")
        new_tok2 = (await session.execute(tok_stmt)).scalar_one()
        new_token_id2 = new_tok2.id

    async with client() as http:
        # 3. User-ID prefix collision: Referer for /admin/users/{user_id}0/ -> falls back to /admin/users/?success=invite_resent
        resp_resend_collision = await http.post(
            f"/admin/users/invite/{new_token_id2}/resend",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test", "Referer": f"http://test/admin/users/{user_id}0/"},
        )
        assert resp_resend_collision.status_code == 303
        assert resp_resend_collision.headers["location"] == "/admin/users/?success=invite_resent"

    # Get another regenerated token for cancel tests
    async with get_session() as session:
        tok_stmt = select(AuthToken).where(AuthToken.user_id == user_id, AuthToken.token_type == "hosting_invite")
        cancel_tok = (await session.execute(tok_stmt)).scalar_one()
        cancel_token_id = cancel_tok.id

    async with client() as http:
        # 4. Cancel from user detail page -> returns to user detail with success=invite_cancelled
        resp_cancel_user = await http.post(
            f"/admin/users/invite/{cancel_token_id}/cancel",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test", "Referer": f"http://test/admin/users/{user_id}/"},
        )
        assert resp_cancel_user.status_code == 303
        assert resp_cancel_user.headers["location"] == f"/admin/users/{user_id}/?success=invite_cancelled"

    # Create fresh tokens for hosting access cancel and fallback cancel
    async with get_session() as session:
        tok_ha = await create_auth_token(session, user_id=user_id, token_type="hosting_invite", expires_in_seconds=3600)
        tok_ha_id = tok_ha.id

    async with client() as http:
        # 5. Cancel from Hosting Access page -> returns to /admin/hosting-access/?success=invite_cancelled
        resp_cancel_ha = await http.post(
            f"/admin/users/invite/{tok_ha_id}/cancel",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test", "Referer": "http://test/admin/hosting-access/"},
        )
        assert resp_cancel_ha.status_code == 303
        assert resp_cancel_ha.headers["location"] == "/admin/hosting-access/?success=invite_cancelled"

    async with get_session() as session:
        tok_fb = await create_auth_token(session, user_id=user_id, token_type="hosting_invite", expires_in_seconds=3600)
        tok_fb_id = tok_fb.id

    async with client() as http:
        # 6. Cancel fallback: missing or unrecognized Referer -> falls back to /admin/users/?success=invite_cancelled
        resp_cancel_fb = await http.post(
            f"/admin/users/invite/{tok_fb_id}/cancel",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test"},
        )
        assert resp_cancel_fb.status_code == 303
        assert resp_cancel_fb.headers["location"] == "/admin/users/?success=invite_cancelled"


@pytest.mark.anyio
async def test_invite_redirect_malformed_referer():
    """Verify resend and cancel safely fall back to default when given a malformed Referer URL."""
    from sqlalchemy import select

    async with get_session() as session:
        admin = await create_user(
            session, email="admin_malformed@example.com", display_name="Admin Malformed", is_admin=True
        )
        user = await create_user(
            session, email="pending_malformed@example.com", display_name="Pending Malformed", password_hash=None
        )
        tok = await create_auth_token(session, user_id=user.id, token_type="hosting_invite", expires_in_seconds=3600)
        token_id = tok.id
        user_id = user.id

    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)

    async with client() as http:
        # 1. Resend with malformed Referer -> falls back to /admin/users/?success=invite_resent
        resp_resend = await http.post(
            f"/admin/users/invite/{token_id}/resend",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test", "Referer": "http://["},
        )
        assert resp_resend.status_code == 303
        assert resp_resend.headers["location"] == "/admin/users/?success=invite_resent"

    # Get the regenerated token for cancel test
    async with get_session() as session:
        tok_stmt = select(AuthToken).where(AuthToken.user_id == user_id, AuthToken.token_type == "hosting_invite")
        new_tok = (await session.execute(tok_stmt)).scalar_one()
        new_token_id = new_tok.id

    async with client() as http:
        # 2. Cancel with malformed Referer -> falls back to /admin/users/?success=invite_cancelled
        resp_cancel = await http.post(
            f"/admin/users/invite/{new_token_id}/cancel",
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test", "Referer": "http://["},
        )
        assert resp_cancel.status_code == 303
        assert resp_cancel.headers["location"] == "/admin/users/?success=invite_cancelled"


@pytest.mark.anyio
async def test_demo_request_hosting_rate_limit_and_user_flag():
    """Demo request enforces rate limits, strips CRLF, ignores spoofed user_id, leaves hosting_requested_at alone."""
    async with get_session() as session:
        user = await create_user(
            session,
            email="requester@example.com",
            display_name="Requester",
            password_hash=hash_password("pw123"),
            email_verified=True,
        )
        user_id = user.id

    token = create_user_token(user_id=user_id, email="requester@example.com")

    async with client() as http:
        with patch("portal.routers.public.send_demo_request_email", new_callable=AsyncMock) as mock_send:
            # 1. Normal submission
            res1 = await http.post(
                "/api/request-demo",
                json={
                    "firstName": "Jane\r\n",
                    "lastName": "Doe",
                    "email": "requester@example.com",
                    "companyName": "Acme\nCorp",
                    "notes": "Conference\r\nTesting",
                    "user_id": 99999,  # spoofed, must be ignored
                    "source": "spoofed_source",
                },
                cookies={"user_token": token},
            )
            assert res1.status_code == 200
            assert res1.json()["ok"] is True
            # Verify CRLF stripped in email call
            called_info = mock_send.call_args[0][0]
            assert "\r" not in called_info["firstName"] and "\n" not in called_info["firstName"]
            assert "\r" not in called_info["companyName"] and "\n" not in called_info["companyName"]
            assert called_info["user_id"] == user_id
            assert called_info["source"] == "account"

            # Hosting access is requested from the Account page form, not the demo form
            async with get_session() as session:
                u1 = await get_user_by_id(session, user_id)
                assert u1.hosting_requested_at is None

            # 2. Second submission should succeed
            res2 = await http.post(
                "/api/request-demo",
                json={
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "email": "requester@example.com",
                    "companyName": "Acme Corp",
                    "notes": "More Testing",
                },
                cookies={"user_token": token},
            )
            assert res2.status_code == 200

            async with get_session() as session:
                u2 = await get_user_by_id(session, user_id)
                assert u2.hosting_requested_at is None

            # 3. Third submission
            res3 = await http.post(
                "/api/request-demo",
                json={
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "email": "requester@example.com",
                    "companyName": "Acme Corp",
                    "notes": "Third",
                },
                cookies={"user_token": token},
            )
            assert res3.status_code == 200

            # 4. Fourth submission from same email exceeds 3/hr limit -> 429
            res4 = await http.post(
                "/api/request-demo",
                json={
                    "firstName": "Jane",
                    "lastName": "Doe",
                    "email": "requester@example.com",
                    "companyName": "Acme Corp",
                    "notes": "Fourth",
                },
                cookies={"user_token": token},
            )
            assert res4.status_code == 429
            assert "too many" in res4.json()["detail"].lower()


@pytest.mark.anyio
async def test_rate_limit_ordering_on_event_creation():
    """Review 6 A2: Slug pre-check must run before rate limiter so slug collision does not consume quota."""
    async with get_session() as session:
        host = await create_user(
            session,
            email="host@example.com",
            display_name="Host",
            password_hash=hash_password("pw123"),
            can_host=True,
        )
        await create_event(session, slug="existing-slug", display_name="Existing Event")
        host_id = host.id

    token = create_user_token(user_id=host_id, email="host@example.com")

    async with client() as http:
        # Submit duplicate slug multiple times
        for _ in range(12):
            resp = await http.post(
                "/workspace/setup",
                data={"display_name": "My New Event", "slug": "existing-slug"},
                cookies={"user_token": token},
            )
            assert resp.status_code == 400
            assert "already exists" in resp.text

        # Now submit a unique valid slug - must NOT be rate limited!
        resp_valid = await http.post(
            "/workspace/setup",
            data={"display_name": "Unique Event", "slug": "unique-event"},
            cookies={"user_token": token},
            follow_redirects=False,
        )
        assert resp_valid.status_code == 303
        assert resp_valid.headers["location"].startswith("/workspace/events/")


@pytest.mark.anyio
async def test_non_host_redirected_to_account_with_error():
    """User without hosting access is redirected from setup with error parameter."""
    async with get_session() as session:
        user = await create_user(
            session,
            email="plain@example.com",
            display_name="Plain User",
            password_hash=hash_password("pw123"),
            can_host=False,
        )
        user_id = user.id

    token = create_user_token(user_id=user_id, email="plain@example.com")
    async with client() as http:
        # GET /workspace/setup
        resp_get = await http.get("/workspace/setup", cookies={"user_token": token}, follow_redirects=False)
        assert resp_get.status_code == 303
        assert resp_get.headers["location"] == "/account?error=hosting_access_required"

        # POST /workspace/setup
        resp_post = await http.post(
            "/workspace/setup",
            data={"display_name": "Forbidden Event"},
            cookies={"user_token": token},
            follow_redirects=False,
        )
        assert resp_post.status_code == 303
        assert resp_post.headers["location"] == "/account?error=hosting_access_required"


@pytest.mark.anyio
async def test_zero_event_host_blocked_from_model_downloads():
    """Host who does not own any events cannot trigger model downloads."""
    async with get_session() as session:
        host = await create_user(
            session,
            email="zero_host@example.com",
            display_name="Zero Host",
            password_hash=hash_password("pw123"),
            can_host=True,
        )
        host_id = host.id

    token = create_user_token(user_id=host_id, email="zero_host@example.com")
    async with client() as http:
        resp = await http.post("/workspace/models/trigger_download", cookies={"user_token": token})
        assert resp.status_code == 403


@pytest.mark.anyio
async def test_new_event_button_visibility():
    """Base, dashboard, and event list templates hide '+ New event' for non-hosts."""
    async with get_session() as session:
        non_host = await create_user(
            session,
            email="nonhost@example.com",
            display_name="Non Host",
            password_hash=hash_password("pw123"),
            can_host=False,
        )
        host = await create_user(
            session,
            email="host_btn@example.com",
            display_name="Host Btn",
            password_hash=hash_password("pw123"),
            can_host=True,
        )
        non_host_id = non_host.id
        host_id = host.id

    token_non_host = create_user_token(user_id=non_host_id, email="nonhost@example.com")
    token_host = create_user_token(user_id=host_id, email="host_btn@example.com")

    async with client() as http:
        # Non-host on account page sees Request access; My events has no New event button
        res_nh_account = await http.get("/account", cookies={"user_token": token_non_host})
        assert res_nh_account.status_code == 200
        assert 'href="/account/hosting-request"' in res_nh_account.text
        res_nh_events = await http.get("/workspace/", cookies={"user_token": token_non_host})
        assert res_nh_events.status_code == 200
        assert 'href="/workspace/setup"' not in res_nh_events.text

        # Host on account page sees Go to My events; My events has the New event button
        res_h_account = await http.get("/account", cookies={"user_token": token_host})
        assert res_h_account.status_code == 200
        assert "Go to My events" in res_h_account.text
        res_h_my_events = await http.get("/workspace/", cookies={"user_token": token_host})
        assert 'href="/workspace/setup" class="me-new-event"' in res_h_my_events.text

        # Host on workspace sees + New event button
        res_h_events = await http.get("/workspace/events/", cookies={"user_token": token_host})
        assert res_h_events.status_code == 200
        assert "+ New event" in res_h_events.text

    # User whose hosting access is revoked (can_host=False, hosting_revoked_at set) and owns an existing event:
    async with get_session() as session:
        revoked_owner = await create_user(
            session,
            email="revoked@example.com",
            display_name="Revoked Owner",
            password_hash=hash_password("pw123"),
            can_host=False,
            hosting_revoked_at=utc_now(),
        )
        ev = await create_event(session, slug="existing-ev", display_name="Existing Event")
        await set_event_membership(session, user_id=revoked_owner.id, event_id=ev.id, role="event_owner")
        revoked_id = revoked_owner.id
        event_id = ev.id

    token_revoked = create_user_token(user_id=revoked_id, email="revoked@example.com")
    async with client() as http:
        # 1. My events still links to the owned event (Issue #761: revocation does not block access to owned events)
        res_rev_events = await http.get("/workspace/", cookies={"user_token": token_revoked})
        assert res_rev_events.status_code == 200
        assert f'href="/workspace/events/{event_id}/" class="me-action">Manage event</a>' in res_rev_events.text
        assert 'href="/workspace/setup"' not in res_rev_events.text
        res_rev_account = await http.get("/account", cookies={"user_token": token_revoked})
        assert res_rev_account.status_code == 200
        # 2. Hosting events card says access was removed and offers Request access again
        assert "Hosting access was removed" in res_rev_account.text
        assert 'href="/account/hosting-request"' in res_rev_account.text

        # 3. Can access their already created event (NOT blocked)
        res_ev = await http.get(f"/workspace/events/{event_id}/", cookies={"user_token": token_revoked}, follow_redirects=False)
        assert res_ev.status_code == 200

        # 4. API request to event returns 200
        res_api = await http.get(
            f"/workspace/events/{event_id}/",
            cookies={"user_token": token_revoked},
            headers={"Accept": "application/json"},
        )
        assert res_api.status_code == 200

        # 5. But cannot create new events
        res_create = await http.post(
            "/workspace/events/",
            data={"slug": "new-blocked", "display_name": "Blocked"},
            cookies={"user_token": token_revoked},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert res_create.status_code == 303
        assert res_create.headers["location"] == "/account?error=hosting_revoked"

        res_create_api = await http.post(
            "/workspace/events/",
            json={"slug": "new-blocked", "display_name": "Blocked"},
            cookies={"user_token": token_revoked},
            headers={"Origin": "http://test", "Accept": "application/json"},
        )
        assert res_create_api.status_code == 403
        assert res_create_api.json()["detail"] == "Hosting access revoked."



@pytest.mark.anyio
async def test_400_html_error_screen():
    """400 exceptions render unified error screen for HTML clients and JSON for API clients."""
    async with get_session() as session:
        admin = await create_user(
            session,
            email="admin_err@example.com",
            display_name="Admin",
            password_hash=hash_password("pw123"),
            is_admin=True,
        )
    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)

    async with client() as http:
        # Browser client requesting text/html receives unified 400.html screen
        res_html = await http.post(
            "/admin/users/invite",
            data={"email": "admin_err@example.com"},
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test", "Accept": "text/html,application/xhtml+xml"},
        )
        assert res_html.status_code == 400
        assert "Bad Request" in res_html.text
        assert "400" in res_html.text
        assert "User is already an administrator." in res_html.text
        assert "Go Back" in res_html.text
        assert "/static/css/error.css" in res_html.text

        # API client receives JSON
        res_json = await http.post(
            "/admin/users/invite",
            data={"email": "admin_err@example.com"},
            cookies={"user_token": admin_token},
            headers={"Origin": "http://test", "Accept": "application/json"},
        )
        assert res_json.status_code == 400
        assert res_json.json() == {"detail": "User is already an administrator."}


@pytest.mark.anyio
async def test_admin_users_table_layout_and_flash_alerts():
    """User table shows flash messages and keeps Hosting column clean without action buttons."""
    async with get_session() as session:
        admin = await create_user(
            session,
            email="admin_layout@example.com",
            display_name="Admin",
            password_hash=hash_password("pw123"),
            is_admin=True,
        )
        invited = await create_user(
            session,
            email="invited@example.com",
            display_name="Invited User",
            password_hash=None,
        )
        tok = await create_auth_token(
            session,
            user_id=invited.id,
            token_type="hosting_invite",
            expires_in_seconds=3600,
        )
    admin_token = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)

    async with client() as http:
        # Success banner renders
        res = await http.get("/admin/users/?success=invite_sent", cookies={"user_token": admin_token})
        assert res.status_code == 200
        assert "Invitation sent successfully." in res.text
        assert "alert-success" in res.text

        # The list tags the pending invitation; resend and cancel live on the user page (#765)
        assert "Invitation pending</span>" in res.text
        detail = await http.get(f"/admin/users/{invited.id}/", cookies={"user_token": admin_token})
        assert f"/admin/users/invite/{tok.id}/resend" in detail.text
        assert f"/admin/users/invite/{tok.id}/cancel" in detail.text


@pytest.mark.anyio
async def test_send_demo_request_email_renders_html():
    """Verify send_demo_request_email renders the neo-brutalist HTML template and sends html subtype."""
    from portal.email_sender import send_demo_request_email

    sent_messages = []

    with patch("portal.email_sender.FastMail.send_message", new_callable=AsyncMock) as mock_send, \
         patch("portal.email_sender.settings.smtp_host", "localhost"):
        mock_send.side_effect = lambda msg: sent_messages.append(msg)
        await send_demo_request_email({
            "email": "applicant@example.com",
            "firstName": "Jane",
            "lastName": "Doe",
            "companyName": "Tech Corp",
            "phoneNumber": "+1234567890",
            "country": "US",
            "industry": "Education",
            "companySize": "11-50",
            "languageSupport": "Live translation",
            "notes": "Looking forward to using VoxBento",
            "user_id": 42,
        })

    assert len(sent_messages) == 1
    msg = sent_messages[0]
    assert msg.subject == "New Demo Request from applicant@example.com"
    assert any("voxbento.dev@gmail.com" in str(r) for r in msg.recipients)
    assert "html" in str(msg.subtype)
    assert "Jane Doe" in msg.body
    assert "applicant@example.com" in msg.body
    assert "Tech Corp" in msg.body
    assert "Education" in msg.body
    assert "Review in Admin Panel" in msg.body
    assert "/admin/users/42/" in msg.body


@pytest.mark.anyio
async def test_unverified_user_with_password_cannot_redeem_without_verification():
    """An unverified user with an existing password hash cannot redeem an invite without verifying their email first."""
    original_pw = "OriginalPassword123!"
    async with get_session() as session:
        user = await create_user(
            session,
            email="unverified_has_pw@example.com",
            display_name="Unverified User",
            password_hash=hash_password(original_pw),
            email_verified=False,
            can_host=False,
        )
        raw_token, tok = await create_hosting_invite(session, user.id, 3600)
        user_id = user.id

    async with client() as http:
        # GET should indicate user must verify email first
        resp_get = await http.get(f"/invite/accept/{raw_token}")
        assert resp_get.status_code == 200
        assert "verify your email address" in resp_get.text.lower()

        # POST without password fails
        resp_post_no_pw = await http.post(
            f"/invite/accept/{raw_token}",
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp_post_no_pw.status_code == 400
        assert "verify your email address" in resp_post_no_pw.text.lower()

        # POST with password also fails because email is unverified
        resp_post_pw = await http.post(
            f"/invite/accept/{raw_token}",
            data={"password": original_pw},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp_post_pw.status_code == 400
        assert "verify your email address" in resp_post_pw.text.lower()

        async with get_session() as session:
            updated = await get_user_by_id(session, user_id)
            assert updated.can_host is False
            assert updated.email_verified is False
            assert verify_password(original_pw, updated.password_hash) is True


@pytest.mark.anyio
async def test_verified_user_with_password_requires_authentication_or_password():
    """A verified user with an existing password hash must authenticate or supply password to redeem."""
    correct_pw = "ValidPassword123!"
    async with get_session() as session:
        user = await create_user(
            session,
            email="verified_has_pw@example.com",
            display_name="Verified User",
            password_hash=hash_password(correct_pw),
            email_verified=True,
            can_host=False,
        )
        raw_token, tok = await create_hosting_invite(session, user.id, 3600)
        user_id = user.id

    async with client() as http:
        # GET displays password verification form when unauthenticated
        resp_get = await http.get(f"/invite/accept/{raw_token}")
        assert resp_get.status_code == 200
        assert "Verify Password & Accept Invitation" in resp_get.text

        # POST without password when unauthenticated -> 400
        resp_post_no_pw = await http.post(
            f"/invite/accept/{raw_token}",
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp_post_no_pw.status_code == 400
        assert "invalid password" in resp_post_no_pw.text.lower()

        # POST with incorrect password -> 400
        resp_post_wrong = await http.post(
            f"/invite/accept/{raw_token}",
            data={"password": "WrongPassword999!"},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp_post_wrong.status_code == 400
        assert "invalid password" in resp_post_wrong.text.lower()

        # POST with correct password -> 303 redirect
        resp_post_ok = await http.post(
            f"/invite/accept/{raw_token}",
            data={"password": correct_pw},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp_post_ok.status_code == 303
        assert resp_post_ok.headers["location"] == "/workspace/"

        async with get_session() as session:
            updated = await get_user_by_id(session, user_id)
            assert updated.can_host is True
            assert updated.email_verified is True
            assert verify_password(correct_pw, updated.password_hash) is True

    # When already authenticated, accepts directly without password
    async with get_session() as session:
        user2 = await create_user(
            session,
            email="auth_user@example.com",
            display_name="Auth User",
            password_hash=hash_password("UserPassword123!"),
            email_verified=True,
            can_host=False,
        )
        raw_token2, tok2 = await create_hosting_invite(session, user2.id, 3600)
        user2_id = user2.id

    user2_cookie = create_user_token(user_id=user2_id, email="auth_user@example.com")
    async with client() as http:
        # GET shows direct accept button
        resp_get_auth = await http.get(f"/invite/accept/{raw_token2}", cookies={"user_token": user2_cookie})
        assert resp_get_auth.status_code == 200
        assert "Accept Invitation & Go to Workspace" in resp_get_auth.text

        # POST accepts without password
        resp_post_auth = await http.post(
            f"/invite/accept/{raw_token2}",
            cookies={"user_token": user2_cookie},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp_post_auth.status_code == 303
        assert resp_post_auth.headers["location"] == "/workspace/"

        async with get_session() as session:
            updated2 = await get_user_by_id(session, user2_id)
            assert updated2.can_host is True


@pytest.mark.anyio
async def test_signed_in_passwordless_user_can_accept_invite():
    """When a user without a password (e.g. stub or magic link) is already signed in, accepting the invite succeeds without requiring a password."""
    async with get_session() as session:
        user = await create_user(
            session,
            email="passwordless_signed_in@example.com",
            display_name="Passwordless Signed In",
            password_hash=None,
            email_verified=False,
            can_host=False,
        )
        raw_token, tok = await create_hosting_invite(session, user.id, 3600)
        user_id = user.id

    cookie = create_user_token(user_id=user_id, email="passwordless_signed_in@example.com")
    async with client() as http:
        # GET shows accept button without password inputs
        resp_get = await http.get(f"/invite/accept/{raw_token}", cookies={"user_token": cookie})
        assert resp_get.status_code == 200
        assert "Accept Invitation & Go to Workspace" in resp_get.text

        # POST submits with empty/no password and succeeds
        resp_post = await http.post(
            f"/invite/accept/{raw_token}",
            cookies={"user_token": cookie},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert resp_post.status_code == 303
        assert resp_post.headers["location"] == "/workspace/"

        async with get_session() as session:
            updated = await get_user_by_id(session, user_id)
            assert updated.can_host is True


@pytest.mark.anyio
async def test_concurrent_redemption_atomicity(tmp_path):
    """Two concurrent redemption requests for the same token result in exactly one success and one 400."""
    db_file = tmp_path / "test_concurrent.db"
    await dispose()
    configure(f"sqlite+aiosqlite:///{db_file}")
    await init_db()
    try:
        async with get_session() as session:
            user = await create_user(
                session,
                email="concurrent_stub@example.com",
                display_name="Concurrent Stub",
                password_hash=None,
                email_verified=False,
                can_host=False,
            )
            raw_token, tok = await create_hosting_invite(session, user.id, 3600)
            user_id = user.id

        async with client() as http:
            req1 = http.post(
                f"/invite/accept/{raw_token}",
                data={"display_name": "Winner 1", "password": "Password123!", "password_confirm": "Password123!"},
                headers={"Origin": "http://test"},
                follow_redirects=False,
            )
            req2 = http.post(
                f"/invite/accept/{raw_token}",
                data={"display_name": "Winner 2", "password": "Password123!", "password_confirm": "Password123!"},
                headers={"Origin": "http://test"},
                follow_redirects=False,
            )
            res1, res2 = await asyncio.gather(req1, req2)

            status_codes = sorted([res1.status_code, res2.status_code])
            assert status_codes == [303, 400]

            err_res = res1 if res1.status_code == 400 else res2
            assert "invalid or expired invite token" in err_res.text.lower()

            async with get_session() as session:
                updated = await get_user_by_id(session, user_id)
                assert updated.can_host is True
                assert updated.password_hash is not None
                used_tok = await session.get(AuthToken, tok.id)
                assert used_tok.is_used is True
    finally:
        await dispose()
        configure("sqlite+aiosqlite://")
        await init_db()


@pytest.mark.anyio
async def test_anonymous_demo_request_does_not_mutate_existing_user_hosting_requested_at():
    """An unauthenticated request-demo matching an existing user's email does not mutate their hosting_requested_at flag (CWE-862)."""
    async with get_session() as session:
        target = await create_user(
            session,
            email="target_user@example.com",
            display_name="Target User",
            password_hash=hash_password("password123"),
            can_host=False,
        )
        target_id = target.id

    with patch("portal.routers.public.send_demo_request_email", new_callable=AsyncMock) as mock_send:
        async with client() as http:
            res = await http.post(
                "/api/request-demo",
                json={
                    "firstName": "Spoofer",
                    "lastName": "User",
                    "email": "target_user@example.com",
                    "companyName": "Acme",
                    "notes": "Testing unauthorized bypass",
                },
            )
            assert res.status_code == 200
            assert res.json()["ok"] is True

            # Verify email sent with source="home"
            called_info = mock_send.call_args[0][0]
            assert called_info["source"] == "home"

            # DB flag must NOT have been updated
            async with get_session() as session:
                target_check = await get_user_by_id(session, target_id)
                assert target_check.hosting_requested_at is None


@pytest.mark.anyio
async def test_demo_modal_privacy_checkbox_starts_unchecked_and_is_required():
    """Verify demo privacy consent checkbox is unchecked by default and marked required."""
    import re

    async with client() as http:
        resp_home = await http.get("/")
        assert resp_home.status_code == 200
        home_match = re.search(r"<input[^>]*id=[\"']demo-privacy[\"'][^>]*>", resp_home.text)
        assert home_match is not None, "demo-privacy checkbox not found on home page"
        tag = home_match.group(0)
        assert "required" in tag, "demo-privacy checkbox must have required attribute"
        assert "checked" not in tag, "demo-privacy checkbox must not start checked"


@pytest.mark.anyio
async def test_alembic_028_migration_with_pre_existing_memberships():
    """Verify migration 028 from a real pre-028 schema with existing memberships.

    Ensures event owners are backfilled with can_host=True while other users remain can_host=False.
    Also verifies clean downgrade to 027.
    """
    import importlib.util
    import sqlite3
    import tempfile
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine

    # Load 029 migration module
    mig_path = Path(__file__).resolve().parent.parent / "alembic" / "versions" / "029_add_can_host_to_users.py"
    spec = importlib.util.spec_from_file_location("migration_029", mig_path)
    mig_029 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig_029)

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    try:
        # 1. Create real pre-028 schema (027 state: users has NO can_host/hosting flags)
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email VARCHAR(255) NOT NULL UNIQUE,
                display_name VARCHAR(255),
                password_hash VARCHAR(255),
                email_verified BOOLEAN NOT NULL DEFAULT 0,
                is_admin BOOLEAN NOT NULL DEFAULT 0,
                created_at DATETIME
            )
        """)
        cur.execute("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100),
                display_name VARCHAR(200) NOT NULL,
                slug VARCHAR(64) NOT NULL UNIQUE,
                created_at DATETIME
            )
        """)
        cur.execute("""
            CREATE TABLE event_memberships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                role VARCHAR(32) NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (event_id) REFERENCES events(id)
            )
        """)

        # 2. Insert existing users, event, and memberships
        cur.execute("INSERT INTO users (id, email) VALUES (1, 'owner@example.com')")
        cur.execute("INSERT INTO users (id, email) VALUES (2, 'interpreter@example.com')")
        cur.execute("INSERT INTO users (id, email) VALUES (3, 'regular@example.com')")
        cur.execute("INSERT INTO events (id, name, display_name, slug) VALUES (1, 'Test Event', 'Test Event', 'test-event')")
        cur.execute("INSERT INTO event_memberships (user_id, event_id, role) VALUES (1, 1, 'event_owner')")
        cur.execute("INSERT INTO event_memberships (user_id, event_id, role) VALUES (2, 1, 'interpreter')")
        conn.commit()
        conn.close()

        # 3. Run migration 029 upgrade() using Alembic Operations on the SQLite connection
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as sa_conn:
            ctx = MigrationContext.configure(sa_conn)
            with Operations.context(ctx):
                mig_029.upgrade()
            sa_conn.commit()

        # 4. Verify post-upgrade schema and data
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        columns_post = [row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()]
        assert "can_host" in columns_post
        assert "hosting_requested_at" in columns_post
        assert "hosting_revoked_at" in columns_post

        # Verify backfill results: user 1 must have can_host=1, others can_host=0
        users_by_id = {row[0]: row[1] for row in cur.execute("SELECT id, can_host FROM users").fetchall()}
        assert users_by_id[1] == 1, "Event owner should have can_host=True"
        assert users_by_id[2] == 0, "Interpreter should have can_host=False"
        assert users_by_id[3] == 0, "Regular user should have can_host=False"
        conn.close()

        # 5. Run migration 029 downgrade()
        with engine.connect() as sa_conn:
            ctx = MigrationContext.configure(sa_conn)
            with Operations.context(ctx):
                mig_029.downgrade()
            sa_conn.commit()

        # 6. Verify columns are dropped cleanly
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        columns_downgrade = [row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()]
        assert "can_host" not in columns_downgrade
        assert "hosting_requested_at" not in columns_downgrade
        assert "hosting_revoked_at" not in columns_downgrade
        conn.close()
        engine.dispose()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)


@pytest.mark.anyio
async def test_transient_db_lock_during_redemption_returns_503():
    """Transient SQLite lock conflict when token is not claimed returns 503 instead of 400."""
    from sqlalchemy.exc import OperationalError

    from portal.models import AuthToken

    async with get_session() as session:
        user = await create_user(
            session,
            email="transient_lock@example.com",
            display_name="Transient Lock User",
            password_hash=hash_password("Password123!"),
            email_verified=True,
            can_host=False,
        )
        raw_token, tok = await create_hosting_invite(session, user.id, 3600)
        tok_id = tok.id

    with patch("sqlalchemy.ext.asyncio.AsyncSession.flush", side_effect=OperationalError("database is locked", None, Exception("database is locked"))):
        async with client() as http:
            resp = await http.post(
                f"/invite/accept/{raw_token}",
                data={"password": "Password123!"},
                headers={"Origin": "http://test"},
                follow_redirects=False,
            )
            assert resp.status_code == 503
            assert "temporarily unavailable" in resp.text.lower() or "busy" in resp.text.lower()

    # Verify the token remains unused and can subsequently be redeemed successfully
    async with get_session() as session:
        token_after = await session.get(AuthToken, tok_id)
        assert token_after.is_used is False

    async with client() as http:
        retry_resp = await http.post(
            f"/invite/accept/{raw_token}",
            data={"password": "Password123!"},
            headers={"Origin": "http://test"},
            follow_redirects=False,
        )
        assert retry_resp.status_code == 303
        assert retry_resp.headers["location"] == "/workspace/"



