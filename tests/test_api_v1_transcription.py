"""
Tests for the /api/v1 transcription start route.

Every request used to fail with a TypeError and come back as a 500, so the route
is driven over HTTP here and asserted on its status code.

Stop and status are covered by #508.

Covers:
- Start returns 200 and hands the worker a room-scoped booth ID
- Start rejects a booth that is not live, not enabled or missing a key
- A full worker pool is a 429
- Unknown event, missing token, and no 5xx under any of them
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import pytest

from portal.globals import booths

EVENT_SLUG = "pycon2026"
ACCESS_TOKEN = "test-oauth-access-token"
SCOPES = ["sessions:manage", "sessions:read"]


@pytest.fixture(autouse=True)
async def setup_db():
    """Set up an in-memory database for the FastAPI app."""
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture(autouse=True)
def clear_registry():
    """Keep the in-memory booth registry from leaking between tests."""
    from portal.transcription.worker import active_workers

    booths._booths.clear()
    active_workers.clear()
    yield
    booths._booths.clear()
    active_workers.clear()


@pytest.fixture
async def seed():
    """Seed an event, a room, a transcription-enabled booth and an OAuth token."""
    from portal.database import create_booth, create_event, create_room, get_session
    from portal.models import DeveloperAccount, EventMembership, OAuthClient, OAuthToken, User

    async with get_session() as s:
        event = await create_event(s, slug=EVENT_SLUG, display_name="PyCon 2026")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        booth = await create_booth(
            s,
            event_id=event.id,
            room_id=room.id,
            language_code="en",
            language_name="English",
        )
        booth.transcription_enabled = True
        booth.transcription_provider = "local"
        booth.transcription_model = "tiny"

        user = User(
            email="owner@example.com",
            display_name="Event Owner",
            password_hash="x",
            is_admin=True,
        )
        s.add(user)
        await s.flush()

        # _verify_token_rbac only accepts a super admin or an event owner
        s.add(EventMembership(user_id=user.id, event_id=event.id, role="event_owner"))

        account = DeveloperAccount(user_id=user.id, status="approved")
        s.add(account)
        await s.flush()

        client = OAuthClient(
            developer_account_id=account.id,
            client_id="test-client",
            client_secret_hash="x",
            name="Eventyay",
        )
        s.add(client)
        await s.flush()

        s.add(
            OAuthToken(
                client_id=client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=SCOPES,
                access_token_hash=hashlib.sha256(ACCESS_TOKEN.encode()).hexdigest(),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        await s.flush()
        return {"event_id": event.id, "room_id": room.id, "booth_id": booth.id}


def client():
    """Return an ASGI client bound to the real FastAPI app."""
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
    )


def base(room_id: int, language_code: str = "en") -> str:
    """Build the booth-scoped route prefix."""
    return f"/api/v1/events/{EVENT_SLUG}/rooms/{room_id}/booths/{language_code}"


async def go_live(room_id: int, language_code: str = "en") -> str:
    """Put the booth in the in-memory registry the way a joining interpreter would."""
    await booths.create_booth(EVENT_SLUG, language_code, "English", room_id)
    from portal.booth_identity import make_booth_id

    return make_booth_id(EVENT_SLUG, room_id, language_code)


@pytest.mark.anyio
async def test_start_returns_200_and_uses_room_scoped_booth_id(seed, monkeypatch):
    """Start succeeds and passes the room-scoped ID plus the booth's settings to the worker."""
    room_id = seed["room_id"]
    await go_live(room_id)
    started = {}

    async def fake_worker(*args, **kwargs):
        """Capture what the route hands the worker."""
        started["args"] = args
        started["kwargs"] = kwargs

    monkeypatch.setattr("portal.routers.api_v1.start_transcription_worker", fake_worker)

    async with client() as c:
        resp = await c.post(f"{base(room_id)}/transcription/start")

    assert resp.status_code == 200
    assert resp.json() == {"status": "started", "booth_id": f"{EVENT_SLUG}-{room_id}-en"}
    assert started["args"][:3] == (EVENT_SLUG, "en", f"{EVENT_SLUG}-{room_id}-en")
    assert started["args"][4:6] == ("local", "tiny")
    assert started["kwargs"] == {"room_id": room_id}


@pytest.mark.anyio
async def test_start_returns_400_when_booth_not_live(seed):
    """A booth absent from the registry is a client error not a 500."""
    async with client() as c:
        resp = await c.post(f"{base(seed['room_id'])}/transcription/start")

    assert resp.status_code == 400
    assert "not active" in resp.json()["detail"]


@pytest.mark.anyio
async def test_start_returns_400_when_transcription_disabled(seed):
    """A booth with transcription switched off is rejected before the worker is touched."""
    from portal.database import get_session
    from portal.models import DBBooth

    async with get_session() as s:
        booth = await s.get(DBBooth, seed["booth_id"])
        booth.transcription_enabled = False

    await go_live(seed["room_id"])
    async with client() as c:
        resp = await c.post(f"{base(seed['room_id'])}/transcription/start")

    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@pytest.mark.anyio
async def test_start_returns_400_when_external_provider_has_no_key(seed):
    """An external provider without an event key is reported rather than started."""
    from portal.database import get_session
    from portal.models import DBBooth, Event

    async with get_session() as s:
        booth = await s.get(DBBooth, seed["booth_id"])
        booth.transcription_provider = "deepgram"
        event = await s.get(Event, seed["event_id"])
        event.transcription_api_enabled = True

    await go_live(seed["room_id"])
    async with client() as c:
        resp = await c.post(f"{base(seed['room_id'])}/transcription/start")

    assert resp.status_code == 400
    assert "API key" in resp.json()["detail"]


@pytest.mark.anyio
async def test_start_returns_429_when_worker_pool_is_full(seed, monkeypatch):
    """The worker's capacity refusal surfaces as 429 not 500."""
    await go_live(seed["room_id"])

    async def full(*_args, **_kwargs):
        """Refuse the way the worker does at capacity."""
        raise ValueError("System at maximum capacity (10 concurrent transcription booths).")

    monkeypatch.setattr("portal.routers.api_v1.start_transcription_worker", full)

    async with client() as c:
        resp = await c.post(f"{base(seed['room_id'])}/transcription/start")

    assert resp.status_code == 429
    assert "maximum capacity" in resp.json()["detail"]


@pytest.mark.anyio
async def test_start_rejects_unknown_event(seed):
    """An unknown event slug is a 404 rather than a crash."""
    async with client() as c:
        resp = await c.post(f"/api/v1/events/nope/rooms/{seed['room_id']}/booths/en/transcription/start")

    assert resp.status_code == 404


@pytest.mark.anyio
async def test_start_rejects_missing_token(seed):
    """Without a bearer token the route is refused."""
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post(f"{base(seed['room_id'])}/transcription/start")

    assert resp.status_code == 401


@pytest.mark.anyio
async def test_start_never_returns_5xx(seed):
    """Guard the original defect: start answered 500 because it raised TypeError.

    The app is driven with raise_app_exceptions off so an unhandled exception
    arrives as a 500 instead of propagating into the test, which is how a caller
    of the API experiences it.
    """
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    room_id = seed["room_id"]
    await go_live(room_id)

    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
        headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
    ) as c:
        resp = await c.post(f"{base(room_id)}/transcription/start")

    assert resp.status_code < 500
