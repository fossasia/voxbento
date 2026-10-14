"""Tests for the invite-token join flow (GET /join/{token}).

Covers:
- Happy path: valid token → JWT cookie + redirect
- Error paths: expired, already-used, invalid tokens
- JWT cookie contents (role, event_slug, language_code, booth_id)
- create_participant_token() in portal/auth.py
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portal.auth import create_participant_token, decode_token
from portal.database import (
    create_booth,
    create_event,
    create_invite_token,
    create_room,
    get_invite_token,
    redeem_invite_token,
)
from portal.models import Base, utc_now

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Yield an async session backed by an in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def sample_booth(db):
    """Create an event → room → booth and return (booth, event) tuple."""
    event = await create_event(db, slug="testcon2026", display_name="TestCon 2026")
    room = await create_room(db, event_id=event.id, display_name="Main Hall")
    booth = await create_booth(
        db,
        event_id=event.id,
        room_id=room.id,
        language_code="fr",
        language_name="French",
    )
    return booth, event


# ---------------------------------------------------------------------------
# create_participant_token() unit tests
# ---------------------------------------------------------------------------


class TestCreateParticipantToken:
    def test_creates_valid_jwt(self):
        token = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="pycon2026",
            room_id=1,
            language_code="en",
        )
        payload = decode_token(token)
        assert payload["booth_id"] == 1
        assert payload["role"] == "interpreter"
        assert payload["event_slug"] == "pycon2026"
        assert payload["language_code"] == "en"
        assert "sub" in payload
        assert "exp" in payload
        assert "iat" in payload

    def test_sub_is_uuid(self):
        import uuid

        token = create_participant_token(
            booth_id=1,
            role="room_coordinator",
            event_slug="ev",
            room_id=1,
            language_code="de",
        )
        payload = decode_token(token)
        uuid.UUID(payload["sub"])  # raises if not a valid UUID

    def test_different_calls_produce_different_subs(self):
        t1 = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="ev",
            room_id=1,
            language_code="en",
        )
        t2 = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="ev",
            room_id=1,
            language_code="en",
        )
        assert decode_token(t1)["sub"] != decode_token(t2)["sub"]

    @pytest.mark.parametrize(
        "role",
        [
            "interpreter",
            "room_coordinator",
            "event_owner",
            "super_admin",
        ],
    )
    def test_all_roles_accepted(self, role):
        token = create_participant_token(
            booth_id=42,
            role=role,
            event_slug="test",
            room_id=1,
            language_code="zh",
        )
        assert decode_token(token)["role"] == role


# ---------------------------------------------------------------------------
# Token redemption + join flow (database-level)
# ---------------------------------------------------------------------------


class TestTokenRedemption:
    @pytest.mark.anyio
    async def test_redeem_valid_token(self, db, sample_booth):
        booth, event = sample_booth
        tok = await create_invite_token(db, booth_id=booth.id, role="interpreter", label="Alice")
        redeemed = await redeem_invite_token(db, tok.token)
        assert redeemed is not None
        assert redeemed.is_used is True
        assert redeemed.used_at is not None

    @pytest.mark.anyio
    async def test_redeem_sets_used_at(self, db, sample_booth):
        booth, _ = sample_booth
        tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")

        redeemed = await redeem_invite_token(db, tok.token)
        assert redeemed.used_at is not None

    @pytest.mark.anyio
    async def test_redeem_already_used_raises(self, db, sample_booth):
        booth, _ = sample_booth
        tok = await create_invite_token(db, booth_id=booth.id, role="interpreter")
        await redeem_invite_token(db, tok.token)
        with pytest.raises(ValueError, match="already been used"):
            await redeem_invite_token(db, tok.token)

    @pytest.mark.anyio
    async def test_redeem_expired_token_raises(self, db, sample_booth):
        booth, _ = sample_booth
        past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        tok = await create_invite_token(
            db,
            booth_id=booth.id,
            role="interpreter",
            expires_at=past,
        )
        with pytest.raises(ValueError, match="expired"):
            await redeem_invite_token(db, tok.token)

    @pytest.mark.anyio
    async def test_redeem_nonexistent_returns_none(self, db):
        result = await redeem_invite_token(db, "a" * 64)
        assert result is None

    @pytest.mark.anyio
    async def test_redeemed_token_has_booth_and_event_loaded(self, db, sample_booth):
        booth, event = sample_booth
        tok = await create_invite_token(db, booth_id=booth.id, role="room_coordinator")
        redeemed = await redeem_invite_token(db, tok.token)
        assert redeemed.booth.event.slug == "testcon2026"
        assert redeemed.booth.language_code == "fr"

    @pytest.mark.anyio
    async def test_future_expiry_is_valid(self, db, sample_booth):
        booth, _ = sample_booth
        future = datetime.now(tz=timezone.utc) + timedelta(hours=24)
        tok = await create_invite_token(
            db,
            booth_id=booth.id,
            role="interpreter",
            expires_at=future,
        )
        redeemed = await redeem_invite_token(db, tok.token)
        assert redeemed is not None
        assert redeemed.is_used is True


# ---------------------------------------------------------------------------
# GET /join/{token} integration tests (FastAPI TestClient)
# ---------------------------------------------------------------------------


class TestJoinRoute:
    """Test the /join/{token} endpoint via the FastAPI test client.

    These tests need a real database. We configure portal.database to use
    an in-memory SQLite before importing the app.
    """

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        """Set up an in-memory database for the FastAPI app."""
        from portal.database import configure, dispose, init_db

        configure("sqlite+aiosqlite://")
        await init_db()
        yield
        await dispose()

    @pytest.fixture
    async def _seed(self):
        """Seed the database with an event, room, and booth."""
        from portal.database import (
            create_booth,
            create_event,
            create_invite_token,
            create_room,
            get_session,
        )

        async with get_session() as s:
            event = await create_event(s, slug="pycon2026", display_name="PyCon 2026")
            room = await create_room(s, event_id=event.id, display_name="Main Hall")
            booth = await create_booth(
                s,
                event_id=event.id,
                room_id=room.id,
                language_code="en",
                language_name="English",
            )
        return event, room, booth

    async def _make_token(self, booth_id, role="interpreter", **kwargs):
        from portal.database import create_invite_token, get_session

        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth_id, role=role, **kwargs)
        return tok.token

    @pytest.mark.anyio
    async def test_valid_token_redirects(self, _seed):
        event, _, booth = _seed
        token_str = await self._make_token(booth.id)

        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/join/{token_str}", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/interpreter"

    @pytest.mark.anyio
    async def test_valid_token_sets_cookie(self, _seed):
        event, _, booth = _seed
        token_str = await self._make_token(booth.id, role="room_coordinator")

        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/join/{token_str}", follow_redirects=False)

        cookie = resp.cookies.get("session_token")
        assert cookie is not None

        payload = decode_token(cookie)
        assert payload["role"] == "room_coordinator"
        assert payload["event_slug"] == "pycon2026"
        assert payload["language_code"] == "en"
        assert payload["booth_id"] == booth.id

    @pytest.mark.anyio
    async def test_invalid_token_returns_404(self, _seed):
        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get("/join/" + "a" * 64, follow_redirects=False)
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_expired_token_returns_403(self, _seed):
        _, _, booth = _seed
        past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        token_str = await self._make_token(booth.id, expires_at=past)

        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/join/{token_str}", follow_redirects=False)
        assert resp.status_code == 403
        assert "expired" in resp.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_used_token_returns_403(self, _seed):
        _, _, booth = _seed
        token_str = await self._make_token(booth.id)

        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            # First use — success
            resp1 = await client.get(f"/join/{token_str}", follow_redirects=False)
            assert resp1.status_code == 303

            # Second use — rejected
            resp2 = await client.get(f"/join/{token_str}", follow_redirects=False)
            assert resp2.status_code == 403
            assert "already been used" in resp2.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_token_with_different_roles(self, _seed):
        _, _, booth = _seed
        for role in ["interpreter", "room_coordinator", "event_owner", "super_admin"]:
            token_str = await self._make_token(booth.id, role=role)

            from httpx import ASGITransport, AsyncClient

            from fastapi_app import app

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
            ) as client:
                resp = await client.get(f"/join/{token_str}", follow_redirects=False)
            assert resp.status_code == 303
            cookie = resp.cookies.get("session_token")
            payload = decode_token(cookie)
            assert payload["role"] == role

    @pytest.mark.anyio
    async def test_token_with_future_expiry_works(self, _seed):
        _, _, booth = _seed
        future = datetime.now(tz=timezone.utc) + timedelta(hours=24)
        token_str = await self._make_token(booth.id, expires_at=future)

        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/join/{token_str}", follow_redirects=False)
        assert resp.status_code == 303

    @pytest.mark.anyio
    async def test_redirect_routes_to_lobby(self, _seed):
        """Ensure redirect routes to the interpreter lobby."""
        _, _, booth = _seed
        token_str = await self._make_token(booth.id)

        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.get(f"/join/{token_str}", follow_redirects=False)
        loc = resp.headers["location"]
        assert loc == "/interpreter"

    @pytest.mark.anyio
    async def test_multiple_booths_redirect_correctly(self):
        """Tokens for different booths redirect to the lobby."""
        from portal.database import (
            create_booth,
            create_event,
            create_invite_token,
            create_room,
            get_session,
        )

        async with get_session() as s:
            event = await create_event(s, slug="multi-event", display_name="Multi")
            room = await create_room(s, event_id=event.id, display_name="Room")
            booth_en = await create_booth(
                s,
                event_id=event.id,
                room_id=room.id,
                language_code="en",
                language_name="English",
            )
            booth_fr = await create_booth(
                s,
                event_id=event.id,
                room_id=room.id,
                language_code="fr",
                language_name="French",
            )

        tok_en = await self._make_token(booth_en.id)
        tok_fr = await self._make_token(booth_fr.id)

        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp_en = await client.get(f"/join/{tok_en}", follow_redirects=False)
            assert resp_en.headers["location"] == "/interpreter"

            resp_fr = await client.get(f"/join/{tok_fr}", follow_redirects=False)
            assert resp_fr.headers["location"] == "/interpreter"


# ---------------------------------------------------------------------------
# Issue B regression tests: Combined session and invite precedence
# ---------------------------------------------------------------------------


class TestCombinedSessionAndInvitePrecedence:
    """Verify that logged-in users with interpreter invites retain proper access

    and that the session precedence and role resolution follow documented rules.
    """

    @pytest.fixture(autouse=True)
    async def setup_db(self):
        """Set up an in-memory database for the FastAPI app."""
        from portal.database import configure, dispose, init_db

        configure("sqlite+aiosqlite://")
        await init_db()
        yield
        await dispose()

    @pytest.fixture
    async def seed_data(self):
        """Seed the database with test events, rooms, booths, and users."""
        from portal.database import (
            create_booth,
            create_event,
            create_room,
            create_user,
            get_session,
            set_booth_membership,
        )

        async with get_session() as s:
            event = await create_event(s, slug="pycon2026", display_name="PyCon 2026")
            room = await create_room(s, event_id=event.id, display_name="Main Hall")
            booth = await create_booth(
                s,
                event_id=event.id,
                room_id=room.id,
                language_code="en",
                language_name="English",
            )
            booth2 = await create_booth(
                s,
                event_id=event.id,
                room_id=room.id,
                language_code="de",
                language_name="German",
            )
            other_event = await create_event(s, slug="othercon", display_name="OtherCon")
            other_room = await create_room(s, event_id=other_event.id, display_name="Room 1")
            other_booth = await create_booth(
                s,
                event_id=other_event.id,
                room_id=other_room.id,
                language_code="en",
                language_name="English",
            )
            # Memberless verified active user
            user = await create_user(
                s,
                email="memberless@test.com",
                password_hash="fakehash",
                display_name="Memberless User",
                email_verified=True,
                is_active=True,
                is_admin=False,
            )
            # Admin user
            admin = await create_user(
                s,
                email="admin@test.com",
                password_hash="fakehash",
                display_name="Admin User",
                email_verified=True,
                is_active=True,
                is_admin=True,
            )
            # Member user with DB booth membership
            member_user = await create_user(
                s,
                email="member@test.com",
                password_hash="fakehash",
                display_name="Booth Member",
                email_verified=True,
                is_active=True,
                is_admin=False,
            )
            await set_booth_membership(
                s,
                booth_id=booth.id,
                user_id=member_user.id,
                role="room_coordinator",
            )

        return {
            "event": event,
            "room": room,
            "booth": booth,
            "booth2": booth2,
            "other_event": other_event,
            "other_room": other_room,
            "other_booth": other_booth,
            "user": user,
            "admin": admin,
            "member_user": member_user,
        }

    @pytest.mark.anyio
    async def test_logged_in_memberless_user_redemption_grants_booth_access(self, seed_data):
        """1. Logged-in memberless user redeems a fresh invite and gains the invited booth access."""
        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app
        from portal.auth import create_user_token
        from portal.database import create_invite_token, get_session

        user = seed_data["user"]
        booth = seed_data["booth"]
        user_jwt = create_user_token(user_id=user.id, email=user.email, is_admin=False)

        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth.id, role="interpreter")
        token_str = tok.token

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Redeem while logged in
            resp = await client.get(
                f"/join/{token_str}",
                cookies={"user_token": user_jwt},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert resp.headers["location"] == "/interpreter"
            session_cookie = resp.cookies.get("session_token")
            assert session_cookie is not None

            # Access booth with BOTH user_token and session_token cookies
            booth_resp = await client.get(
                f"/interpreter/{seed_data['event'].slug}/{booth.room_id}/{booth.language_code}",
                cookies={"user_token": user_jwt, "session_token": session_cookie},
            )
            assert booth_resp.status_code == 200
            from portal.booth_identity import make_booth_id

            assert make_booth_id(seed_data["event"].slug, booth.room_id, booth.language_code) in booth_resp.text

    @pytest.mark.anyio
    async def test_combined_session_lobby_lists_invited_booth(self, seed_data):
        """2. The combined-session lobby lists the invited booth."""
        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app
        from portal.auth import create_participant_token, create_user_token

        user = seed_data["user"]
        booth = seed_data["booth"]
        user_jwt = create_user_token(user_id=user.id, email=user.email, is_admin=False)
        invite_jwt = create_participant_token(
            booth_id=booth.id,
            role="interpreter",
            event_slug=seed_data["event"].slug,
            room_id=booth.room_id,
            language_code=booth.language_code,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                "/interpreter",
                cookies={"user_token": user_jwt, "session_token": invite_jwt},
            )
            assert resp.status_code == 200
            assert "/interpreter/pycon2026/1/en" in resp.text
            assert "English" in resp.text
            assert "PyCon 2026" in resp.text

    @pytest.mark.anyio
    async def test_reusing_consumed_invite_is_rejected(self, seed_data):
        """3. Reusing a consumed invitation returns 403."""
        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app
        from portal.auth import create_user_token
        from portal.database import create_invite_token, get_session

        user = seed_data["user"]
        booth = seed_data["booth"]
        user_jwt = create_user_token(user_id=user.id, email=user.email, is_admin=False)

        async with get_session() as s:
            tok = await create_invite_token(s, booth_id=booth.id, role="interpreter")
            token_str = tok.token

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp1 = await client.get(
                f"/join/{token_str}",
                cookies={"user_token": user_jwt},
                follow_redirects=False,
            )
            assert resp1.status_code == 303

            # Verify token is burned in database
            async with get_session() as s:
                tok_after = await get_invite_token(s, token_str)
                assert tok_after.is_used is True
                assert tok_after.used_at is not None

            # Second redemption must return 403
            resp2 = await client.get(
                f"/join/{token_str}",
                cookies={"user_token": user_jwt},
                follow_redirects=False,
            )
            assert resp2.status_code == 403
            assert "already been used" in resp2.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_administrator_higher_privilege_preserved_with_both_cookies(self, seed_data):
        """4. An administrator's higher privilege is preserved when both cookies are present."""
        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app
        from portal.auth import create_participant_token, create_user_token, decode_token, resolve_booth_role

        admin = seed_data["admin"]
        booth = seed_data["booth"]
        admin_jwt = create_user_token(user_id=admin.id, email=admin.email, is_admin=True)
        invite_jwt = create_participant_token(
            booth_id=booth.id,
            role="interpreter",
            event_slug=seed_data["event"].slug,
            room_id=booth.room_id,
            language_code=booth.language_code,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/interpreter/{seed_data['event'].slug}/{booth.room_id}/{booth.language_code}",
                cookies={"user_token": admin_jwt, "session_token": invite_jwt},
            )
            assert resp.status_code == 200

            sessions = {
                "user_token": decode_token(admin_jwt),
                "session_token": decode_token(invite_jwt),
            }
            from portal.booth_identity import make_booth_id

            booth_id = make_booth_id(seed_data["event"].slug, booth.room_id, booth.language_code)
            resolved = await resolve_booth_role(list(sessions.values()), booth_id)
            assert resolved == "super_admin"

    @pytest.mark.anyio
    async def test_invite_cannot_grant_access_to_unrelated_booth_or_event(self, seed_data):
        """5. A lower-privilege invite cannot grant access to another event or unrelated booth."""
        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app
        from portal.auth import create_participant_token, create_user_token, decode_token, resolve_booth_role
        from portal.booth_identity import make_booth_id

        user = seed_data["user"]
        booth = seed_data["booth"]
        booth2 = seed_data["booth2"]
        other_booth = seed_data["other_booth"]
        user_jwt = create_user_token(user_id=user.id, email=user.email, is_admin=False)
        invite_jwt = create_participant_token(
            booth_id=booth.id,
            role="interpreter",
            event_slug=seed_data["event"].slug,
            room_id=booth.room_id,
            language_code=booth.language_code,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Unrelated booth in same event
            resp_diff_booth = await client.get(
                f"/interpreter/{seed_data['event'].slug}/{booth2.room_id}/{booth2.language_code}",
                cookies={"user_token": user_jwt, "session_token": invite_jwt},
            )
            assert resp_diff_booth.status_code == 403

            # Unrelated event
            resp_diff_event = await client.get(
                f"/interpreter/{seed_data['other_event'].slug}/{other_booth.room_id}/{other_booth.language_code}",
                cookies={"user_token": user_jwt, "session_token": invite_jwt},
            )
            assert resp_diff_event.status_code == 403

            sessions = {
                "user_token": decode_token(user_jwt),
                "session_token": decode_token(invite_jwt),
            }
            booth2_id = make_booth_id(seed_data["event"].slug, booth2.room_id, booth2.language_code)
            other_booth_id = make_booth_id(
                seed_data["other_event"].slug, other_booth.room_id, other_booth.language_code
            )
            assert await resolve_booth_role(list(sessions.values()), booth2_id) is None
            assert await resolve_booth_role(list(sessions.values()), other_booth_id) is None

    @pytest.mark.anyio
    async def test_combined_session_preserves_user_identity_and_display_name(self, seed_data):
        """6. Confirm user display_name and identity are preserved and not replaced by invite UUID."""
        from httpx import ASGITransport, AsyncClient

        from fastapi_app import app
        from portal.auth import create_participant_token, create_user_token

        user = seed_data["user"]
        booth = seed_data["booth"]
        user_jwt = create_user_token(
            user_id=user.id,
            email=user.email,
            display_name="Ada Lovelace",
            is_admin=False,
        )
        invite_jwt = create_participant_token(
            booth_id=booth.id,
            role="interpreter",
            event_slug=seed_data["event"].slug,
            room_id=booth.room_id,
            language_code=booth.language_code,
        )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/interpreter/{seed_data['event'].slug}/{booth.room_id}/{booth.language_code}",
                cookies={"user_token": user_jwt, "session_token": invite_jwt},
            )
            assert resp.status_code == 200
            assert "data-display-name='Ada Lovelace'" in resp.text
            assert "data-granted-role='interpreter'" in resp.text

    @pytest.mark.anyio
    async def test_malformed_and_two_part_booth_ids_rejected(self, seed_data):
        """Malformed, two-part, or prefix-spoofed booth IDs cannot match a scoped invite."""
        from portal.auth import create_participant_token, decode_token, resolve_booth_role
        from portal.booth_identity import make_booth_id

        booth = seed_data["booth"]
        invite_jwt = create_participant_token(
            booth_id=booth.id,
            role="interpreter",
            event_slug=seed_data["event"].slug,
            room_id=booth.room_id,
            language_code=booth.language_code,
        )
        payload = decode_token(invite_jwt)

        # Valid 3-part booth ID matching scope grants role
        valid_id = make_booth_id(seed_data["event"].slug, booth.room_id, booth.language_code)
        assert await resolve_booth_role([payload], valid_id) == "interpreter"

        # Legacy 2-part booth ID missing room must NOT match
        assert await resolve_booth_role([payload], "pycon2026-en") is None

        # Prefix spoofing attack must NOT match
        assert await resolve_booth_role([payload], f"evil-{valid_id}") is None

        # Cross-room must NOT match (real booth ID of the other event's room)
        cross_room_id = make_booth_id(seed_data["event"].slug, seed_data["other_room"].id, booth.language_code)
        assert await resolve_booth_role([payload], cross_room_id) is None

        # Cross-language must NOT match (real booth ID of the German booth)
        booth2 = seed_data["booth2"]
        cross_lang_id = make_booth_id(seed_data["event"].slug, booth2.room_id, booth2.language_code)
        assert await resolve_booth_role([payload], cross_lang_id) is None

        # Cross-event must NOT match (real booth ID of the other event's booth)
        other_booth = seed_data["other_booth"]
        cross_event_id = make_booth_id(seed_data["other_event"].slug, other_booth.room_id, other_booth.language_code)
        assert await resolve_booth_role([payload], cross_event_id) is None

    @pytest.mark.anyio
    async def test_db_membership_precedence_consistent_across_rest_and_websocket(self, seed_data):
        """User with higher DB membership and lower interpreter invite gets consistent role."""
        from unittest.mock import MagicMock

        from portal.auth import (
            create_participant_token,
            create_user_token,
            decode_token,
            resolve_booth_role,
            resolve_ws_auth,
        )
        from portal.database import get_session
        from portal.models import RoomMembership

        user = seed_data["user"]
        booth = seed_data["booth"]

        # Add room_coordinator DB membership for this room
        async with get_session() as s:
            s.add(RoomMembership(user_id=user.id, room_id=booth.room_id, role="room_coordinator"))
            await s.commit()

        user_jwt = create_user_token(user_id=user.id, email=user.email, is_admin=False)
        invite_jwt = create_participant_token(
            booth_id=booth.id,
            role="interpreter",
            event_slug=seed_data["event"].slug,
            room_id=booth.room_id,
            language_code=booth.language_code,
        )

        sessions = [decode_token(user_jwt), decode_token(invite_jwt)]

        from portal.booth_identity import make_booth_id

        booth_id = make_booth_id(seed_data["event"].slug, booth.room_id, booth.language_code)

        # 1. REST role evaluation: resolve_booth_role yields room_coordinator
        rest_role = await resolve_booth_role(sessions, booth_id)
        assert rest_role == "room_coordinator"

        # 2. WebSocket role evaluation: resolve_ws_auth selects the winning session
        mock_ws = MagicMock()
        mock_ws.query_params = {}
        mock_ws.cookies = {"user_token": user_jwt, "session_token": invite_jwt}
        mock_ws.headers = {}
        ws_payload = await resolve_ws_auth(mock_ws, booth_id)
        ws_role = await resolve_booth_role(ws_payload, booth_id)
        assert ws_role == "room_coordinator"

        # 3. Consistency: WebSocket and REST strictly agree on room_coordinator
        assert rest_role == ws_role == "room_coordinator"
