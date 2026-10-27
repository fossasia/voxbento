"""The reserved `floor` marker cannot be used as a real booth language (#1046).

`validate_language_code` accepts `floor` so stream paths can name the room floor.
Every path that writes a real booth must therefore use the stricter validator, and
these tests pin each one: the ORM, the legacy in-memory registry, the admin helper
and the API v1 endpoint.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def seed_data():
    from portal.auth import hash_password
    from portal.database import create_event, create_room, create_user, get_session
    from portal.models import DeveloperAccount, EventMembership, OAuthClient, OAuthToken

    async with get_session() as s:
        user = await create_user(s, email="floor@example.com", display_name="Floor", password_hash=hash_password("pw"))
        event = await create_event(s, slug="floor-event", display_name="Floor Event")
        room = await create_room(s, event_id=event.id, display_name="Room")
        s.add(EventMembership(user_id=user.id, event_id=event.id, role="event_owner"))

        developer = DeveloperAccount(user_id=user.id)
        s.add(developer)
        await s.flush()

        client = OAuthClient(
            developer_account_id=developer.id,
            name="App",
            client_id="client-floor",
            client_secret_hash="hash",
        )
        s.add(client)
        await s.flush()

        token_str = "secret_token_floor"
        s.add(
            OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                access_token_hash=hashlib.sha256(token_str.encode()).hexdigest(),
                scopes=["booths:read", "booths:write", "rooms:read"],
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
        )
        await s.commit()

    return event, room, token_str


class TestBoothLanguageCodeApi:
    @pytest.mark.anyio
    async def test_create_booth_rejects_floor(self, seed_data):
        """`floor` marks the room floor captions, so it cannot name a real booth."""
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth

        event, room, token = seed_data

        async with _client() as c:
            resp = await c.post(
                f"/api/v1/events/{event.slug}/rooms/{room.id}/booths/floor",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 400
        assert "floor" in resp.json()["detail"]

        # The rejected request must not leave a row behind.
        async with get_session() as session:
            row = await session.scalar(
                select(DBBooth).where(DBBooth.room_id == room.id, DBBooth.language_code == "floor")
            )
            assert row is None

    @pytest.mark.anyio
    async def test_create_booth_still_accepts_a_real_code(self, seed_data):
        """A normal two-letter code keeps working, so the guard is not over-broad."""
        event, room, token = seed_data

        async with _client() as c:
            resp = await c.post(
                f"/api/v1/events/{event.slug}/rooms/{room.id}/booths/de",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 200
        assert resp.json()["whip_path"].endswith("/de")

    @pytest.mark.anyio
    async def test_validator_rejects_floor(self):
        """The shared helper is the single place the rule lives."""
        from portal.booth_identity import validate_booth_language_code

        with pytest.raises(ValueError):
            validate_booth_language_code("floor")
        assert validate_booth_language_code("de") == "de"


class TestBoothLanguageCodeModel:
    @pytest.mark.anyio
    async def test_model_rejects_floor(self):
        """The ORM validator is the backstop: every DBBooth write passes through it."""
        from portal.models import DBBooth

        with pytest.raises(ValueError):
            DBBooth(event_id=1, room_id=1, language_code="floor", language_name="Floor")

        assert DBBooth(event_id=1, room_id=1, language_code="de", language_name="German").language_code == "de"


class TestBoothLanguageCodeRegistry:
    @pytest.mark.anyio
    async def test_registry_rejects_floor_without_provisioning(self):
        """The legacy registry must raise before it registers or provisions anything."""
        from portal.booth_state import BoothRegistry

        registry = BoothRegistry()

        with pytest.raises(ValueError):
            await registry.create_booth(event_slug="some-event", language_code="floor", language="Floor", room_id=1)

        assert registry._booths == {}

        created = await registry.create_booth(event_slug="some-event", language_code="de", language="German", room_id=1)
        assert created["language_code"] == "de"


class TestBoothLanguageCodeAdmin:
    @pytest.mark.anyio
    async def test_admin_helper_rejects_floor(self, seed_data):
        """The admin creation helper is shared by the setup wizard and the booth form."""
        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth
        from portal.routers.admin import _create_booth_unless_duplicate

        event, room, _token = seed_data

        with pytest.raises(ValueError):
            await _create_booth_unless_duplicate(
                event_id=event.id, room_id=room.id, language_code="floor", language_name="Floor"
            )

        async with get_session() as session:
            row = await session.scalar(
                select(DBBooth).where(DBBooth.room_id == room.id, DBBooth.language_code == "floor")
            )
            assert row is None
