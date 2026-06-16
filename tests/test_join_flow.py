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
            language_code="de",
        )
        payload = decode_token(token)
        uuid.UUID(payload["sub"])  # raises if not a valid UUID

    def test_different_calls_produce_different_subs(self):
        t1 = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="ev",
            language_code="en",
        )
        t2 = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="ev",
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
