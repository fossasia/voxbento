"""Regression test for ``GET /api/v1/events/{event_slug}/rooms``.

The route built each room's payload with ``r.is_active``, but the ``Room``
model has no ``is_active`` column (nor an ``enabled`` one). Every request
therefore raised ``AttributeError: 'Room' object has no attribute 'is_active'``
and the global exception handler turned it into a 500 — so the OAuth rooms
listing could never return data to a third-party client.

This mirrors what a real integrator does: obtain a token with the
``events:read`` scope for an event that owns a room, then list the rooms.
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.models import (
    DeveloperAccount,
    Event,
    EventMembership,
    OAuthClient,
    OAuthToken,
    Room,
    User,
)

ACCESS_TOKEN = "test_rooms_access_token"


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _client():
    # raise_app_exceptions=False so an unhandled server error surfaces as the
    # 500 response an OAuth client actually receives, instead of being
    # re-raised into the test process.
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test")


@pytest.fixture
async def seeded_event_with_room(setup_db):
    from portal.database import get_session
    from portal.routers.oauth import hash_token

    async with get_session() as session:
        owner = User(email="owner@example.com", display_name="Owner")
        session.add(owner)
        await session.flush()

        developer = DeveloperAccount(user_id=owner.id)
        session.add(developer)
        await session.flush()

        client = OAuthClient(
            developer_account_id=developer.id,
            client_id="rooms_listing_client",
            name="Rooms Listing Client",
            is_confidential=True,
            redirect_uris=["http://localhost/callback"],
            scopes_requested=["events:read"],
        )
        session.add(client)

        event = Event(slug="pycon2026", display_name="PyCon 2026")
        session.add(event)
        await session.flush()

        session.add(EventMembership(user_id=owner.id, event_id=event.id, role="event_owner"))
        session.add(Room(event_id=event.id, display_name="Main Hall"))
        await session.flush()

        session.add(
            OAuthToken(
                client_id=client.id,
                user_id=owner.id,
                event_id=event.id,
                scopes=["events:read"],
                access_token_hash=hash_token(ACCESS_TOKEN),
                refresh_token_hash=hash_token(ACCESS_TOKEN + "-refresh"),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await session.flush()
        session.expunge_all()
        await session.commit()

    return {"event_slug": event.slug, "room_name": "Main Hall"}


@pytest.mark.anyio
async def test_list_rooms_returns_the_events_rooms(seeded_event_with_room):
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

    async with _client() as client:
        response = await client.get(
            f"/api/v1/events/{seeded_event_with_room['event_slug']}/rooms",
            headers=headers,
        )

    assert response.status_code == 200, response.text
    rooms = response.json()
    assert [room["display_name"] for room in rooms] == [seeded_event_with_room["room_name"]]

    # The payload must expose the room's real fields and must not reference
    # ``Room.is_active`` (the model has no such column).
    assert "is_active" not in rooms[0]
    assert rooms[0]["id"] is not None
    assert rooms[0]["created_at"]
