"""Regression tests for scoped authorization in the legacy booth API."""

from __future__ import annotations

from datetime import datetime, timezone

import anyio
import pytest
from fastapi.testclient import TestClient

from fastapi_app import app
from portal.auth import create_participant_token, create_user_token
from portal.config import settings
from portal.database import create_api_key, create_event, create_room, create_user, get_session, set_event_membership
from portal.models import DBBooth
from portal.utils import _require_access

client = TestClient(app)


def _request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""})


def test_missing_booth_token_allows_development_access(monkeypatch):
    monkeypatch.setattr(settings, "booth_access_token", "")
    monkeypatch.setattr(settings, "debug", True)

    _require_access(_request(), None)


def test_missing_booth_token_rejects_production_access(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "booth_access_token", "")
    monkeypatch.setattr(settings, "debug", False)

    with pytest.raises(HTTPException, match="BOOTH_ACCESS_TOKEN"):
        _require_access(_request(), None)


def test_configured_booth_token_allows_legacy_access(monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials

    monkeypatch.setattr(settings, "booth_access_token", "test-booth-token")
    monkeypatch.setattr(settings, "debug", False)

    _require_access(
        _request(),
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="test-booth-token"),
    )


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


def test_room_coordinator_cannot_manage_booths(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="coordinator-event", display_name="Coordinator")
                room = await create_room(session, event_id=event.id, display_name="Main")
                user = await create_user(
                    session,
                    email="coordinator@example.com",
                    display_name="Coordinator",
                    password_hash="hash",
                )
                from portal.database import set_room_membership

                await set_room_membership(session, user_id=user.id, room_id=room.id, role="room_coordinator")
                await session.commit()
                return user.id, room.id

        user_id, room_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
        monkeypatch.setattr(settings, "debug", False)
        token = create_user_token(user_id=user_id, email="coordinator@example.com")
        response = client.post(
            "/api/events/coordinator-event/booths",
            json={"language_code": "en", "room_id": room_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 403, response.text
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


def test_event_api_key_can_delete_only_its_event_booth(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="key-delete", display_name="Key Delete")
                other = await create_event(session, slug="other-key-delete", display_name="Other")
                room = await create_room(session, event_id=event.id, display_name="Main", eventyay_room_id="42")
                other_room = await create_room(session, event_id=other.id, display_name="Other", eventyay_room_id="42")
                session.add_all(
                    [
                        DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English"),
                        DBBooth(event_id=other.id, room_id=other_room.id, language_code="en", language_name="English"),
                    ]
                )
                _, raw_key = await create_api_key(session, event.id, name="Delete key")
                await session.commit()
                return raw_key

        raw_key = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
        monkeypatch.setattr(settings, "debug", False)
        headers = {"Authorization": f"Bearer {raw_key}"}

        allowed = client.delete("/api/events/key-delete/eventyay-rooms/42/booths/en", headers=headers)
        assert allowed.status_code == 204, allowed.text

        missing = client.delete("/api/events/other-key-delete/eventyay-rooms/42/booths/en")
        assert missing.status_code == 403, missing.text

        invalid = client.delete(
            "/api/events/other-key-delete/eventyay-rooms/42/booths/en",
            headers={"Authorization": "Bearer invalid-api-key"},
        )
        assert invalid.status_code == 403, invalid.text

        denied = client.delete("/api/events/other-key-delete/eventyay-rooms/42/booths/en", headers=headers)
        assert denied.status_code == 403, denied.text
    finally:
        anyio.run(dispose)


def test_invalid_bearer_cannot_fall_back_to_participant_cookie(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="cookie-scope", display_name="Cookie Scope")
                room = await create_room(session, event_id=event.id, display_name="Main")
                session.add(DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English"))
                await session.commit()
                return room.id

        room_id = anyio.run(seed)
        participant = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="cookie-scope",
            room_id=room_id,
            language_code="en",
        )
        monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
        monkeypatch.setattr(settings, "debug", False)
        path = f"/api/events/cookie-scope/booths/en/state?room_id={room_id}"
        cookie = {"session_token": participant}

        allowed = client.get(path, cookies=cookie)
        assert allowed.status_code == 200, allowed.text

        unauthorized_user = create_user_token(user_id=999, email="not-a-member@example.com")
        mixed_cookies = {**cookie, "user_token": unauthorized_user}
        participant_fallback = client.get(path, cookies=mixed_cookies)
        assert participant_fallback.status_code == 200, participant_fallback.text

        other_room_participant = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="cookie-scope",
            room_id=room_id + 1,
            language_code="en",
        )
        wrong_room_cookies = {"session_token": other_room_participant, "user_token": unauthorized_user}
        wrong_room = client.get(path, cookies=wrong_room_cookies)
        assert wrong_room.status_code == 403, wrong_room.text

        rejected = client.get(path, cookies=cookie, headers={"Authorization": "Bearer malformed"})
        assert rejected.status_code == 403, rejected.text

        malformed = client.get(path, cookies=cookie, headers={"Authorization": "Basic not-a-bearer"})
        assert malformed.status_code == 403, malformed.text
    finally:
        anyio.run(dispose)


def test_auth_token_flow_authorizes_legacy_event_api(monkeypatch):
    dispose = _setup_database()
    try:
        monkeypatch.setattr(settings, "booth_access_token", "a" * 32)
        monkeypatch.setattr(settings, "debug", False)
        response = client.post("/api/auth/token", json={"token": "a" * 32})
        assert response.status_code == 200, response.text
        jwt_token = response.json()["access_token"]

        list_response = client.get(
            "/api/events/token-flow-event/booths", headers={"Authorization": f"Bearer {jwt_token}"}
        )
        assert list_response.status_code == 200, list_response.text
        assert list_response.json()["booths"] == []
    finally:
        anyio.run(dispose)


def test_soft_deleted_event_cannot_create_or_list_booths(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="deleted-event", display_name="Deleted")
                event.deleted_at = datetime.now(timezone.utc)
                await session.commit()

        anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "a" * 32)
        monkeypatch.setattr(settings, "debug", False)
        headers = {"Authorization": "Bearer " + "a" * 32}
        create_response = client.post(
            "/api/events/deleted-event/booths", json={"language_code": "en"}, headers=headers
        )
        assert create_response.status_code == 404
        list_response = client.get("/api/events/deleted-event/booths", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()["booths"] == []
    finally:
        anyio.run(dispose)


def test_global_booth_token_does_not_inherit_participant_cookie_scope(monkeypatch):
    dispose = _setup_database()
    try:
        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="global-cookie", display_name="Global Cookie")
                room = await create_room(session, event_id=event.id, display_name="Main")
                session.add(DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English"))
                await session.commit()
                return room.id

        room_id = anyio.run(seed)
        participant = create_participant_token(
            booth_id=1,
            role="interpreter",
            event_slug="another-event",
            room_id=999,
            language_code="de",
        )
        monkeypatch.setattr(settings, "booth_access_token", "configured-master-token")
        monkeypatch.setattr(settings, "debug", False)
        response = client.get(
            f"/api/events/global-cookie/booths/en/state?room_id={room_id}",
            headers={"Authorization": "Bearer configured-master-token"},
            cookies={"session_token": participant},
        )
        assert response.status_code == 200, response.text
    finally:
        anyio.run(dispose)
