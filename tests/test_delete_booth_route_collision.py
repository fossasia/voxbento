from __future__ import annotations

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import portal.routers.api as api_module
from fastapi_app import app
from portal.auth import create_user_token
from portal.config import settings
from portal.database import (
    configure,
    create_event,
    create_room,
    create_user,
    dispose,
    get_session,
    init_db,
    set_event_membership,
)
from portal.models import DBBooth, Event, Room

client = TestClient(app)


def _setup_database():
    configure("sqlite+aiosqlite://")
    anyio.run(init_db)
    return dispose


def test_delete_booth_by_internal_room_id(monkeypatch):
    """Verify that deleting through the internal Room.id route deletes the booth for that database room."""
    cleanup = _setup_database()
    try:

        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="event-internal", display_name="Internal Event")
                room = await create_room(
                    session, event_id=event.id, display_name="Main Room", eventyay_room_id="ext-room-1"
                )
                booth = DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English")
                session.add(booth)
                await session.commit()
                return event.slug, room.id

        slug, room_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "test-token")
        monkeypatch.setattr(settings, "debug", False)

        resp = client.delete(
            f"/api/events/{slug}/rooms/{room_id}/booths/en",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 204

        async def check_db():
            async with get_session() as session:
                booths = (await session.execute(select(DBBooth).where(DBBooth.room_id == room_id))).scalars().all()
                assert len(booths) == 0

        anyio.run(check_db)
    finally:
        anyio.run(cleanup)


def test_delete_booth_by_external_room_id_non_numeric(monkeypatch):
    """Verify that deleting through the canonical external-ID route deletes the booth for a non-numeric Eventyay room ID."""
    cleanup = _setup_database()
    try:

        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="event-ext-non-num", display_name="Ext Event")
                room = await create_room(
                    session, event_id=event.id, display_name="Track Room", eventyay_room_id="ext-room-99"
                )
                booth = DBBooth(event_id=event.id, room_id=room.id, language_code="de", language_name="German")
                session.add(booth)
                await session.commit()
                return event.slug, room.id

        slug, room_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "test-token")
        monkeypatch.setattr(settings, "debug", False)

        # Deleting through the explicit external-ID route must succeed and remove the booth
        resp = client.delete(
            f"/api/events/{slug}/eventyay-rooms/ext-room-99/booths/de",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 204

        async def check_db():
            async with get_session() as session:
                booths = (await session.execute(select(DBBooth).where(DBBooth.room_id == room_id))).scalars().all()
                assert len(booths) == 0

        anyio.run(check_db)
    finally:
        anyio.run(cleanup)


def test_delete_booth_by_external_room_id_numeric_looking(monkeypatch):
    """Verify that deleting through the canonical external-ID route deletes the booth for a numeric-looking Eventyay room ID."""
    cleanup = _setup_database()
    try:

        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="event-ext-num", display_name="Ext Num Event")
                room = await create_room(
                    session, event_id=event.id, display_name="Room Numeric", eventyay_room_id="12345"
                )
                booth = DBBooth(event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
                session.add(booth)
                await session.commit()
                return event.slug, room.id

        slug, room_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "test-token")
        monkeypatch.setattr(settings, "debug", False)

        resp = client.delete(
            f"/api/events/{slug}/eventyay-rooms/12345/booths/fr",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 204

        async def check_db():
            async with get_session() as session:
                booths = (await session.execute(select(DBBooth).where(DBBooth.room_id == room_id))).scalars().all()
                assert len(booths) == 0

        anyio.run(check_db)
    finally:
        anyio.run(cleanup)


def test_canonical_routes_isolate_internal_and_external_identifier_namespaces(monkeypatch):
    """Verify namespace isolation between internal and external identifier routes.

    Setup:
    - Room A: internal ID = 10, external ID = 'external-room-a'
    - Room B: internal ID = 20, external ID = '10' (numeric-looking string matching Room A's internal ID)
    Both rooms have a 'de' booth.

    When using the canonical external route:
    - DELETE /eventyay-rooms/10/booths/de resolves Room B by its external ID '10',
      deleting Room B's booth and leaving Room A's booth completely intact.

    When using the internal route:
    - DELETE /rooms/10/booths/de resolves Room A by its internal ID 10,
      deleting Room A's booth.

    Clarification on route contracts:
    - Passing an external room ID (even if numeric-looking, like '10') to `/rooms/{room_id}`
      is an invalid client request that treats the path parameter strictly as an internal
      integer Room.id (deleting Room A, not Room B).
    - Clients with external Eventyay room IDs must use the canonical external route
      `/eventyay-rooms/{eventyay_room_id}`.
    """
    cleanup = _setup_database()
    try:

        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="event-isolation", display_name="Isolation Event")
                room_a = await create_room(
                    session, event_id=event.id, display_name="Room A", eventyay_room_id="external-room-a"
                )
                room_b = await create_room(
                    session, event_id=event.id, display_name="Room B", eventyay_room_id=str(room_a.id)
                )

                booth_a = DBBooth(event_id=event.id, room_id=room_a.id, language_code="de", language_name="German")
                booth_b = DBBooth(event_id=event.id, room_id=room_b.id, language_code="de", language_name="German")
                session.add_all([booth_a, booth_b])
                await session.commit()
                return event.slug, room_a.id, room_b.id, str(room_a.id)

        slug, room_a_id, room_b_id, room_b_ext_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "test-token")
        monkeypatch.setattr(settings, "debug", False)

        # 1. Target Room B via external-ID route: /eventyay-rooms/{eventyay_room_id}/
        resp_ext = client.delete(
            f"/api/events/{slug}/eventyay-rooms/{room_b_ext_id}/booths/de",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp_ext.status_code == 204

        async def verify_after_ext_delete():
            async with get_session() as session:
                # Room B booth must be deleted
                booths_b = (await session.execute(select(DBBooth).where(DBBooth.room_id == room_b_id))).scalars().all()
                assert len(booths_b) == 0

                # Room A booth must STILL exist
                booths_a = (await session.execute(select(DBBooth).where(DBBooth.room_id == room_a_id))).scalars().all()
                assert len(booths_a) == 1
                assert booths_a[0].language_code == "de"

        anyio.run(verify_after_ext_delete)

        # 2. Target Room A via internal-ID route: /rooms/{room_id}/
        resp_int = client.delete(
            f"/api/events/{slug}/rooms/{room_a_id}/booths/de",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp_int.status_code == 204

        async def verify_after_int_delete():
            async with get_session() as session:
                booths_a = (await session.execute(select(DBBooth).where(DBBooth.room_id == room_a_id))).scalars().all()
                assert len(booths_a) == 0

        anyio.run(verify_after_int_delete)
    finally:
        anyio.run(cleanup)


def test_delete_booth_internal_room_id_not_found_is_idempotent(monkeypatch):
    """Verify existing behavior: when an internal room ID does not exist, DELETE returns 204 No Content."""
    cleanup = _setup_database()
    try:

        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="event-missing-room", display_name="Missing Room Event")
                await session.commit()
                return event.slug

        slug = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "test-token")
        monkeypatch.setattr(settings, "debug", False)

        resp = client.delete(
            f"/api/events/{slug}/rooms/99999/booths/en",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 204
    finally:
        anyio.run(cleanup)


def test_delete_booth_external_room_id_not_found(monkeypatch):
    """Verify existing behavior: when an external room ID does not exist, DELETE returns 404 Room not found."""
    cleanup = _setup_database()
    try:

        async def seed():
            async with get_session() as session:
                event = await create_event(
                    session, slug="event-missing-ext-room", display_name="Missing Ext Room Event"
                )
                await session.commit()
                return event.slug

        slug = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "test-token")
        monkeypatch.setattr(settings, "debug", False)

        resp = client.delete(
            f"/api/events/{slug}/eventyay-rooms/nonexistent-ext-id/booths/en",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Room not found"
    finally:
        anyio.run(cleanup)


def test_delete_booth_nonexistent_language_leaves_existing_booths_intact(monkeypatch):
    """Verify that deleting a non-existent language returns 204 and does not affect existing booths."""
    cleanup = _setup_database()
    try:

        async def seed():
            async with get_session() as session:
                event = await create_event(session, slug="event-missing-booth", display_name="Missing Booth Event")
                room = await create_room(
                    session, event_id=event.id, display_name="Main Room", eventyay_room_id="ext-room-existing"
                )
                booth = DBBooth(event_id=event.id, room_id=room.id, language_code="en", language_name="English")
                session.add(booth)
                await session.commit()
                return event.slug, room.id

        slug, room_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "test-token")
        monkeypatch.setattr(settings, "debug", False)

        # Deleting a non-existent language ('ja') on internal route returns 204
        resp_int = client.delete(
            f"/api/events/{slug}/rooms/{room_id}/booths/ja",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp_int.status_code == 204

        # Deleting a non-existent language ('ja') on external route returns 204
        resp_ext = client.delete(
            f"/api/events/{slug}/eventyay-rooms/ext-room-existing/booths/ja",
            headers={"Authorization": "Bearer test-token"},
        )
        assert resp_ext.status_code == 204

        async def verify_db():
            async with get_session() as session:
                booths = (await session.execute(select(DBBooth).where(DBBooth.room_id == room_id))).scalars().all()
                assert len(booths) == 1
                assert booths[0].language_code == "en"

        anyio.run(verify_db)
    finally:
        anyio.run(cleanup)


def test_delete_booth_authorization_restrictions(monkeypatch):
    """Verify authorization restrictions on both internal and external delete routes."""
    cleanup = _setup_database()
    try:

        async def seed():
            async with get_session() as session:
                event_a = await create_event(session, slug="event-auth-a", display_name="Auth A")
                event_b = await create_event(session, slug="event-auth-b", display_name="Auth B")
                room_a = await create_room(
                    session, event_id=event_a.id, display_name="Room A", eventyay_room_id="ext-a"
                )
                booth_a = DBBooth(event_id=event_a.id, room_id=room_a.id, language_code="en", language_name="English")
                session.add(booth_a)

                # Owner of event_b (not event_a)
                owner_b = await create_user(
                    session, email="owner_b@example.com", display_name="Owner B", password_hash="hash"
                )
                await set_event_membership(session, user_id=owner_b.id, event_id=event_b.id, role="event_owner")

                # Owner of event_a
                owner_a = await create_user(
                    session, email="owner_a@example.com", display_name="Owner A", password_hash="hash"
                )
                await set_event_membership(session, user_id=owner_a.id, event_id=event_a.id, role="event_owner")

                await session.commit()
                return event_a.slug, room_a.id, owner_b.id, owner_a.id

        slug_a, room_a_id, owner_b_id, owner_a_id = anyio.run(seed)
        monkeypatch.setattr(settings, "booth_access_token", "master-token")
        monkeypatch.setattr(settings, "debug", False)

        # 1. No token -> 403 on both routes
        assert client.delete(f"/api/events/{slug_a}/rooms/{room_a_id}/booths/en").status_code == 403
        assert client.delete(f"/api/events/{slug_a}/eventyay-rooms/ext-a/booths/en").status_code == 403

        # 2. Invalid token -> 403 on both routes
        bad_headers = {"Authorization": "Bearer wrong-token"}
        assert (
            client.delete(f"/api/events/{slug_a}/rooms/{room_a_id}/booths/en", headers=bad_headers).status_code == 403
        )
        assert (
            client.delete(f"/api/events/{slug_a}/eventyay-rooms/ext-a/booths/en", headers=bad_headers).status_code
            == 403
        )

        # 3. Unauthorized user (owner of B trying to delete booth in A) -> 403 on both routes
        token_b = create_user_token(user_id=owner_b_id, email="owner_b@example.com")
        unauth_headers = {"Authorization": f"Bearer {token_b}"}
        assert (
            client.delete(f"/api/events/{slug_a}/rooms/{room_a_id}/booths/en", headers=unauth_headers).status_code
            == 403
        )
        assert (
            client.delete(f"/api/events/{slug_a}/eventyay-rooms/ext-a/booths/en", headers=unauth_headers).status_code
            == 403
        )

        # 4. Authorized owner of A -> 204
        token_a = create_user_token(user_id=owner_a_id, email="owner_a@example.com")
        auth_headers = {"Authorization": f"Bearer {token_a}"}
        resp_auth = client.delete(f"/api/events/{slug_a}/rooms/{room_a_id}/booths/en", headers=auth_headers)
        assert resp_auth.status_code == 204
    finally:
        anyio.run(cleanup)


def test_router_has_no_duplicate_or_dead_booth_delete_routes():
    """Verify that route registration contains no duplicate or dead DELETE endpoints for booths.

    Guarantees:
    1. /api/events/{event_slug}/rooms/{room_id}/booths/{language_code} is only registered to delete_booth_by_language.
    2. /api/events/{event_slug}/eventyay-rooms/{eventyay_room_id}/booths/{language_code} is only registered to delete_event_booth.
    3. The dead route /api/events/{event_slug}/rooms/{eventyay_room_id}/booths/{language_code} is not registered.
    """
    delete_routes = []
    for r in app.routes:
        if hasattr(r, "original_router") and r.original_router == api_module.router:
            for route in r.original_router.routes:
                if "booths/{language_code}" in route.path and "DELETE" in route.methods:
                    delete_routes.append((route.path, route.endpoint.__name__))

    assert len(delete_routes) == 2, f"Expected exactly 2 delete routes, found {len(delete_routes)}: {delete_routes}"

    paths_and_handlers = set(delete_routes)
    assert (
        "/api/events/{event_slug}/rooms/{room_id}/booths/{language_code}",
        "delete_booth_by_language",
    ) in paths_and_handlers
    assert (
        "/api/events/{event_slug}/eventyay-rooms/{eventyay_room_id}/booths/{language_code}",
        "delete_event_booth",
    ) in paths_and_handlers

    # Confirm dead conflicting decorator was removed
    assert (
        "/api/events/{event_slug}/rooms/{eventyay_room_id}/booths/{language_code}",
        "delete_event_booth",
    ) not in paths_and_handlers
