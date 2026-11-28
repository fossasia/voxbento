"""A refused /ws/tts connection reaches the browser with its close code.

A close before accept() is sent as an HTTP 403, and browsers report that as
close code 1006, the same as a dropped connection. The listener page stops
reconnecting its translation feed only on 4001/4003, so the refusal has to
arrive as a real close frame. The test client hides the difference, so these
tests use a real server and a real WebSocket client.
"""

from __future__ import annotations

import asyncio
import socket
import time
from contextlib import asynccontextmanager

import pytest
import uvicorn
import websockets

from fastapi_app import app
from portal.database import configure, create_event, create_room, dispose, get_session, init_db
from portal.join_code_limit import JOIN_CODE_RATE_LIMIT, _join_code_attempts

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def database(tmp_path):
    configure(f"sqlite+aiosqlite:///{tmp_path / 'tts-refusal.db'}")
    await init_db()
    yield
    await dispose()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def _server():
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def _feed_url(base: str) -> str:
    """The French feed of a floor booth in an event that needs the join code JOIN1234."""
    async with get_session() as session:
        event = await create_event(session, slug="tts-refusal", display_name="TTS refusal")
        event.listener_join_code = "JOIN1234"
        room = await create_room(session, event_id=event.id, display_name="Main")
        return f"{base}/ws/tts/{room.id}/fr/tts-refusal-{room.id}-floor"


async def test_listener_without_access_gets_4003_not_an_http_403():
    async with _server() as base:
        url = await _feed_url(base)
        async with websockets.connect(url, open_timeout=5) as ws:
            with pytest.raises(websockets.ConnectionClosed) as closed:
                await asyncio.wait_for(ws.recv(), timeout=5)
    assert closed.value.rcvd.code == 4003


async def test_listener_with_the_join_code_stays_connected():
    async with _server() as base:
        url = await _feed_url(base)
        headers = {"Cookie": "listener_code_tts-refusal=JOIN1234"}
        async with websockets.connect(url, additional_headers=headers, open_timeout=5) as ws:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), timeout=0.5)
            assert ws.state is websockets.protocol.State.OPEN


async def test_throttled_join_code_gets_1008_so_the_listener_retries():
    # Wrong guesses from others on the same venue network can use up the budget for a
    # listener who has the right code. That clears on its own, so it must not be a 4003.
    _join_code_attempts.clear()
    try:
        async with _server() as base:
            url = await _feed_url(base)
            _join_code_attempts["127.0.0.1"] = (JOIN_CODE_RATE_LIMIT, time.monotonic())
            headers = {"Cookie": "listener_code_tts-refusal=JOIN1234"}
            async with websockets.connect(url, additional_headers=headers, open_timeout=5) as ws:
                with pytest.raises(websockets.ConnectionClosed) as closed:
                    await asyncio.wait_for(ws.recv(), timeout=5)
    finally:
        _join_code_attempts.clear()
    assert closed.value.rcvd.code == 1008
