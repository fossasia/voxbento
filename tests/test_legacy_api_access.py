"""Regression tests for scoped authorization in the legacy booth API."""

from __future__ import annotations

import anyio
from fastapi.testclient import TestClient

from fastapi_app import app
from portal.auth import create_user_token
from portal.config import settings
from portal.database import create_event, create_room, create_user, get_session, set_event_membership
from portal.models import DBBooth

client = TestClient(app)


def _setup_database():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    anyio.run(init_db)
    return dispose


def test_user_cannot_create_or_list_booths_for_another_event(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event_a = await create_event(session, slug="owned-event", display_name="Owned")
                event_b = await create_event(session, slug="other-event", display_name="Other")
                room_b = await create_room(session, event_id=event_b.id, display_name="Other Room")
                user = await create_user(
                    session,
                    email="owner@example.com",
                    display_name="Owner",
                    password_hash="hash",
                )
                await set_event_membership(session, user_id=user.id, event_id=event_a.id, role="event_owner")
                await session.commit()
                return user.id, room_b.id

        user_id, room_b_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "")
        monkeypatch.setattr(settings, "debug", True)
        token = create_user_token(user_id=user_id, email="owner@example.com")
        headers = {"Authorization": f"Bearer {token}"}

        create_response = client.post(
            "/api/events/other-event/booths",
            json={"language_code": "en", "room_id": room_b_id},
            headers=headers,
        )
        assert create_response.status_code == 403

        list_response = client.get("/api/events/other-event/booths", headers=headers)
        assert list_response.status_code == 403
    finally:
        anyio.run(dispose)


def test_numeric_external_room_id_uses_explicit_external_route(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="numeric-event", display_name="Numeric")
                room = await create_room(session, event_id=event.id, display_name="External", eventyay_room_id="42")
                booth = DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English")
                session.add(booth)
                await session.commit()

        anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "master-token")
        monkeypatch.setattr(settings, "debug", False)
        response = client.delete(
            "/api/events/numeric-event/eventyay-rooms/42/booths/en",
            headers={"Authorization": "Bearer master-token"},
        )
        assert response.status_code == 204
    finally:
        anyio.run(dispose)
