from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.models import FLOOR_SOURCE_JITSI_BOT, FLOOR_SOURCE_PROGRAM_INGEST


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


async def seed(mode: str, offset_ms: int = 4000):
    from portal.database import create_event, create_room, get_session

    async with get_session() as s:
        ev = await create_event(s, slug="progcon", display_name="ProgCon")
        ev.listener_join_code = "JOIN"
        room = await create_room(s, event_id=ev.id, display_name="Main")
        room.floor_transcription_enabled = True
        room.floor_source_mode = mode
        room.program_sync_offset_ms = offset_ms
        await s.flush()
        return room.id


async def listener_data(ensure: AsyncMock) -> dict:
    with patch("portal.routers.listener._ensure_mediamtx_path", ensure):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/listener/progcon?code=JOIN")
    assert resp.status_code == 200
    match = re.search(r'<script id="listener-data" type="application/json">(.*?)</script>', resp.text, re.S)
    return json.loads(match.group(1))


@pytest.mark.anyio
async def test_program_ingest_floor_exposes_sync_offset_and_skips_alwaysavailable():
    room_id = await seed(FLOOR_SOURCE_PROGRAM_INGEST)
    ensure = AsyncMock()
    data = await listener_data(ensure)
    floor = next(b for b in data["booths"] if b["id"] == f"floor_{room_id}")
    assert floor["sync_offset_ms"] == 4000
    assert floor["whep_url"].endswith(f"/progcon/{room_id}/floor/whep")
    ensure.assert_not_awaited()


@pytest.mark.anyio
async def test_bot_floor_has_no_sync_offset_and_keeps_alwaysavailable():
    room_id = await seed(FLOOR_SOURCE_JITSI_BOT)
    ensure = AsyncMock()
    data = await listener_data(ensure)
    floor = next(b for b in data["booths"] if b["id"] == f"floor_{room_id}")
    assert floor["sync_offset_ms"] == 0
    ensure.assert_awaited_once_with(f"progcon/{room_id}/floor")
