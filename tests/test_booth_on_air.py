"""Listener-safe ``on_air`` status read from MediaMTX (#1397, part 1)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from portal import booth_on_air

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def unknown_paths():
    booth_on_air.live_paths = None
    booth_on_air.path_list_outage.failures = 0
    yield
    booth_on_air.live_paths = None
    booth_on_air.path_list_outage.failures = 0


LIVE = {"broadcast_unlocked": True, "ingest_status": "connected", "mediamtx_path": "floorcon/1/th"}


@pytest.mark.parametrize(
    ("state", "paths", "expected"),
    [
        (LIVE, None, True),
        (LIVE, frozenset({"floorcon/1/th"}), True),
        (LIVE, frozenset({"floorcon/1/de"}), False),
        ({**LIVE, "broadcast_unlocked": False}, None, False),
        ({**LIVE, "broadcast_unlocked": "true"}, None, False),
        ({**LIVE, "ingest_status": "disconnected"}, None, False),
        # A failed transcription marks the booth overloaded; the interpreter is still on air.
        ({**LIVE, "ingest_status": "overloaded"}, None, True),
        ({**LIVE, "ingest_status": "overloaded"}, frozenset(), False),
        ({}, None, False),
    ],
)
async def test_is_on_air(state, paths, expected):
    booth_on_air.live_paths = paths
    assert booth_on_air.is_on_air(state) is expected


async def test_listener_state_keys_are_exact():
    from portal.websockets.manager import listener_booth_state

    full = {
        **LIVE,
        "booth_id": "floorcon-1-th",
        "event_slug": "floorcon",
        "room_id": 1,
        "language_code": "th",
        "participants": [{"display_name": "Eve"}],
        "chat_messages": [{"body": "booth-only"}],
        "handoff_state": "offered",
        "on_air": False,
    }
    state = listener_booth_state(full)
    assert set(state) == {
        "booth_id",
        "event_slug",
        "room_id",
        "language_code",
        "broadcast_unlocked",
        "ingest_status",
        "on_air",
    }
    assert state["on_air"] is True, "on_air is computed on the server, never copied from the input"


def _media(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_refresh_reads_sources_from_one_path_list(monkeypatch):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "name": "floorcon/1/th",
                        "ready": True,
                        "online": True,
                        "source": {"type": "webRTCSession", "id": "x"},
                    },
                    # alwaysAvailable keeps an empty path ready, online false, source null.
                    {"name": "floorcon/1/de", "ready": True, "online": False, "source": None, "inboundBytes": 9975},
                    {"name": 7, "source": {"type": "rtspSession"}},
                    "junk",
                ]
            },
        )

    async with _media(handler) as client:
        monkeypatch.setattr(booth_on_air, "get_http_client", lambda: client)
        await booth_on_air.refresh_live_paths()
    assert booth_on_air.live_paths == frozenset({"floorcon/1/th"})
    assert len(calls) == 1 and calls[0].endswith("/v3/paths/list?itemsPerPage=1000")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"items": "nope"}),
        httpx.Response(200, json=["not", "an", "object"]),
    ],
)
async def test_unreadable_path_list_means_unknown(monkeypatch, response):
    booth_on_air.live_paths = frozenset({"floorcon/1/th"})
    async with _media(lambda request: response) as client:
        monkeypatch.setattr(booth_on_air, "get_http_client", lambda: client)
        await booth_on_air.refresh_live_paths()
    assert booth_on_air.live_paths is None, "unknown falls back to the interpreter's own report"


async def test_media_outage_logged_once(monkeypatch, caplog):
    outage = True

    def handler(request):
        if outage:
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"items": []})

    caplog.set_level(logging.WARNING, logger="portal.booth_on_air")
    async with _media(handler) as client:
        monkeypatch.setattr(booth_on_air, "get_http_client", lambda: client)
        for _ in range(3):
            await booth_on_air.refresh_live_paths()
        outage = False
        await booth_on_air.refresh_live_paths()
    lines = [r.getMessage() for r in caplog.records if r.name == "portal.booth_on_air"]
    assert lines[0] == "booth_on_air path_list_unavailable"
    assert len(lines) == 2 and lines[1].startswith("booth_on_air path_list_unavailable recovered failures=3")
    assert booth_on_air.live_paths == frozenset()


class _RecordingWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))


async def _live_booth(booth_id: str, *, mic_active: bool = True) -> None:
    from portal.globals import booths

    participant, _ = await booths.join_participant(
        booth_id=booth_id, display_name="Eve", role="interpreter", language="Thai", channel_id=f"{booth_id}-audio"
    )
    await booths.set_broadcast_unlocked(booth_id, True, "Thai", f"{booth_id}-audio")
    await booths.update_participant_state(
        booth_id, participant.participant_id, "Thai", f"{booth_id}-audio", mic_active=mic_active, ingest_connected=True
    )


async def test_listeners_told_when_the_stream_goes_away_and_comes_back():
    from portal.websockets.manager import listener_manager

    booth = "onair-1-th"
    await _live_booth(booth)
    ws = _RecordingWebSocket()
    listener_manager.add(ws, booth)
    try:
        await listener_manager.send_booth_state(ws, booth)
        assert ws.sent[-1]["state"]["on_air"] is True

        booth_on_air.live_paths = frozenset()
        await listener_manager.refresh_booth_states()
        assert ws.sent[-1]["state"]["on_air"] is False, "a stream that died while the booth says connected (#1357)"

        await listener_manager.refresh_booth_states()
        assert len(ws.sent) == 2, "an unchanged status is not sent again"

        booth_on_air.live_paths = frozenset({"onair/1/th"})
        await listener_manager.refresh_booth_states()
        assert ws.sent[-1]["state"]["on_air"] is True
        assert all("participants" not in m["state"] and "chat_messages" not in m["state"] for m in ws.sent)
    finally:
        listener_manager.remove(ws, booth)


async def test_mic_mute_keeps_the_booth_on_air():
    from portal.globals import booths
    from portal.websockets.manager import listener_booth_state

    booth = "onair-1-de"
    await _live_booth(booth, mic_active=False)
    assert listener_booth_state(booths.get_booth_sync(booth).as_public_dict())["on_air"] is True


async def test_refresh_skips_listeners_of_unknown_booths():
    from portal.websockets.manager import listener_manager

    ws = _RecordingWebSocket()
    listener_manager.add(ws, "nobody-1-th")
    try:
        await listener_manager.refresh_booth_states()
    finally:
        listener_manager.remove(ws, "nobody-1-th")
    assert ws.sent == []


async def test_on_air_loop_reads_media_only_while_someone_listens(monkeypatch):
    from portal.websockets import manager

    reads = []

    async def refresh():
        reads.append(1)
        booth_on_air.live_paths = frozenset()

    async def stop_after_one_tick(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "refresh_live_paths", refresh)
    monkeypatch.setattr(manager.asyncio, "sleep", stop_after_one_tick)

    ws = _RecordingWebSocket()
    manager.listener_manager.add(ws, "loop-1-th")
    try:
        with pytest.raises(asyncio.CancelledError):
            await manager.on_air_loop()
    finally:
        manager.listener_manager.remove(ws, "loop-1-th")
    assert reads == [1] and booth_on_air.live_paths == frozenset()

    with pytest.raises(asyncio.CancelledError):
        await manager.on_air_loop()
    assert reads == [1], "no listeners, no MediaMTX read"
    assert booth_on_air.live_paths is None, "a stale list is forgotten"
