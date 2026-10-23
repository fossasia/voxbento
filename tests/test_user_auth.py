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

    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


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
    async def test_duplicate_unverified_registration_queues_once(self, setup_db):
        from sqlalchemy import select

        from portal.database import create_auth_token, create_user, get_session
        from portal.models import AuthToken, EmailDelivery

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
        async with get_session() as session:
            tokens = list(
                (await session.execute(select(AuthToken).where(AuthToken.user_id == user.id).order_by(AuthToken.id)))
                .scalars()
                .all()
            )
        assert len(tokens) == 2
        assert tokens[0].used_at is None
        assert sum(token.used_at is None for token in tokens) == 2
        async with get_session() as session:
            deliveries = list((await session.execute(select(EmailDelivery))).scalars().all())
        assert len(deliveries) == 1
        assert deliveries[0].notification_key == "verification"

    @pytest.mark.anyio
    async def test_duplicate_registration_cannot_activate_preexisting_password(self, setup_db):
        from sqlalchemy import select

        from portal.database import create_user, get_session, get_user_by_email
        from portal.models import AuthToken

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
            async with get_session() as session:
                token = await session.scalar(
                    select(AuthToken).where(AuthToken.token_type == "verification").order_by(AuthToken.id.desc())
                )
            assert token is not None
            verification = await c.get(f"/auth/verify/{token.jti}", follow_redirects=False)
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
    async def test_duplicate_registration_preserves_old_token_and_queues_resend(self, setup_db):
        from sqlalchemy import select

        from portal.database import create_auth_token, create_user, get_session
        from portal.models import AuthToken, EmailDelivery

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
        assert tokens[1].used_at is None
        async with get_session() as session:
            deliveries = list((await session.execute(select(EmailDelivery))).scalars().all())
        assert len(deliveries) == 1

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


# ---------------------------------------------------------------------------
# Magic link authentication & cross-browser isolation
# ---------------------------------------------------------------------------


class TestMagicLinkAuthentication:
    @pytest.mark.anyio
    async def test_magic_link_request_returns_ok_without_poll_id_or_verifier_cookie(self, setup_db):
        user = await _create_test_user(email="magic-user@example.com")
        async with _client() as c:
            resp = await c.post("/api/auth/magic-link", json={"email": user.email})
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"status": "ok"}
        assert "poll_id" not in data
        assert "poll_verifier" not in c.cookies
        assert "user_token" not in c.cookies

    @pytest.mark.anyio
    async def test_magic_poll_endpoint_is_removed(self, setup_db):
        async with _client() as c:
            resp = await c.get("/api/auth/magic-poll")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_magic_link_opened_in_separate_session_does_not_sign_in_requester(self, setup_db):
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import AuthToken

        user = await _create_test_user(email="requester@example.com")

        # Session A: The browser / session that requests the magic link
        async with _client() as requester_client:
            req_res = await requester_client.post("/api/auth/magic-link", json={"email": user.email})
            assert req_res.status_code == 200
            assert req_res.json() == {"status": "ok"}
            assert "user_token" not in requester_client.cookies
            assert "poll_verifier" not in requester_client.cookies

            # Retrieve the magic link token created in the DB
            async with get_session() as s:
                tok = (await s.execute(select(AuthToken).where(AuthToken.user_id == user.id))).scalars().first()
                assert tok is not None
                magic_token = tok.jti

            # Session B: A completely separate browser / session that opens the magic link
            async with _client() as opener_client:
                open_res = await opener_client.get(f"/auth/magic/{magic_token}", follow_redirects=False)
                assert open_res.status_code == 303
                assert "/account?auth_sync=1" in open_res.headers.get("location", "")
                assert "user_token" in opener_client.cookies
                assert "auth_sync" in opener_client.cookies

                # Session B is signed in
                status_res_b = await opener_client.get("/api/auth/status")
                assert status_res_b.status_code == 200
                assert status_res_b.json()["authenticated"] is True
                assert status_res_b.json()["email"] == user.email

            # Session A (requester) must NOT be signed in
            assert "user_token" not in requester_client.cookies
            status_res_a = await requester_client.get("/api/auth/status")
            assert status_res_a.status_code == 200
            assert status_res_a.json()["authenticated"] is False

            # Session A cannot access protected routes
            account_res_a = await requester_client.get("/account", follow_redirects=False)
            assert account_res_a.status_code in (302, 303, 307)
            assert "/login" in account_res_a.headers.get("location", "")

    @pytest.mark.anyio
    async def test_magic_link_oauth_flow_renders_magic_success(self, setup_db, monkeypatch):
        from urllib.parse import parse_qs, urlparse

        import portal.routers.auth as auth_router

        captured_links: list[str] = []
        queue_notification = auth_router.queue_notification

        async def capture(session, key, recipient, context, **kwargs):
            if key == "magic_login":
                captured_links.append(context["magic_link"])
            return await queue_notification(session, key, recipient, context, **kwargs)

        monkeypatch.setattr(auth_router, "queue_notification", capture)

        user = await _create_test_user(email="oauth-user@example.com")
        next_oauth = "/oauth/authorize?client_id=test_client&event=test_event"

        async with _client() as requester_client:
            req_res = await requester_client.post(
                "/api/auth/magic-link", json={"email": user.email, "next_url": next_oauth}
            )
            assert req_res.status_code == 200

        assert len(captured_links) == 1
        magic_link = captured_links[0]

        # Verify the generated magic link retains the OAuth continuation target
        parsed_url = urlparse(magic_link)
        query_params = parse_qs(parsed_url.query)
        assert "next" in query_params
        assert query_params["next"][0] == next_oauth

        # Redeem the actual link that was generated and sent in the email
        redemption_target = parsed_url.path + (f"?{parsed_url.query}" if parsed_url.query else "")

        async with _client() as opener_client:
            res = await opener_client.get(redemption_target, follow_redirects=False)
            assert res.status_code == 200
            assert b"Successfully Signed In" in res.content
            assert b"voxbento_auth" in res.content
            assert "user_token" in opener_client.cookies

    @pytest.mark.anyio
    async def test_login_page_renders_notice_and_no_polling(self, setup_db):
        import re

        async with _client() as c:
            resp = await c.get("/login")
        assert resp.status_code == 200
        content = resp.text
        # Acceptance criteria checks:
        assert "/api/auth/magic-poll" not in content
        assert "startMagicPoll" not in content
        assert "voxbento_auth" in content
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", content))
        assert "Open it in this browser and this page continues on its own." in text
        assert "If you open it on another device or browser, you'll be signed in there instead." in text
        assert 'id="request-new-link"' in content

    @pytest.mark.anyio
    async def test_base_and_admin_templates_render_auth_sync_script_when_query_param_present(self, setup_db):
        user = await _create_test_user()
        token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
        async with _client() as c:
            # base.html (e.g. /account)
            resp_sync = await c.get("/account?auth_sync=1", cookies={"user_token": token})
            assert resp_sync.status_code == 200
            assert "auth-sync.js" in resp_sync.text
            # Invariant #5: no inline <script> block for auth sync
            assert "const authChannel = new BroadcastChannel" not in resp_sync.text

            resp_normal = await c.get("/account", cookies={"user_token": token})
            assert resp_normal.status_code == 200
            assert "auth-sync.js" not in resp_normal.text

            # admin/base.html (e.g. /workspace/)
            resp_admin_sync = await c.get("/workspace/?auth_sync=1", cookies={"user_token": token})
            assert resp_admin_sync.status_code == 200
            assert "auth-sync.js" in resp_admin_sync.text

            resp_admin_normal = await c.get("/workspace/", cookies={"user_token": token})
            assert resp_admin_normal.status_code == 200
            assert "auth-sync.js" not in resp_admin_normal.text

            # standalone templates (e.g. / and /local)
            # Unauthenticated public request with auth_sync=1 does NOT load auth-sync.js
            resp_home_unauth = await c.get("/?auth_sync=1")
            assert resp_home_unauth.status_code == 200
            assert "auth-sync.js" not in resp_home_unauth.text

            resp_local_unauth = await c.get("/local?auth_sync=1")
            assert resp_local_unauth.status_code == 200
            assert "auth-sync.js" not in resp_local_unauth.text

            # Authenticated request with auth_sync=1 loads auth-sync.js ES module
            resp_home_auth = await c.get("/?auth_sync=1", cookies={"user_token": token})
            assert resp_home_auth.status_code == 200
            assert "auth-sync.js" in resp_home_auth.text

            resp_local_auth = await c.get("/local?auth_sync=1", cookies={"user_token": token})
            assert resp_local_auth.status_code == 200
            assert "auth-sync.js" in resp_local_auth.text

    @pytest.mark.anyio
    async def test_auth_sync_renders_on_non_admin_routes_without_user_in_context(self, setup_db):
        from portal.database import create_event, get_session

        user = await _create_test_user(email="non-admin-sync@example.com")
        token = create_user_token(user_id=user.id, email=user.email, is_admin=False)

        async with get_session() as s:
            event = await create_event(s, slug="sync-route-event", display_name="Sync Route Event")

        async with _client() as c:
            # /listener/{slug} (listener-event.html omits user/current_user/is_admin in context)
            resp_listener_auth = await c.get(f"/listener/{event.slug}?auth_sync=1", cookies={"user_token": token})
            assert resp_listener_auth.status_code == 200
            assert "auth-sync.js" in resp_listener_auth.text

            # Unauthenticated request to listener route must not load auth-sync.js
            resp_listener_unauth = await c.get(f"/listener/{event.slug}?auth_sync=1")
            assert resp_listener_unauth.status_code == 200
            assert "auth-sync.js" not in resp_listener_unauth.text

            # /interpreter (interpreter_landing.html omits user/current_user/is_admin in context)
            resp_interp_auth = await c.get("/interpreter?auth_sync=1", cookies={"user_token": token})
            assert resp_interp_auth.status_code == 200
            assert "auth-sync.js" in resp_interp_auth.text

            # Ephemeral cookie trigger without query parameter also renders auth-sync.js
            resp_cookie_auth = await c.get(f"/listener/{event.slug}", cookies={"user_token": token, "auth_sync": "1"})
            assert resp_cookie_auth.status_code == 200
            assert "auth-sync.js" in resp_cookie_auth.text

    @pytest.mark.anyio
    async def test_magic_link_redirect_places_auth_sync_in_query_before_fragment(self, setup_db):
        import secrets
        from urllib.parse import quote

        from portal.database import create_auth_token, get_session

        user = await _create_test_user()
        test_cases = [
            ("/account#billing", "/account?auth_sync=1#billing"),
            ("/workspace/?tab=events#section", "/workspace/?tab=events&auth_sync=1#section"),
            ("/account#section?foo=bar", "/account?auth_sync=1#section?foo=bar"),
        ]

        for next_url, expected_redirect in test_cases:
            token_val = secrets.token_hex(32)
            async with get_session() as s:
                await create_auth_token(s, jti=token_val, user_id=user.id, token_type="magic_link")

            async with _client() as c:
                res = await c.get(f"/auth/magic/{token_val}?next={quote(next_url)}", follow_redirects=False)
                assert res.status_code == 303
                assert res.headers.get("location") == expected_redirect
