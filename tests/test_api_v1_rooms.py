"""Tests for the api_v1 room sync endpoint (PUT /api/v1/events/{slug}/rooms/{id}).

Covers the partial-update and validation behaviour: omitted fields must leave
existing data alone, and invalid values must be rejected before they reach the
database.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-pass")

import pytest

ACCESS_TOKEN = "test-room-sync-token"


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


async def _seed():
    """Create an event whose owner holds a rooms:write token."""
    from portal.auth import hash_password
    from portal.database import create_event, create_user, get_session, set_event_membership
    from portal.models import DeveloperAccount, OAuthClient, OAuthToken

    async with get_session() as s:
        user = await create_user(
            s,
            email="owner@example.com",
            display_name="Owner",
            password_hash=hash_password("password123"),
        )
        event = await create_event(s, slug="synccon", display_name="SyncCon 2026")
        await set_event_membership(s, user_id=user.id, event_id=event.id, role="event_owner")

        dev_account = DeveloperAccount(user_id=user.id, status="approved")
        s.add(dev_account)
        await s.flush()
        client = OAuthClient(developer_account_id=dev_account.id, client_id="sync-client", name="Sync")
        s.add(client)
        await s.flush()
        s.add(
            OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=["rooms:write"],
                access_token_hash=hashlib.sha256(ACCESS_TOKEN.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
    return event


def _auth():
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


async def _languages_and_booths(room_id: int):
    from sqlalchemy import select

    from portal.database import get_session
    from portal.models import DBBooth, RoomTranslationLanguage

    async with get_session() as s:
        langs = (
            (await s.execute(select(RoomTranslationLanguage).where(RoomTranslationLanguage.room_id == room_id)))
            .scalars()
            .all()
        )
        booths = (await s.execute(select(DBBooth).where(DBBooth.room_id == room_id))).scalars().all()
    return sorted(rl.language_code for rl in langs), sorted(b.language_code for b in booths)


@pytest.mark.anyio
async def test_partial_update_keeps_languages_and_booths():
    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": ["fr", "de"]},
            headers=_auth(),
        )
        assert created.status_code == 200
        room_id = created.json()["room_id"]
        assert await _languages_and_booths(room_id) == (["de", "fr"], ["de", "fr"])

        # A payload without target_languages must not remove anything.
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall Renamed"},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert await _languages_and_booths(room_id) == (["de", "fr"], ["de", "fr"])


@pytest.mark.anyio
async def test_explicit_language_list_still_reconciles():
    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": ["fr", "de"]},
            headers=_auth(),
        )
        room_id = created.json()["room_id"]

        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": ["fr"]},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert await _languages_and_booths(room_id) == (["fr"], ["fr"])


@pytest.mark.anyio
async def test_empty_language_list_clears_languages():
    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": ["fr"]},
            headers=_auth(),
        )
        room_id = created.json()["room_id"]

        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall", "target_languages": []},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert await _languages_and_booths(room_id) == ([], [])


@pytest.mark.anyio
async def test_update_without_name_keeps_the_existing_name():
    from portal.database import get_session
    from portal.models import Room

    event = await _seed()

    async with _client() as c:
        created = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"name": "Main Hall"},
            headers=_auth(),
        )
        room_id = created.json()["room_id"]

        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json={"target_languages": ["fr"]},
            headers=_auth(),
        )

    assert resp.status_code == 200
    async with get_session() as s:
        room = await s.get(Room, room_id)
        assert room.display_name == "Main Hall"


@pytest.mark.anyio
async def test_create_without_name_is_rejected():
    event = await _seed()

    async with _client() as c:
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-new",
            json={"target_languages": ["fr"]},
            headers=_auth(),
        )

    assert resp.status_code == 422
    assert "name" in resp.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "x" * 201},
        {"name": "Main Hall", "description": "d" * 1001},
        {"name": "Main Hall", "target_languages": ["français"]},
        {"name": "Main Hall", "target_languages": ["x" * 40]},
        {"name": "Main Hall", "target_languages": ["zz"]},
    ],
)
async def test_invalid_values_are_rejected(payload):
    event = await _seed()

    async with _client() as c:
        resp = await c.put(
            f"/api/v1/events/{event.slug}/rooms/room-1",
            json=payload,
            headers=_auth(),
        )

    assert resp.status_code == 422
