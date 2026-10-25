"""Transcript routes must not serve rooms that belong to a different event (#812)."""

from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app

os.environ["BOOTH_ACCESS_TOKEN"] = ""

from portal.auth import create_user_token


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _setup():
    """Two events, each with one room; the user owns only event A."""
    from portal.auth import hash_password
    from portal.database import create_event, create_room, create_user, get_session
    from portal.models import EventMembership

    async with get_session() as s:
        user = await create_user(s, email="owner@test.com", display_name="Owner", password_hash=hash_password("pw"))
        ev_a = await create_event(s, slug="ev-a", display_name="Event A")
        ev_b = await create_event(s, slug="ev-b", display_name="Event B")
        room_a = await create_room(s, event_id=ev_a.id, display_name="Room A")
        room_b = await create_room(s, event_id=ev_b.id, display_name="Room B")
        s.add(EventMembership(user_id=user.id, event_id=ev_a.id, role="event_owner"))
        await s.commit()
        token = create_user_token(user_id=user.id, email=user.email)
        return token, ev_a.id, room_a.id, room_b.id


@pytest.mark.anyio
async def test_api_transcripts_rejects_room_from_other_event(client):
    token, ev_a, room_a, room_b = await _setup()
    client.cookies.set("user_token", token)

    r = await client.get(f"/api/workspace/events/{ev_a}/rooms/{room_b}/transcripts/en")
    assert r.status_code == 404

    r = await client.get(f"/api/workspace/events/{ev_a}/rooms/{room_a}/transcripts/en")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.anyio
async def test_transcripts_page_rejects_room_from_other_event(client):
    token, ev_a, room_a, room_b = await _setup()
    client.cookies.set("user_token", token)

    r = await client.get(f"/workspace/events/{ev_a}/rooms/{room_b}/transcripts/")
    assert r.status_code == 404

    r = await client.get(f"/workspace/events/{ev_a}/rooms/{room_a}/transcripts/")
    assert r.status_code == 200


@pytest.mark.anyio
async def test_transcripts_page_loads_external_module_and_exposes_ids(client):
    """The viewer must not use inline scripts/handlers and must pass ids via data attributes (#904)."""
    token, ev_a, room_a, _room_b = await _setup()
    client.cookies.set("user_token", token)

    r = await client.get(f"/workspace/events/{ev_a}/rooms/{room_a}/transcripts/")

    assert r.status_code == 200
    assert "js/room-transcripts.js" in r.text
    assert f'data-event-id="{ev_a}"' in r.text
    assert f'data-room-id="{room_a}"' in r.text
    assert 'onclick="fetchTranscripts()"' not in r.text
    assert "function fetchTranscripts" not in r.text
