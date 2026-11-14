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
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.auth import create_user_token, decode_token, generate_admin_csrf_token, hash_password
from portal.booth_identity import validate_booth_language_code
from portal.booth_state import BoothRegistry
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    create_user,
    dispose,
    get_booth_by_id,
    get_session,
    get_user_by_email,
    init_db,
)
from portal.models import DBBooth, DeveloperAccount, EventMembership, OAuthClient, OAuthToken
from portal.routers.admin import _create_booth_unless_duplicate


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def seed_data():
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
    async def test_event_booth_state_rejects_floor(self, seed_data):
        """GET /events/{event_slug}/booths/floor/state returns 400."""
        event, room, token = seed_data

        async with _client() as c:
            resp = await c.get(
                f"/api/events/{event.slug}/booths/floor/state?room_id={room.id}",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 400
        assert "floor" in resp.json()["detail"]

    @pytest.mark.anyio
    async def test_validator_rejects_floor(self):
        """The shared helper is the single place the rule lives."""
        with pytest.raises(ValueError):
            validate_booth_language_code("floor")
        assert validate_booth_language_code("de") == "de"


class TestBoothLanguageCodeModel:
    @pytest.mark.anyio
    async def test_model_rejects_floor(self):
        """The ORM validator is the backstop: every DBBooth write passes through it."""
        with pytest.raises(ValueError):
            DBBooth(event_id=1, room_id=1, language_code="floor", language_name="Floor")

        assert DBBooth(event_id=1, room_id=1, language_code="de", language_name="German").language_code == "de"


class TestBoothLanguageCodeRegistry:
    @pytest.mark.anyio
    async def test_registry_rejects_floor_without_provisioning(self):
        """The legacy registry must raise before it registers or provisions anything."""
        registry = BoothRegistry()

        with pytest.raises(ValueError):
            await registry.create_booth(event_slug="some-event", language_code="floor", language="Floor", room_id=1)

        assert await registry.list_booths_for_event("some-event") == []

        created = await registry.create_booth(event_slug="some-event", language_code="de", language="German", room_id=1)
        assert created["language_code"] == "de"

    @pytest.mark.anyio
    async def test_registry_get_or_create_rejects_floor(self):
        """Lazy creation on snapshot or join must also reject floor."""
        registry = BoothRegistry()

        with pytest.raises(ValueError):
            await registry.snapshot("some-event-1-floor", "Floor", "some/path")

        assert await registry.list_booths_for_event("some-event") == []


class TestBoothLanguageCodeAdmin:
    @pytest.mark.anyio
    async def test_admin_helper_rejects_floor(self, seed_data):
        """The admin creation helper is shared by the setup wizard and the booth form."""
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

    @pytest.mark.anyio
    async def test_admin_create_booth_redirects_with_invalid_language_error(self, seed_data):
        """Admin form submission of floor redirects with invalid_language_code error."""
        event, room, _token = seed_data
        async with get_session() as session:
            user = await get_user_by_email(session, "floor@example.com")
            user_id = user.id

        user_token = create_user_token(user_id=user_id, email="floor@example.com", is_admin=False)
        session_id = decode_token(user_token)["jti"]
        csrf_token = generate_admin_csrf_token(session_id)
        cookies = {"user_token": user_token, "admin_csrf": csrf_token}

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{event.id}/rooms/{room.id}/booths/",
                data={"language_code": "floor", "language_name": "Floor", "csrf_token": csrf_token},
                cookies=cookies,
                headers={"X-CSRF-Token": csrf_token},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert "error=invalid_language_code" in resp.headers["location"]

            page = await c.get(resp.headers["location"], cookies=cookies)
            assert page.status_code == 200
            assert b"The reserved code" in page.content
            assert b"floor" in page.content

    @pytest.mark.anyio
    async def test_admin_edit_booth_rejects_floor_and_preserves_language(self, seed_data):
        """Admin booth edit rejects 'floor' with 400 and keeps stored language unchanged."""
        event, room, _token = seed_data
        async with get_session() as session:
            user = await get_user_by_email(session, "floor@example.com")
            user_id = user.id
            booth = await create_booth(
                session,
                event_id=event.id,
                room_id=room.id,
                language_code="fr",
                language_name="French",
            )
            booth_id = booth.id

        user_token = create_user_token(user_id=user_id, email="floor@example.com", is_admin=False)
        session_id = decode_token(user_token)["jti"]
        csrf_token = generate_admin_csrf_token(session_id)
        cookies = {"user_token": user_token, "admin_csrf": csrf_token}

        async with _client() as c:
            resp = await c.post(
                f"/workspace/events/{event.id}/rooms/{room.id}/booths/{booth_id}/edit",
                data={"language_code": "floor", "language_name": "Floor", "csrf_token": csrf_token},
                cookies=cookies,
                headers={"X-CSRF-Token": csrf_token},
                follow_redirects=False,
            )
            assert resp.status_code == 400
            assert "floor" in resp.json()["detail"]

            async with get_session() as session:
                reloaded = await get_booth_by_id(session, booth_id)
                assert reloaded is not None
                assert reloaded.language_code == "fr"
                assert reloaded.language_name == "French"

            resp_valid = await c.post(
                f"/workspace/events/{event.id}/rooms/{room.id}/booths/{booth_id}/edit",
                data={"language_code": "de", "language_name": "German", "csrf_token": csrf_token},
                cookies=cookies,
                headers={"X-CSRF-Token": csrf_token},
                follow_redirects=False,
            )
            assert resp_valid.status_code == 303

            async with get_session() as session:
                reloaded = await get_booth_by_id(session, booth_id)
                assert reloaded is not None
                assert reloaded.language_code == "de"
                assert reloaded.language_name == "German"
