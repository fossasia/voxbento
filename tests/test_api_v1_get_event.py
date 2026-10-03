"""Regression tests for GET /api/v1/events/{slug} — Issue: AttributeError owner_id.

Covers:
- Returns 200 with correct fields (including owner_id resolved from EventMembership).
- Returns owner_id=None (null in JSON) when no event_owner membership exists.
- Returns lowest user_id deterministically when multiple event_owners exist.
- Returns 404 for a soft-deleted event.
- Returns 404 for an unknown slug.
- Returns 401 when no Bearer token is supplied.
- Returns 403 when the token belongs to a different event.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_event(slug: str, display_name: str):
    from portal.database import create_event, get_session

    async with get_session() as s:
        event = await create_event(s, slug=slug, display_name=display_name)
    return event


async def _make_user(email: str, is_admin: bool = False):
    from portal.auth import hash_password
    from portal.database import create_user, get_session

    async with get_session() as s:
        user = await create_user(
            s,
            email=email,
            display_name="Test User",
            password_hash=hash_password("pass"),
            email_verified=True,
        )
        if is_admin:
            user.is_admin = True
    return user


async def _make_event_membership(user_id: int, event_id: int, role: str):
    from portal.database import get_session, set_event_membership

    async with get_session() as s:
        membership = await set_event_membership(s, user_id=user_id, event_id=event_id, role=role)
    return membership


async def _make_oauth_token(user_id: int, event_id: int, scopes: list[str]) -> str:
    """Insert a raw OAuthToken into the DB and return the raw (unhashed) token string."""
    from portal.database import get_session
    from portal.models import DeveloperAccount, OAuthClient, OAuthToken

    raw_token = secrets.token_hex(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    async with get_session() as s:
        dev = DeveloperAccount(user_id=user_id, status="approved")
        s.add(dev)
        await s.flush()

        client = OAuthClient(
            developer_account_id=dev.id,
            client_id=secrets.token_hex(8),
            name="Test Client",
            redirect_uris=["https://example.com/callback"],
            scopes_requested=scopes,
            is_confidential=True,
            status="active",
        )
        s.add(client)
        await s.flush()

        token = OAuthToken(
            client_id=client.id,
            user_id=user_id,
            event_id=event_id,
            scopes=scopes,
            access_token_hash=token_hash,
            expires_at=expires_at,
            revoked=False,
        )
        s.add(token)
        await s.commit()

    return raw_token


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetEvent:
    @pytest.mark.anyio
    async def test_returns_owner_id_from_membership(self, setup_db):
        """owner_id must be resolved from EventMembership, not event.owner_id."""
        event = await _make_event("fossasia-2026", "FOSSASIA 2026")
        user = await _make_user("owner@example.com")
        await _make_event_membership(user.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        async with _client() as client:
            resp = await client.get(
                f"/api/v1/events/{event.slug}",
                headers={"Authorization": f"Bearer {raw_token}"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == event.id
        assert data["slug"] == event.slug
        assert data["display_name"] == event.display_name
        assert data["owner_id"] == user.id
        assert "created_at" in data

    @pytest.mark.anyio
    async def test_owner_id_is_none_when_no_owner_membership(self, setup_db):
        """If no event_owner row exists, owner_id must be None (null in JSON).

        An admin user token is used so RBAC passes without requiring an event_owner row.
        """
        event = await _make_event("no-owner-event", "No Owner Event")
        admin_user = await _make_user("admin@example.com", is_admin=True)
        raw_token = await _make_oauth_token(admin_user.id, event.id, ["events:read"])

        async with _client() as client:
            resp = await client.get(
                f"/api/v1/events/{event.slug}",
                headers={"Authorization": f"Bearer {raw_token}"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == event.id
        assert data["slug"] == event.slug
        assert data["owner_id"] is None

    @pytest.mark.anyio
    async def test_owner_id_is_deterministic_with_multiple_owners(self, setup_db):
        """When multiple event_owner rows exist, the lowest user_id must be deterministically returned."""
        event = await _make_event("multi-owner-event", "Multi Owner Event")
        user_1 = await _make_user("owner1@example.com")
        user_2 = await _make_user("owner2@example.com")
        await _make_event_membership(user_1.id, event.id, "event_owner")
        await _make_event_membership(user_2.id, event.id, "event_owner")

        expected_owner_id = min(user_1.id, user_2.id)
        raw_token = await _make_oauth_token(user_1.id, event.id, ["events:read"])

        async with _client() as client:
            resp = await client.get(
                f"/api/v1/events/{event.slug}",
                headers={"Authorization": f"Bearer {raw_token}"},
            )

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["owner_id"] == expected_owner_id

    @pytest.mark.anyio
    async def test_soft_deleted_event_returns_404(self, setup_db):
        """A soft-deleted event (deleted_at is set) must return 404."""
        from portal.database import get_session
        from portal.models import Event

        event = await _make_event("deleted-event", "Deleted Event")
        user = await _make_user("owner-del@example.com")
        await _make_event_membership(user.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        async with get_session() as s:
            evt = await s.get(Event, event.id)
            evt.deleted_at = datetime.now(timezone.utc)

        async with _client() as client:
            resp = await client.get(
                f"/api/v1/events/{event.slug}",
                headers={"Authorization": f"Bearer {raw_token}"},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Event not found"

    @pytest.mark.anyio
    async def test_returns_404_for_unknown_slug(self, setup_db):
        """A slug that doesn't exist must return 404."""
        event = await _make_event("real-event", "Real Event")
        user = await _make_user("user404@example.com")
        await _make_event_membership(user.id, event.id, "event_owner")
        raw_token = await _make_oauth_token(user.id, event.id, ["events:read"])

        async with _client() as client:
            resp = await client.get(
                "/api/v1/events/does-not-exist",
                headers={"Authorization": f"Bearer {raw_token}"},
            )

        assert resp.status_code == 404
        assert resp.json()["detail"] == "Event not found"

    @pytest.mark.anyio
    async def test_returns_401_without_token(self, setup_db):
        """Requests without an Authorization header must be rejected with 401."""
        await _make_event("open-event", "Open Event")

        async with _client() as client:
            resp = await client.get("/api/v1/events/open-event")

        assert resp.status_code == 401

    @pytest.mark.anyio
    async def test_returns_403_for_wrong_event(self, setup_db):
        """A token scoped to event-A must be rejected when requesting event-B."""
        event_a = await _make_event("event-alpha", "Event Alpha")
        event_b = await _make_event("event-beta", "Event Beta")
        user = await _make_user("crossevent@example.com")
        await _make_event_membership(user.id, event_a.id, "event_owner")
        # Token is scoped to event_a
        raw_token = await _make_oauth_token(user.id, event_a.id, ["events:read"])

        async with _client() as client:
            resp = await client.get(
                f"/api/v1/events/{event_b.slug}",
                headers={"Authorization": f"Bearer {raw_token}"},
            )

        assert resp.status_code == 403
