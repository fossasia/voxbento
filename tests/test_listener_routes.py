"""
Tests for listener routes.

Covers:
- Missing event
- Join page rendering
- Join code validation
- Cookie creation
- Audio delay endpoint authorization
- Missing room
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"

import pytest

from portal.config import settings

settings.admin_password = "test-admin-pass"


@pytest.fixture(autouse=True)
async def setup_db():
    """Set up an in-memory database for the FastAPI app."""
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()

@pytest.fixture
async def seed_event():
    """Seed an event, room, and booth."""
    from portal.database import create_booth, create_event, create_room, get_session

    async with get_session() as s:
        event = await create_event(s, slug="testcon", display_name="TestCon 2026")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        booth = await create_booth(
            s,
            event_id=event.id,
            room_id=room.id,
            language_code="en",
            language_name="English",
        )
    return event, room, booth


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")



class TestListenerRoutes:

    @pytest.mark.anyio
    async def test_missing_event_returns_404(self):
        async with _client() as c:
            resp = await c.get("/listener/does-not-exist")

        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_listener_without_code_renders_join_form(self, seed_event):
        event, _, _ = seed_event

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}")

        assert resp.status_code == 200
        assert b"Join Event" in resp.content
        assert b"Enter the join code" in resp.content


    @pytest.mark.anyio
    async def test_invalid_join_code_shows_error(self, seed_event):
        event, _, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=WRONG")

        assert resp.status_code == 200
        assert b"Invalid join code." in resp.content


    @pytest.mark.anyio
    async def test_valid_join_code_sets_cookie(self, seed_event):
        event, _, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(f"/listener/{event.slug}?code=ROOM42")

        assert resp.status_code == 200
        assert "listener_code_testcon" in resp.headers["set-cookie"]


    @pytest.mark.anyio
    async def test_audio_delay_requires_listener_access(self, seed_event):
        event, room, _ = seed_event

        async with _client() as c:
            resp = await c.get(
                f"/listener/{event.slug}/rooms/{room.id}/audio-delay"
            )

        assert resp.status_code == 403


    @pytest.mark.anyio
    async def test_audio_delay_unknown_room_returns_404(self, seed_event):
        event, _, _ = seed_event

        from portal.database import get_session

        async with get_session() as s:
            db_event = await s.get(type(event), event.id)
            db_event.listener_join_code = "ROOM42"

        async with _client() as c:
            resp = await c.get(
                f"/listener/{event.slug}/rooms/9999/audio-delay?code=ROOM42"
            )

        assert resp.status_code == 404


