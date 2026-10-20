"""Tests for user registration, login, account, and admin user management."""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest

from portal.auth import create_user_token, hash_password, verify_password

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def admin_cookie():
    from portal.database import create_user, get_session

    async with get_session() as s:
        user = await create_user(
            s,
            email="admin@example.com",
            display_name="Admin",
            password_hash=hash_password("test-admin-pass"),
            is_admin=True,
        )
    return {"user_token": create_user_token(user_id=user.id, email="admin@example.com", is_admin=True)}


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_test_user(email="test@example.com", display_name="Test User", password="securepass123"):
    from portal.database import create_user, get_session

    pw_hash = hash_password(password) if password else None
    async with get_session() as s:
        user = await create_user(s, email=email, display_name=display_name, password_hash=pw_hash, email_verified=True)
    return user


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


class TestPasswordHashing:
    @pytest.mark.anyio
    async def test_hash_and_verify(self, setup_db):
        pw = "my-secret-password"
        hashed = hash_password(pw)
        assert hashed != pw
        assert verify_password(pw, hashed)

    @pytest.mark.anyio
    async def test_wrong_password_fails(self, setup_db):
        hashed = hash_password("correct")
        assert not verify_password("wrong", hashed)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    @pytest.mark.anyio
    async def test_register_page_renders(self, setup_db):
        async with _client() as c:
            resp = await c.get("/register")
        assert resp.status_code == 200
        assert b"Create your account" in resp.content

    @pytest.mark.anyio
    async def test_register_creates_listener_account(self, setup_db):
        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "new@example.com",
                    "display_name": "New User",
                    "password": "securepass123",
                    "password_confirm": "securepass123",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert b"Check your email" in resp.content

    @pytest.mark.anyio
    async def test_register_rejects_duplicate_email(self, setup_db):
        await _create_test_user(email="dupe@example.com")
        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "dupe@example.com",
                    "display_name": "Another User",
                    "password": "securepass123",
                    "password_confirm": "securepass123",
                },
            )
        assert resp.status_code == 200
        assert b"eligible for registration" in resp.content
        assert b"already exists" not in resp.content

    @pytest.mark.anyio
    async def test_duplicate_unverified_registration_refreshes_once(self, setup_db, monkeypatch):
        from sqlalchemy import select

        import portal.routers.auth as auth_router
        from portal.database import create_auth_token, create_user, get_session
        from portal.models import AuthToken

        sent_tokens = []

        async def fake_send_verification_email(email, token):
            sent_tokens.append((email, token))
            return True

        monkeypatch.setattr(auth_router, "send_verification_email", fake_send_verification_email)

        async with get_session() as session:
            user = await create_user(
                session,
                email="unverified-duplicate@example.com",
                display_name="Unverified",
                email_verified=False,
            )
            await create_auth_token(
                session,
                jti="old-verification-token",
                user_id=user.id,
                token_type="verification",
            )

        form = {
            "email": "unverified-duplicate@example.com",
            "display_name": "Another Name",
            "password": "securepass123",
            "password_confirm": "securepass123",
        }
        async with _client() as c:
            first = await c.post("/register", data=form)
            second = await c.post("/register", data=form)

        assert first.status_code == second.status_code == 200
        assert b"eligible for registration" in first.content
        assert b'href="/login"' in first.content
        assert len(sent_tokens) == 1

        async with get_session() as session:
            tokens = list(
                (await session.execute(select(AuthToken).where(AuthToken.user_id == user.id).order_by(AuthToken.id)))
                .scalars()
                .all()
            )
        assert len(tokens) == 2
        assert tokens[0].used_at is None
        assert sum(token.used_at is None for token in tokens) == 2

    @pytest.mark.anyio
    async def test_duplicate_registration_cannot_activate_preexisting_password(self, setup_db, monkeypatch):
        import portal.routers.auth as auth_router
        from portal.database import create_user, get_session, get_user_by_email

        sent_tokens = []

        async def fake_send_delayed_onboarding_email(_email, _display_name):
            return None

        async def fake_send_verification_email(_email, token):
            sent_tokens.append(token)
            return True

        monkeypatch.setattr(auth_router, "send_verification_email", fake_send_verification_email)
        monkeypatch.setattr(auth_router, "send_delayed_onboarding_email", fake_send_delayed_onboarding_email)

        async with get_session() as session:
            await create_user(
                session,
                email="preclaimed@example.com",
                display_name="Earlier registrant",
                password_hash=hash_password("attacker-password"),
                email_verified=False,
                is_active=True,
            )

        async with _client() as c:
            registration = await c.post(
                "/register",
                data={
                    "email": "preclaimed@example.com",
                    "display_name": "Email owner",
                    "password": "owner-password",
                    "password_confirm": "owner-password",
                },
            )
            verification = await c.get(f"/auth/verify/{sent_tokens[0]}", follow_redirects=False)
            login = await c.post(
                "/login",
                data={"email": "preclaimed@example.com", "password": "attacker-password"},
            )

        assert registration.status_code == 200
        assert verification.status_code == 303
        assert login.status_code == 403

        async with get_session() as session:
            user = await get_user_by_email(session, "preclaimed@example.com")
        assert user is not None
        assert user.email_verified is True
        assert user.password_hash is None

    @pytest.mark.anyio
    async def test_duplicate_registration_preserves_old_token_when_resend_fails(self, setup_db, monkeypatch):
        from sqlalchemy import select

        import portal.routers.auth as auth_router
        from portal.database import create_auth_token, create_user, get_session
        from portal.models import AuthToken

        async def fake_send_verification_email(_email, _token):
            return False

        monkeypatch.setattr(auth_router, "send_verification_email", fake_send_verification_email)

        async with get_session() as session:
            user = await create_user(
                session,
                email="unverified-delivery-failure@example.com",
                display_name="Unverified",
                email_verified=False,
            )
            await create_auth_token(
                session,
                jti="delivered-verification-token",
                user_id=user.id,
                token_type="verification",
            )

        async with _client() as c:
            response = await c.post(
                "/register",
                data={
                    "email": "unverified-delivery-failure@example.com",
                    "display_name": "Another Name",
                    "password": "securepass123",
                    "password_confirm": "securepass123",
                },
            )

        assert response.status_code == 200
        async with get_session() as session:
            tokens = list(
                (await session.execute(select(AuthToken).where(AuthToken.user_id == user.id).order_by(AuthToken.id)))
                .scalars()
                .all()
            )
        assert len(tokens) == 2
        assert tokens[0].used_at is None
        assert tokens[1].used_at is not None

    @pytest.mark.anyio
    async def test_register_rejects_mismatched_password_confirm(self, setup_db):
        from portal.database import get_session, get_user_by_email

        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "typo@example.com",
                    "display_name": "Typo User",
                    "password": "securepass123",
                    "password_confirm": "securepass124",
                },
            )
        assert resp.status_code == 422
        assert b"Passwords do not match." in resp.content
        async with get_session() as s:
            assert await get_user_by_email(s, "typo@example.com") is None

    @pytest.mark.anyio
    async def test_register_rejects_short_password(self, setup_db):
        from portal.database import get_session, get_user_by_email

        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "short@example.com",
                    "display_name": "Short User",
                    "password": "1234567",
                    "password_confirm": "1234567",
                },
            )
        assert resp.status_code == 422
        assert b"Password must be at least 8 characters." in resp.content
        async with get_session() as s:
            assert await get_user_by_email(s, "short@example.com") is None

    @pytest.mark.anyio
    async def test_register_accepts_eight_character_password(self, setup_db):
        from portal.database import get_session, get_user_by_email

        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "exact8@example.com",
                    "display_name": "Exact 8 User",
                    "password": "12345678",
                    "password_confirm": "12345678",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert b"Check your email" in resp.content
        async with get_session() as s:
            assert await get_user_by_email(s, "exact8@example.com") is not None

    @pytest.mark.anyio
    async def test_register_accepts_password_with_leading_trailing_spaces(self, setup_db):
        from portal.database import get_session, get_user_by_email

        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "spaces@example.com",
                    "display_name": "Spaces User",
                    "password": "  abcdef  ",
                    "password_confirm": "  abcdef  ",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert b"Check your email" in resp.content
        async with get_session() as s:
            assert await get_user_by_email(s, "spaces@example.com") is not None

    @pytest.mark.anyio
    async def test_register_rejects_whitespace_only_password(self, setup_db):
        from portal.database import get_session, get_user_by_email

        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "whitespace@example.com",
                    "display_name": "Whitespace User",
                    "password": "   ",
                    "password_confirm": "   ",
                },
            )
        assert resp.status_code == 422
        assert b"Password cannot be blank or only whitespace." in resp.content
        async with get_session() as s:
            assert await get_user_by_email(s, "whitespace@example.com") is None

    @pytest.mark.anyio
    async def test_register_passwordless_needs_no_confirm(self, setup_db):
        async with _client() as c:
            resp = await c.post(
                "/register",
                data={"email": "nopass@example.com", "display_name": "No Pass"},
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert b"Check your email" in resp.content

    @pytest.mark.anyio
    async def test_register_passwordless_ignores_leftover_confirm(self, setup_db):
        async with _client() as c:
            resp = await c.post(
                "/register",
                data={
                    "email": "leftover@example.com",
                    "display_name": "Leftover",
                    "password": "",
                    "password_confirm": "stale-value",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 200
        assert b"Check your email" in resp.content

    @pytest.mark.anyio
    async def test_register_creates_active_non_admin(self, setup_db):
        from portal.database import get_session, get_user_by_email

        async with _client() as c:
            await c.post(
                "/register",
                data={
                    "email": "listener@example.com",
                    "display_name": "Listener",
                    "password": "securepass123",
                    "password_confirm": "securepass123",
                },
                follow_redirects=False,
            )
        async with get_session() as s:
            user = await get_user_by_email(s, "listener@example.com")
        assert user is not None
        assert user.is_admin is False
        assert user.is_active is True


# ---------------------------------------------------------------------------
# Email discovery privacy
# ---------------------------------------------------------------------------


class TestEmailDiscoveryPrivacy:
    @pytest.mark.anyio
    async def test_check_email_has_the_same_neutral_response_for_every_account_state(self, setup_db):
        await _create_test_user(email="password@example.com", password="securepass123")
        await _create_test_user(email="passwordless@example.com", password=None)

        async with _client() as c:
            responses = [
                await c.post("/api/auth/check-email", json={"email": email})
                for email in (
                    "password@example.com",
                    "passwordless@example.com",
                    "unknown@example.com",
                )
            ]

        assert [response.status_code for response in responses] == [200, 200, 200]
        assert [response.json() for response in responses] == [
            {"status": "ok", "exists": True, "has_password": True},
            {"status": "ok", "exists": True, "has_password": True},
            {"status": "ok", "exists": True, "has_password": True},
        ]

    @pytest.mark.anyio
    async def test_login_page_does_not_branch_on_account_state(self, setup_db):
        async with _client() as c:
            response = await c.get("/login")

        assert response.status_code == 200
        assert b"data.exists" not in response.content
        assert b"data.has_password" not in response.content
        assert b'id="register-link"' in response.content
        assert b"/register?email=" not in response.content


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


class TestUserLogin:
    @pytest.mark.anyio
    async def test_login_page_renders(self, setup_db):
        async with _client() as c:
            resp = await c.get("/login")
        assert resp.status_code == 200
        assert b"Sign in" in resp.content
        assert b'type="button" id="forgot-password-link"' in resp.content
        assert b'href="#" id="forgot-password-link"' not in resp.content

    @pytest.mark.anyio
    async def test_login_with_correct_credentials(self, setup_db):
        await _create_test_user(email="login@example.com", password="securepass123")
        async with _client() as c:
            resp = await c.post(
                "/login",
                data={
                    "email": "login@example.com",
                    "password": "securepass123",
                },
                follow_redirects=False,
            )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/account"
        assert "user_token" in resp.headers.get("set-cookie", "")

    @pytest.mark.anyio
    async def test_login_with_wrong_password(self, setup_db):
        await _create_test_user(email="login@example.com", password="securepass123")
        async with _client() as c:
            resp = await c.post(
                "/login",
                data={
                    "email": "login@example.com",
                    "password": "wrongpassword",
                },
            )
        assert resp.status_code == 403
        assert b"Invalid email or password" in resp.content

    @pytest.mark.anyio
    async def test_login_with_nonexistent_email(self, setup_db):
        async with _client() as c:
            resp = await c.post(
                "/login",
                data={
                    "email": "nobody@example.com",
                    "password": "whatever",
                },
            )
        assert resp.status_code == 403
        assert b"Invalid email or password" in resp.content

    @pytest.mark.anyio
    async def test_login_with_passwordless_account_uses_generic_error(self, setup_db):
        await _create_test_user(email="passwordless-login@example.com", password=None)
        async with _client() as c:
            resp = await c.post(
                "/login",
                data={
                    "email": "passwordless-login@example.com",
                    "password": "whatever",
                },
            )
        assert resp.status_code == 403
        assert b"Invalid email or password" in resp.content
        assert b"Please verify" not in resp.content

    @pytest.mark.anyio
    async def test_passwordless_account_rejected_even_if_dummy_verification_succeeds(self, setup_db, monkeypatch):
        import portal.routers.auth as auth_router

        await _create_test_user(email="passwordless-dummy@example.com", password=None)
        monkeypatch.setattr(auth_router, "verify_password", lambda _password, _password_hash: True)

        async with _client() as c:
            resp = await c.post(
                "/login",
                data={
                    "email": "passwordless-dummy@example.com",
                    "password": "any-password",
                },
            )

        assert resp.status_code == 403
        assert b"Invalid email or password" in resp.content
        assert "user_token" not in resp.headers.get("set-cookie", "")

    @pytest.mark.anyio
    async def test_login_deactivated_user_rejected(self, setup_db):
        from portal.database import get_session, update_user_active

        user = await _create_test_user(email="deactivated@example.com", password="securepass123")
        async with get_session() as s:
            await update_user_active(s, user.id, is_active=False)
        async with _client() as c:
            resp = await c.post(
                "/login",
                data={
                    "email": "deactivated@example.com",
                    "password": "securepass123",
                },
            )
        assert resp.status_code == 403
        assert b"Invalid email or password" in resp.content
        assert b"Your account has been deactivated" not in resp.content

    @pytest.mark.anyio
    async def test_deactivated_user_cookie_is_not_authenticated(self, setup_db):
        from portal.database import get_session, update_user_active

        user = await _create_test_user(email="stale-cookie@example.com")
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        async with get_session() as session:
            await update_user_active(session, user.id, is_active=False)

        async with _client() as c:
            response = await c.get("/api/auth/status", cookies={"user_token": token})

        assert response.status_code == 200
        assert response.json() == {"authenticated": False}


class TestEmailVerification:
    @pytest.mark.anyio
    async def test_inactive_user_cannot_verify_or_receive_a_session(self, setup_db):
        from portal.database import create_auth_token, create_user, get_auth_token, get_session, get_user_by_email

        async with get_session() as session:
            user = await create_user(
                session,
                email="inactive-unverified@example.com",
                display_name="Inactive",
                email_verified=False,
                is_active=False,
            )
            await create_auth_token(
                session,
                jti="inactive-verification-token",
                user_id=user.id,
                token_type="verification",
            )

        async with _client() as c:
            response = await c.get("/auth/verify/inactive-verification-token", follow_redirects=False)

        assert response.status_code == 400
        assert "user_token" not in response.headers.get("set-cookie", "")
        async with get_session() as session:
            user_after = await get_user_by_email(session, "inactive-unverified@example.com")
            token_after = await get_auth_token(session, "inactive-verification-token")
        assert user_after is not None
        assert user_after.email_verified is False
        assert token_after is not None
        assert token_after.is_used is False


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


class TestPasswordReset:
    @pytest.mark.anyio
    async def test_reset_password_page_renders_for_valid_token(self, setup_db):
        from portal.database import create_auth_token, get_session

        user = await _create_test_user(email="reset-valid@example.com")
        async with get_session() as s:
            await create_auth_token(s, jti="valid-reset-token", user_id=user.id, token_type="password_reset")

        async with _client() as c:
            resp = await c.get("/auth/reset/valid-reset-token")
        assert resp.status_code == 200
        assert b'<form method="post" action="/auth/reset/valid-reset-token"' in resp.content
        assert b"alert-error" not in resp.content

    @pytest.mark.anyio
    async def test_reset_password_page_rejects_nonexistent_token(self, setup_db):
        async with _client() as c:
            resp = await c.get("/auth/reset/nonexistent-token")
        assert resp.status_code == 400
        assert b"<form" not in resp.content
        assert b"Invalid reset link" in resp.content

    @pytest.mark.anyio
    async def test_reset_password_page_rejects_expired_token(self, setup_db):
        from portal.database import create_auth_token, get_session

        user = await _create_test_user(email="reset-expired@example.com")
        async with get_session() as s:
            await create_auth_token(
                s, jti="expired-reset-token", user_id=user.id, token_type="password_reset", expires_in_seconds=-3600
            )

        async with _client() as c:
            resp = await c.get("/auth/reset/expired-reset-token")
        assert resp.status_code == 400
        assert b"<form" not in resp.content
        assert b"This reset link has expired" in resp.content

    @pytest.mark.anyio
    async def test_reset_password_page_rejects_used_token(self, setup_db):
        from datetime import datetime, timezone

        from portal.database import create_auth_token, get_session

        user = await _create_test_user(email="reset-used@example.com")
        async with get_session() as s:
            tok = await create_auth_token(s, jti="used-reset-token", user_id=user.id, token_type="password_reset")
            tok.used_at = datetime.now(timezone.utc)
            await s.flush()

        async with _client() as c:
            resp = await c.get("/auth/reset/used-reset-token")
        assert resp.status_code == 400
        assert b"<form" not in resp.content
        assert b"This reset link has already been used" in resp.content

    @pytest.mark.anyio
    async def test_reset_password_page_rejects_wrong_token_type(self, setup_db):
        from portal.database import create_auth_token, get_session

        user = await _create_test_user(email="reset-wrongtype@example.com")
        async with get_session() as s:
            await create_auth_token(s, jti="verification-token-not-reset", user_id=user.id, token_type="verification")

        async with _client() as c:
            resp = await c.get("/auth/reset/verification-token-not-reset")
        assert resp.status_code == 400
        assert b"<form" not in resp.content
        assert b"Invalid reset link" in resp.content

    @pytest.mark.anyio
    async def test_reset_password_get_does_not_consume_token(self, setup_db):
        from portal.database import create_auth_token, get_auth_token, get_session

        user = await _create_test_user(email="reset-not-consumed@example.com")
        async with get_session() as s:
            await create_auth_token(s, jti="unconsumed-token", user_id=user.id, token_type="password_reset")

        async with _client() as c:
            # Visit reset page via GET
            resp_get = await c.get("/auth/reset/unconsumed-token")
            assert resp_get.status_code == 200

            # Verify token is still not marked as used in DB
            async with get_session() as s:
                tok = await get_auth_token(s, "unconsumed-token")
                assert tok is not None
                assert tok.is_used is False

            # Submit password reset via POST
            resp_post = await c.post(
                "/auth/reset/unconsumed-token",
                data={"password": "brandnewpassword123"},
                follow_redirects=False,
            )
            assert resp_post.status_code == 303
            assert resp_post.headers["location"] == "/account"

            # Verify token is now marked as used
            async with get_session() as s:
                tok_after = await get_auth_token(s, "unconsumed-token")
                assert tok_after is not None
                assert tok_after.is_used is True

            # A follow-up GET after redemption must be rejected, not show the form
            resp_get_after = await c.get("/auth/reset/unconsumed-token")
            assert resp_get_after.status_code == 400
            assert b"<form" not in resp_get_after.content
            assert b"already been used" in resp_get_after.content

    @pytest.mark.anyio
    async def test_reset_password_post_validation_error_keeps_form_visible(self, setup_db):
        from portal.database import create_auth_token, get_auth_token, get_session

        user = await _create_test_user(email="reset-shortpass@example.com")
        async with get_session() as s:
            await create_auth_token(s, jti="retryable-token", user_id=user.id, token_type="password_reset")

        async with _client() as c:
            # 1. Submit password shorter than 8 characters
            resp_short = await c.post(
                "/auth/reset/retryable-token",
                data={"password": "short"},
                follow_redirects=False,
            )
            assert resp_short.status_code == 400
            assert b"Password must be at least 8 characters" in resp_short.content
            assert b'<form method="post" action="/auth/reset/retryable-token"' in resp_short.content
            assert b"Back to Login to request a new link" not in resp_short.content

            # 2. Token must remain unused in the database
            async with get_session() as s:
                tok = await get_auth_token(s, "retryable-token")
                assert tok is not None
                assert tok.is_used is False

            # 3. User can retry and successfully reset password with valid length
            resp_retry = await c.post(
                "/auth/reset/retryable-token",
                data={"password": "validpassword123"},
                follow_redirects=False,
            )
            assert resp_retry.status_code == 303
            assert resp_retry.headers["location"] == "/account"

            # 4. Token is now marked used
            async with get_session() as s:
                tok_after = await get_auth_token(s, "retryable-token")
                assert tok_after is not None
                assert tok_after.is_used is True

    @pytest.mark.anyio
    async def test_reset_password_post_redemption_failure_shows_invalid_token(self, setup_db):
        from portal.database import create_auth_token, get_session

        user = await _create_test_user(email="reset-expired-post@example.com")
        async with get_session() as s:
            await create_auth_token(
                s, jti="expired-post-token", user_id=user.id, token_type="password_reset", expires_in_seconds=-3600
            )

        async with _client() as c:
            resp = await c.post(
                "/auth/reset/expired-post-token",
                data={"password": "validpassword123"},
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert b"Token has expired." in resp.content
            assert b"Back to Login to request a new link" in resp.content
            assert b"<form" not in resp.content

    @pytest.mark.anyio
    async def test_reset_password_post_update_error_rolls_back_token_and_password(self, setup_db, monkeypatch):
        import portal.routers.auth as auth_router
        from portal.database import create_auth_token, get_auth_token, get_session, get_user_by_id

        user = await _create_test_user(email="reset-atomic@example.com", password="originalpassword123")
        original_hash = user.password_hash
        async with get_session() as s:
            await create_auth_token(s, jti="atomic-err-token", user_id=user.id, token_type="password_reset")

        def _mock_create_user_token(*args, **kwargs):
            raise RuntimeError("Token signing service unavailable")

        monkeypatch.setattr(auth_router, "create_user_token", _mock_create_user_token)

        async with _client() as c:
            resp = await c.post(
                "/auth/reset/atomic-err-token",
                data={"password": "brandnewpassword123"},
                follow_redirects=False,
            )
            assert resp.status_code == 500
            assert b"An error occurred while resetting your password. Please try again." in resp.content
            assert b"Token signing service unavailable" not in resp.content
            assert b"Back to Login to request a new link" not in resp.content

        # Verify database atomicity: both token usage and password update were rolled back
        async with get_session() as s:
            tok = await get_auth_token(s, "atomic-err-token")
            assert tok is not None
            assert tok.is_used is False
            assert tok.used_at is None

            user_after = await get_user_by_id(s, user.id)
            assert user_after is not None
            assert user_after.password_hash == original_hash
            assert verify_password("originalpassword123", user_after.password_hash) is True
            assert verify_password("brandnewpassword123", user_after.password_hash) is False


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


class TestUserLogout:
    @pytest.mark.anyio
    async def test_logout_clears_cookie(self, setup_db):
        async with _client() as c:
            resp = await c.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


# ---------------------------------------------------------------------------
# Account page
# ---------------------------------------------------------------------------


class TestAccountPage:
    @pytest.mark.anyio
    async def test_account_requires_login(self, setup_db):
        async with _client() as c:
            resp = await c.get("/account", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    @pytest.mark.anyio
    async def test_account_shows_user_info(self, setup_db):
        user = await _create_test_user(email="me@example.com", display_name="Me")
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        async with _client() as c:
            resp = await c.get("/account", cookies={"user_token": token})
        assert resp.status_code == 200
        assert b"me@example.com" in resp.content
        assert b"Me" in resp.content

    @pytest.mark.anyio
    async def test_account_booth_link_includes_room_and_resolves(self, setup_db):
        from portal.database import create_booth, create_event, create_room, get_session, set_booth_membership

        user = await _create_test_user(email="interp@example.com", display_name="Interp")
        async with get_session() as s:
            event = await create_event(s, slug="zephyr-2031", display_name="Zephyr Summit 2031")
            room = await create_room(s, event_id=event.id, display_name="Quartz Hall")
            booth = await create_booth(
                s, event_id=event.id, room_id=room.id, language_code="ja", language_name="Japanese"
            )
            await set_booth_membership(s, user_id=user.id, booth_id=booth.id, role="interpreter")
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        link = f"/interpreter/zephyr-2031/{room.id}/ja"

        async with _client() as c:
            my_events = await c.get("/workspace/", cookies={"user_token": token})
            booth_page = await c.get(link, follow_redirects=False)

        assert f'href="{link}"' in my_events.text
        assert 'href="/interpreter/zephyr-2031/ja"' not in my_events.text
        assert booth_page.status_code == 303
        assert booth_page.headers["location"].startswith(f"/login?next={link}")


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------


class TestAdminUserManagement:
    @pytest.mark.anyio
    async def test_user_list_shows_users(self, setup_db, admin_cookie):
        _u1 = await _create_test_user(email="user1@example.com", display_name="User One")
        _u2 = await _create_test_user(email="user2@example.com", display_name="User Two")
        u3 = await _create_test_user(email="user3@example.com", display_name="User Three")
        # u3 is the newest user, so it sorts first when descending by created_at.
        newest_id = max(_u1.id, _u2.id, u3.id)
        assert u3.id == newest_id

        async with _client() as c:
            resp = await c.get("/admin/users/?sort_by=created_at&sort_order=desc&limit=1&page=1", cookies=admin_cookie)
        assert resp.status_code == 200
        assert b"user3@example.com" in resp.content
        assert b"User Three" in resp.content
        assert b"user1@example.com" not in resp.content
        # Database ids are never shown as row numbers.
        assert f"<td>{u3.id}</td>".encode() not in resp.content

        # On page 2 with limit=1 (descending), the middle user gets row index 2.
        async with _client() as c:
            resp2 = await c.get("/admin/users/?sort_by=created_at&sort_order=desc&limit=1&page=2", cookies=admin_cookie)
        assert resp2.status_code == 200
        assert b"user2@example.com" in resp2.content
        assert b"user3@example.com" not in resp2.content
        assert b"limit=1" in resp2.content

    @pytest.mark.anyio
    async def test_user_list_shows_readable_join_date(self, setup_db, admin_cookie):
        user = await _create_test_user(email="dated@example.com")
        async with _client() as c:
            resp = await c.get("/admin/users/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert user.created_at.strftime("%b %d, %Y").encode() in resp.content
        assert user.created_at.strftime("%Y-%m-%d").encode() not in resp.content

    @pytest.mark.anyio
    async def test_user_detail_shows_readable_join_date(self, setup_db, admin_cookie):
        user = await _create_test_user(email="detail@example.com")
        async with _client() as c:
            resp = await c.get(f"/admin/users/{user.id}/", cookies=admin_cookie)
        assert resp.status_code == 200
        assert user.created_at.strftime("%b %d, %Y").encode() in resp.content
        assert user.created_at.strftime("%Y-%m-%d").encode() not in resp.content

    @pytest.mark.anyio
    async def test_user_list_requires_admin(self, setup_db):
        async with _client() as c:
            resp = await c.get("/admin/users/")
        assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_set_event_membership(self, setup_db, admin_cookie):
        from portal.database import create_event, get_session, list_memberships_for_event

        user = await _create_test_user()
        async with get_session() as s:
            event = await create_event(s, display_name="Test Event", slug="test-event")
        async with _client() as c:
            resp = await c.post(
                f"/admin/events/{event.id}/members/",
                data={"email": user.email, "role": "interpreter"},
                cookies=admin_cookie,
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            memberships = await list_memberships_for_event(s, event.id)
        assert len(memberships) == 1
        assert memberships[0].role == "interpreter"

    @pytest.mark.anyio
    async def test_toggle_user_active(self, setup_db, admin_cookie):
        from portal.database import get_session, get_user_by_id

        user = await _create_test_user()
        assert user.is_active is True
        async with _client() as c:
            resp = await c.post(
                f"/admin/users/{user.id}/toggle-active",
                cookies=admin_cookie,
                headers={"origin": "http://test"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            updated = await get_user_by_id(s, user.id)
        assert updated.is_active is False

    @pytest.mark.anyio
    async def test_delete_user(self, setup_db, admin_cookie):
        from portal.database import get_session, get_user_by_id

        user = await _create_test_user()
        async with _client() as c:
            resp = await c.post(
                f"/admin/users/{user.id}/delete",
                data={"confirm_email": user.email},
                cookies=admin_cookie,
                headers={"origin": "http://test"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        async with get_session() as s:
            deleted = await get_user_by_id(s, user.id)
        assert deleted is None


# ---------------------------------------------------------------------------
# Home page auth links
# ---------------------------------------------------------------------------


class TestHomePageAuthLinks:
    @pytest.mark.anyio
    async def test_home_shows_single_sign_in_when_logged_out(self, setup_db):
        async with _client() as c:
            resp = await c.get("/")
        assert resp.status_code == 200
        assert b'<a href="/login"' in resp.content
        assert b">Sign in</a>" in resp.content
        # One entry point: no separate Register button or "Login" label (#752).
        assert b"Register" not in resp.content
        assert b"Login" not in resp.content
        assert b"Sign In" not in resp.content

    @pytest.mark.anyio
    async def test_home_shows_account_when_logged_in(self, setup_db):
        user = await _create_test_user()
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        async with _client() as c:
            resp = await c.get("/", cookies={"user_token": token})
        assert resp.status_code == 200
        assert b"Logout" in resp.content
