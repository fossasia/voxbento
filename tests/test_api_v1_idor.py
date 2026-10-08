"""Tests for OAuth IDOR vulnerabilities in v1 endpoints."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"

import pytest

from portal.config import settings

settings.admin_password = "test-admin-pass"


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
    from portal.models import DeveloperAccount, OAuthClient, OAuthToken

    async with get_session() as s:
        user = await create_user(s, email="test@example.com", display_name="Test", password_hash=hash_password("pw"))

        # event A
        event_a = await create_event(s, slug="event-a", display_name="Event A")
        room_a = await create_room(s, event_id=event_a.id, display_name="Room A")
        booth_a = await create_booth(s, event_id=event_a.id, room_id=room_a.id, language_code="en", language_name="English")

        # event B
        event_b = await create_event(s, slug="event-b", display_name="Event B")
        room_b = await create_room(s, event_id=event_b.id, display_name="Room B")
        booth_b = await create_booth(s, event_id=event_b.id, room_id=room_b.id, language_code="es", language_name="Spanish")

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

    return event_a, room_a, booth_a, event_b, room_b, booth_b, token_str


class TestApiV1IDOR:
    @pytest.mark.anyio
    async def test_room_id_cross_tenant_returns_404(self, seed_data):
        event_a, room_a, booth_a, event_b, room_b, booth_b, token_a = seed_data

        async with _client() as c:
            # Token A belongs to Event A.
            # We try to access Room B through Event A's API URL.
            resp = await c.get(
                f"/api/v1/events/{event_a.slug}/rooms/{room_b.id}/booths",
                headers={"Authorization": f"Bearer {token_a}"}
            )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Room not found"

    @pytest.mark.anyio
    async def test_booth_cross_tenant_returns_404(self, seed_data):
        event_a, room_a, booth_a, event_b, room_b, booth_b, token_a = seed_data

        async with _client() as c:
            # Accessing Room B's booth through Event A
            resp = await c.get(
                f"/api/v1/events/{event_a.slug}/rooms/{room_b.id}/booths/es",
                headers={"Authorization": f"Bearer {token_a}"}
            )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Room not found"
