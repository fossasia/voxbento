"""Tests for listener join-code rate limiting (Plan 016).

Covers:
- Repeated bad join codes from one client get throttled with 429
- A valid join code succeeds and is not throttled
- A client holding a valid session cookie is never throttled
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"

import pytest

from portal.routers import listener as listener_router

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_db():
    """Set up an in-memory database for the FastAPI app."""
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    """Rate-limit state is a module-level dict shared across tests/clients."""
    listener_router._join_code_attempts.clear()
    yield
    listener_router._join_code_attempts.clear()


@pytest.fixture
async def seed_event():
    """Seed an event with a known join code."""
    from portal.database import create_event, create_room, get_session

    async with get_session() as s:
        event = await create_event(s, slug="testcon", display_name="TestCon 2026")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        event.listener_join_code = "ROOM42"
    return event, room


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client(client_ip: str = "10.0.0.1"):
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app, client=(client_ip, 123)), base_url="http://test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestListenerRateLimit:
    @pytest.mark.anyio
    async def test_repeated_bad_codes_get_throttled(self, seed_event):
        event, _ = seed_event
        async with _client() as c:
            for _ in range(10):
                resp = await c.get(f"/listener/{event.slug}?code=WRONGCODE")
                assert resp.status_code == 200
                assert b"Invalid join code." in resp.content

            resp = await c.get(f"/listener/{event.slug}?code=WRONGCODE")
            assert resp.status_code == 429

    @pytest.mark.anyio
    async def test_throttled_browser_request_renders_429_page(self, seed_event):
        event, _ = seed_event
        async with _client() as c:
            for _ in range(10):
                await c.get(f"/listener/{event.slug}?code=WRONGCODE")

            resp = await c.get(f"/listener/{event.slug}?code=WRONGCODE", headers={"accept": "text/html"})
            assert resp.status_code == 429
            assert b"Too Many Attempts" in resp.content

    @pytest.mark.anyio
    async def test_valid_code_succeeds_and_is_not_throttled(self, seed_event):
        event, _ = seed_event
        async with _client() as c:
            for _ in range(10):
                resp = await c.get(f"/listener/{event.slug}?code=WRONGCODE")
                assert resp.status_code == 200

            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")
            assert resp.status_code == 200
            assert b"Invalid join code." not in resp.content

    @pytest.mark.anyio
    async def test_cookie_holder_is_never_throttled(self, seed_event):
        event, _ = seed_event
        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")
            assert resp.status_code == 200
            assert "listener_code_testcon" in resp.cookies

            for _ in range(15):
                resp = await c.get(f"/listener/{event.slug}")
                assert resp.status_code == 200
                assert b"Invalid join code." not in resp.content

    @pytest.mark.anyio
    async def test_different_clients_are_throttled_independently(self, seed_event):
        event, _ = seed_event
        async with _client("10.0.0.2") as c1, _client("10.0.0.3") as c2:
            for _ in range(11):
                await c1.get(f"/listener/{event.slug}?code=WRONGCODE")

            resp = await c2.get(f"/listener/{event.slug}?code=WRONGCODE")
            assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_audio_delay_endpoint_is_throttled(self, seed_event):
        event, room = seed_event
        async with _client() as c:
            for _ in range(10):
                resp = await c.get(f"/listener/{event.slug}/rooms/{room.id}/audio-delay?code=WRONGCODE")
                assert resp.status_code == 403

            resp = await c.get(f"/listener/{event.slug}/rooms/{room.id}/audio-delay?code=WRONGCODE")
            assert resp.status_code == 429
