"""Tests for OAuth IDOR vulnerabilities in v1 endpoints."""

from __future__ import annotations

import asyncio
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
    from portal.database import create_booth, create_event, create_room, create_user, get_session
    from portal.models import DeveloperAccount, EventMembership, OAuthClient, OAuthToken

    async with get_session() as s:
        user = await create_user(s, email="test@example.com", display_name="Test", password_hash=hash_password("pw"))

        # event A
        event_a = await create_event(s, slug="event-a", display_name="Event A")
        room_a = await create_room(s, event_id=event_a.id, display_name="Room A")
        booth_a = await create_booth(s, event_id=event_a.id, room_id=room_a.id, language_code="en", language_name="English")

        # Grant user Event Owner access to Event A
        s.add(EventMembership(user_id=user.id, event_id=event_a.id, role="event_owner"))

        # event B
        event_b = await create_event(s, slug="event-b", display_name="Event B")
        room_b = await create_room(s, event_id=event_b.id, display_name="Room B")
        booth_b = await create_booth(s, event_id=event_b.id, room_id=room_b.id, language_code="es", language_name="Spanish")

        # A cross-tenant rogue booth (attached to Room A, but belonging to Event B)
        rogue_booth = await create_booth(s, event_id=event_b.id, room_id=room_a.id, language_code="fr", language_name="French")

        dev = DeveloperAccount(user_id=user.id)
        s.add(dev)
        await s.flush()

        client = OAuthClient(
            developer_account_id=dev.id,
            name="App",
            client_id="client123",
            client_secret_hash="hash"
        )
        s.add(client)
        await s.flush()

        token_str = "secret_token_a"
        token_hash = hashlib.sha256(token_str.encode()).hexdigest()

        token = OAuthToken(
            client_id=client.id,
            user_id=user.id,
            event_id=event_a.id,
            access_token_hash=token_hash,
            scopes=["events:read", "rooms:read", "booths:read", "events:write", "rooms:write", "booths:write", "sessions:manage", "sessions:read", "transcripts:read", "listeners:provision"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=1)
        )
        s.add(token)
        await s.commit()

    return event_a, room_a, booth_a, event_b, room_b, booth_b, rogue_booth, token_str


class TestApiV1IDOR:
    @pytest.mark.anyio
    async def test_deleted_event_cannot_create_booth(self, seed_data):
        """A deleted event cannot receive a new booth through API v1."""
        event_a, room_a, *_rest, token_a = seed_data

        from sqlalchemy import select

        from portal.database import get_session
        from portal.models import DBBooth

        async with _client() as c:
            delete_response = await c.delete(
                f"/api/v1/events/{event_a.slug}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert delete_response.status_code == 204

            create_response = await c.post(
                f"/api/v1/events/{event_a.slug}/rooms/{room_a.id}/booths/de",
                headers={"Authorization": f"Bearer {token_a}"},
            )

        assert create_response.status_code == 404
        assert create_response.json()["detail"] == "Event not found"

        async with get_session() as session:
            result = await session.execute(
                select(DBBooth).where(
                    DBBooth.event_id == event_a.id,
                    DBBooth.room_id == room_a.id,
                    DBBooth.language_code == "de",
                )
            )
            assert result.scalars().first() is None

    @pytest.mark.anyio
    async def test_deleted_event_cannot_be_restored_by_create_event(self, seed_data):
        """Posting a deleted slug does not restore the event or update its name."""
        event_a, *_rest, token_a = seed_data

        from portal.database import get_session
        from portal.models import Event

        async with _client() as c:
            delete_response = await c.delete(
                f"/api/v1/events/{event_a.slug}",
                headers={"Authorization": f"Bearer {token_a}"},
            )
            assert delete_response.status_code == 204

            create_response = await c.post(
                "/api/v1/events/",
                json={"slug": event_a.slug, "name": "Restored Event"},
                headers={"Authorization": f"Bearer {token_a}"},
            )

        assert create_response.status_code == 404
        assert create_response.json()["detail"] == "Event not found"

        async with get_session() as session:
            deleted_event = await session.get(Event, event_a.id)
            assert deleted_event is not None
            assert deleted_event.deleted_at is not None
            assert deleted_event.display_name == "Event A"

    @pytest.mark.anyio
    async def test_mutating_rbac_rechecks_deleted_event_state(self, seed_data):
        """Mutating authorization rejects an event deleted after its initial load."""
        event_a, *_rest, token_a = seed_data

        from fastapi import HTTPException
        from sqlalchemy import select, update

        from portal.database import get_session
        from portal.models import Event, OAuthToken
        from portal.routers.api_v1 import _verify_token_rbac

        async with get_session() as session:
            event = await session.get(Event, event_a.id)
            token_result = await session.execute(
                select(OAuthToken).where(
                    OAuthToken.access_token_hash == hashlib.sha256(token_a.encode()).hexdigest()
                )
            )
            oauth_token = token_result.scalars().one()

            await session.execute(
                update(Event)
                .where(Event.id == event_a.id)
                .values(deleted_at=datetime.now(timezone.utc))
                .execution_options(synchronize_session=False)
            )
            assert event is not None
            assert event.deleted_at is None

            with pytest.raises(HTTPException) as exc_info:
                await _verify_token_rbac(session, oauth_token, event, lock_event=True)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Event not found"

    @pytest.mark.anyio
    async def test_sqlite_mutation_lock_serializes_event_deletion(self, tmp_path):
        """SQLite mutation authorization holds its write lock until the request ends."""
        from portal.database import configure, create_event, create_room, create_user, dispose, get_session, init_db
        from portal.models import DeveloperAccount, Event, EventMembership, OAuthClient, OAuthToken
        from portal.routers.api_v1 import _verify_token_rbac

        await dispose()
        configure(f"sqlite+aiosqlite:///{tmp_path / 'api-v1-lock.db'}")
        await init_db()

        from portal.auth import hash_password

        async with get_session() as session:
            user = await create_user(
                session,
                email="sqlite-lock@example.com",
                display_name="SQLite Lock",
                password_hash=hash_password("pw"),
            )
            event = await create_event(session, slug="sqlite-lock-event", display_name="SQLite Lock Event")
            room = await create_room(session, event_id=event.id, display_name="Room")
            session.add(EventMembership(user_id=user.id, event_id=event.id, role="event_owner"))
            developer = DeveloperAccount(user_id=user.id)
            session.add(developer)
            await session.flush()
            client = OAuthClient(
                developer_account_id=developer.id,
                name="SQLite Lock App",
                client_id="sqlite-lock-client",
                client_secret_hash="hash",
            )
            session.add(client)
            await session.flush()
            token = OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                access_token_hash="hash",
                scopes=["booths:write", "events:write"],
                expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            )
            session.add(token)
            await session.flush()
            event_id, room_id, token_id = event.id, room.id, token.id

        async def delete_event_after_lock():
            async with get_session() as session:
                event_to_delete = await session.get(Event, event_id)
                token_for_delete = await session.get(OAuthToken, token_id)
                await _verify_token_rbac(
                    session,
                    token_for_delete,
                    event_to_delete,
                    manage=True,
                    lock_event=True,
                    write_lock=True,
                )
                event_to_delete.deleted_at = datetime.now(timezone.utc)
                await session.flush()

        async with get_session() as mutation_session:
            event_for_mutation = await mutation_session.get(Event, event_id)
            token_for_mutation = await mutation_session.get(OAuthToken, token_id)
            await _verify_token_rbac(
                mutation_session,
                token_for_mutation,
                event_for_mutation,
                room_id,
                manage=True,
                lock_event=True,
                write_lock=True,
            )

            deletion_task = asyncio.create_task(delete_event_after_lock())
            await asyncio.sleep(0.1)
            assert not deletion_task.done()

        await asyncio.wait_for(deletion_task, timeout=5)

    @pytest.mark.anyio
    async def test_room_id_cross_tenant_returns_404(self, seed_data):
        event_a, room_a, booth_a, event_b, room_b, booth_b, rogue_booth, token_a = seed_data

        async with _client() as c:
            # Token A belongs to Event A.
            # We try to access Room B through Event A's API URL.
            resp = await c.get(
                f"/api/v1/events/{event_a.slug}/rooms/{room_b.id}/booths",
                headers={"Authorization": f"Bearer {token_a}"}
            )
            assert resp.status_code == 404
            assert resp.json()["detail"] == "Room not found"

            # Cross-tenant POST to create a booth in Room B using Event A token
            resp_post = await c.post(
                f"/api/v1/events/{event_a.slug}/rooms/{room_b.id}/booths/de",
                headers={"Authorization": f"Bearer {token_a}"}
            )
            assert resp_post.status_code == 404
            assert resp_post.json()["detail"] == "Room not found"

    @pytest.mark.anyio
    async def test_booth_cross_tenant_returns_404(self, seed_data):
        event_a, room_a, booth_a, event_b, room_b, booth_b, rogue_booth, token_a = seed_data

        async with _client() as c:
            # Accessing Room B's booth through Event A
            resp = await c.get(
                f"/api/v1/events/{event_a.slug}/rooms/{room_b.id}/booths/es",
                headers={"Authorization": f"Bearer {token_a}"}
            )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Room not found"

    @pytest.mark.anyio
    async def test_booth_db_query_bounds(self, seed_data):
        event_a, room_a, booth_a, event_b, room_b, booth_b, rogue_booth, token_a = seed_data

        async with _client() as c:
            # 1. Positive Access Check
            # We should be able to see Booth A
            resp = await c.get(
                f"/api/v1/events/{event_a.slug}/rooms/{room_a.id}/booths/en",
                headers={"Authorization": f"Bearer {token_a}"}
            )
            assert resp.status_code == 200

            # 2. List exclusion
            # Event B has a rogue booth 'fr' attached to Room A. It should NOT be returned when we query Event A.
            resp = await c.get(
                f"/api/v1/events/{event_a.slug}/rooms/{room_a.id}/booths",
                headers={"Authorization": f"Bearer {token_a}"}
            )
            assert resp.status_code == 200
            langs = [b["language_code"] for b in resp.json()]
            assert "en" in langs
            assert "fr" not in langs

            # 3. Lookup exclusion
            resp = await c.get(
                f"/api/v1/events/{event_a.slug}/rooms/{room_a.id}/booths/fr",
                headers={"Authorization": f"Bearer {token_a}"}
            )
            assert resp.status_code == 404

            # 4. Creation checks duplicate by room_id and language_code and returns 409
            resp = await c.post(
                f"/api/v1/events/{event_a.slug}/rooms/{room_a.id}/booths/fr",
                headers={"Authorization": f"Bearer {token_a}"}
            )
            assert resp.status_code == 409
            assert resp.json()["detail"] == "Booth already exists"

            # 5. Deletion fails to delete the Event B booth when attempting to delete through Event A
            # Booth B ('es') is in Room B (Event B). If an Event A token tries to delete it using its room_b.id, it should 404.
            resp = await c.delete(
                f"/api/v1/events/{event_a.slug}/rooms/{room_b.id}/booths/es",
                headers={"Authorization": f"Bearer {token_a}"}
            )
            assert resp.status_code == 404
