from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.database import configure, dispose, get_session, init_db
from portal.models import DeveloperAccount, Event, EventMembership, OAuthClient, OAuthToken, User
from portal.rate_limit import InMemoryRateLimiter
from portal.roles import EVENT_OWNER, ROOM_COORDINATOR
from portal.routers import api_v1
from portal.team import remove_assignment

ACCESS_TOKEN = "event-upsert-test-token"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}"}


@pytest.fixture(autouse=True)
async def setup_db(monkeypatch):
    configure("sqlite+aiosqlite://")
    await init_db()
    monkeypatch.setattr(api_v1, "auth_rate_limiter", InMemoryRateLimiter(10, 60))
    yield
    await dispose()


@pytest.fixture
async def access():
    async with get_session() as session:
        user = User(email="owner@example.com", display_name="Owner", email_verified=True)
        remaining_owner = User(email="remaining@example.com", display_name="Remaining owner")
        event = Event(slug="upsert-event", display_name="Original event")
        session.add_all([user, remaining_owner, event])
        await session.flush()
        account = DeveloperAccount(user_id=user.id, status="approved")
        session.add(account)
        await session.flush()
        client = OAuthClient(
            developer_account_id=account.id, client_id="upsert-client", name="Public app", is_confidential=False
        )
        session.add(client)
        await session.flush()
        membership = EventMembership(user_id=user.id, event_id=event.id, role=EVENT_OWNER)
        session.add_all(
            [
                membership,
                EventMembership(user_id=remaining_owner.id, event_id=event.id, role=EVENT_OWNER),
                OAuthToken(
                    client_id=client.id,
                    user_id=user.id,
                    event_id=event.id,
                    scopes=["events:read", "events:write"],
                    access_token_hash=hashlib.sha256(ACCESS_TOKEN.encode()).hexdigest(),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                ),
            ]
        )
        await session.flush()
        return event.id, user.id, client.id, membership.id


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("change", "confidential"),
    [("removed", False), ("demoted", False), ("inactive", False), ("inactive", True)],
)
async def test_existing_event_upsert_rejects_lost_access_without_side_effects(access, change, confidential):
    event_id, user_id, client_id, membership_id = access
    async with get_session() as session:
        client = await session.get(OAuthClient, client_id)
        client.is_confidential = confidential
        if change == "removed":
            await remove_assignment(session, event_id=event_id, level="event", membership_id=membership_id)
        elif change == "demoted":
            membership = await session.get(EventMembership, membership_id)
            membership.role = ROOM_COORDINATOR
        else:
            user = await session.get(User, user_id)
            user.is_active = False

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/events/",
            headers=HEADERS,
            json={"slug": "upsert-event", "name": "Changed", "organizer_emails": ["outsider@example.com"]},
        )

    assert response.status_code == 403
    async with get_session() as session:
        event = await session.get(Event, event_id)
        membership = await session.get(EventMembership, membership_id)
        assert event.display_name == "Original event"
        assert await session.scalar(select(User).where(User.email == "outsider@example.com")) is None
        if change == "removed":
            assert membership is None
        else:
            assert membership.role == (ROOM_COORDINATOR if change == "demoted" else EVENT_OWNER)


@pytest.mark.anyio
@pytest.mark.parametrize("caller", ["owner", "admin", "confidential"])
async def test_existing_event_upsert_preserves_authorized_callers(access, caller):
    event_id, user_id, client_id, membership_id = access
    async with get_session() as session:
        if caller != "owner":
            await remove_assignment(session, event_id=event_id, level="event", membership_id=membership_id)
        if caller == "admin":
            user = await session.get(User, user_id)
            user.is_admin = True
        elif caller == "confidential":
            client = await session.get(OAuthClient, client_id)
            client.is_confidential = True

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/events/",
            headers=HEADERS,
            json={"slug": "upsert-event", "name": "Updated", "organizer_emails": ["organizer@example.com"]},
        )

    assert response.status_code == 201
    async with get_session() as session:
        event = await session.get(Event, event_id)
        assert event.display_name == "Updated"
        organizer = await session.scalar(select(User).where(User.email == "organizer@example.com"))
        assert organizer is not None
        membership = await session.scalar(
            select(EventMembership).where(EventMembership.event_id == event_id, EventMembership.user_id == organizer.id)
        )
        assert membership.role == EVENT_OWNER


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["removed", "inactive", "deleted"])
async def test_new_event_creation_requires_current_access_to_authorizing_event(access, change):
    event_id, user_id, _, membership_id = access
    async with get_session() as session:
        if change == "removed":
            await remove_assignment(session, event_id=event_id, level="event", membership_id=membership_id)
        elif change == "inactive":
            user = await session.get(User, user_id)
            user.is_active = False
        else:
            event = await session.get(Event, event_id)
            event.deleted_at = datetime.now(timezone.utc)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/events/", headers=HEADERS, json={"slug": "new-event", "name": "New event"}
        )

    assert response.status_code == (404 if change == "deleted" else 403)
    async with get_session() as session:
        assert await session.scalar(select(Event).where(Event.slug == "new-event")) is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("state", "expected"),
    [("owner", 201), ("coordinator", 403), ("inactive", 403), ("admin", 403), ("confidential", 403)],
)
async def test_existing_event_upsert_outside_token_scope_requires_current_ownership(access, state, expected):
    _, user_id, client_id, _ = access
    async with get_session() as session:
        other = Event(slug="other-event", display_name="Other event")
        session.add(other)
        await session.flush()
        role = EVENT_OWNER if state in ("owner", "inactive") else ROOM_COORDINATOR
        session.add(EventMembership(user_id=user_id, event_id=other.id, role=role))
        user = await session.get(User, user_id)
        user.is_active = state != "inactive"
        user.is_admin = state == "admin"
        client = await session.get(OAuthClient, client_id)
        client.is_confidential = state == "confidential"
        other_id = other.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/events/", headers=HEADERS, json={"slug": "other-event", "name": "Changed"}
        )

    assert response.status_code == expected
    async with get_session() as session:
        other = await session.get(Event, other_id)
        assert other.display_name == ("Changed" if expected == 201 else "Other event")


@pytest.mark.anyio
@pytest.mark.parametrize("confidential", [False, True])
async def test_new_event_creation_preserves_active_organizer_provisioning(access, confidential):
    _, user_id, client_id, _ = access
    async with get_session() as session:
        client = await session.get(OAuthClient, client_id)
        client.is_confidential = confidential

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/events/", headers=HEADERS, json={"slug": "new-event", "name": "New event"}
        )

    assert response.status_code == 201
    assert response.json() == {"status": "success", "event_slug": "new-event"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        retry = await client.post("/api/v1/events/", headers=HEADERS, json={"slug": "new-event", "name": "New event"})

    assert retry.status_code == 201
    async with get_session() as session:
        event = await session.scalar(select(Event).where(Event.slug == "new-event"))
        membership = await session.scalar(
            select(EventMembership).where(EventMembership.event_id == event.id, EventMembership.user_id == user_id)
        )
        assert membership.role == EVENT_OWNER
