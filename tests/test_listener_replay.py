from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock

import pytest

from portal.websockets.manager import ListenerConnectionManager, TTSConnectionManager, purge_replay


@pytest.mark.anyio
class TestListenerReplay:
    async def test_listener_manager_records_and_sends_replay(self):
        manager = ListenerConnectionManager(max_replay_per_booth=15, replay_ttl_seconds=600.0)
        booth_id = "event-1-en"

        # Partial should not be recorded
        await manager.broadcast(booth_id, {"type": "caption", "status": "partial", "text": "partial text"})
        assert len(manager._replay_buffers.get(booth_id, [])) == 0

        # Final caption should be recorded
        await manager.broadcast(
            booth_id, {"type": "caption", "status": "final", "text": "Sentence 1", "seq": 1, "segment_id": "seg-1"}
        )
        await manager.broadcast(
            booth_id, {"type": "caption", "status": "final", "text": "Sentence 2", "seq": 2, "segment_id": "seg-2"}
        )

        assert len(manager._replay_buffers[booth_id]) == 2

        # Mock websocket
        ws = AsyncMock()
        await manager.send_replay(ws, booth_id)

        assert ws.send_text.call_count == 2
        first_call = json.loads(ws.send_text.call_args_list[0][0][0])
        assert first_call["replay"] is True
        assert first_call["text"] == "Sentence 1"
        assert first_call["seq"] == 1

        second_call = json.loads(ws.send_text.call_args_list[1][0][0])
        assert second_call["replay"] is True
        assert second_call["text"] == "Sentence 2"

    async def test_listener_manager_ring_buffer_maxlen(self):
        manager = ListenerConnectionManager(max_replay_per_booth=3, replay_ttl_seconds=600.0)
        booth_id = "event-1-en"

        for i in range(5):
            await manager.broadcast(
                booth_id, {"type": "caption", "status": "final", "text": f"S{i}", "seq": i, "segment_id": f"s-{i}"}
            )

        assert len(manager._replay_buffers[booth_id]) == 3
        # Oldest two should have been evicted
        texts = [item[1]["text"] for item in manager._replay_buffers[booth_id]]
        assert texts == ["S2", "S3", "S4"]

    async def test_listener_manager_ttl_expiry(self):
        manager = ListenerConnectionManager(max_replay_per_booth=15, replay_ttl_seconds=1.0)
        booth_id = "event-1-en"

        # Manually insert expired entry
        manager._replay_buffers[booth_id] = manager._replay_buffers.get(booth_id, [])
        from collections import deque

        buf = deque(maxlen=15)
        buf.append((time.time() - 10.0, {"type": "caption", "status": "final", "text": "Old sentence"}))
        buf.append((time.time(), {"type": "caption", "status": "final", "text": "New sentence"}))
        manager._replay_buffers[booth_id] = buf

        ws = AsyncMock()
        await manager.send_replay(ws, booth_id)

        assert ws.send_text.call_count == 1
        call_msg = json.loads(ws.send_text.call_args[0][0])
        assert call_msg["text"] == "New sentence"

    async def test_tts_manager_records_and_sends_replay(self):
        manager = TTSConnectionManager(max_replay_per_channel=15, replay_ttl_seconds=600.0)
        room_id = 1
        lang_code = "fr"
        booth_id = "event-1-floor"

        await manager.broadcast_bundle(
            room_id, lang_code, booth_id, b"\x00\x01\x02", "seg-1", 1, caption="Hello", translation="Bonjour"
        )

        ws = AsyncMock()
        await manager.send_replay(ws, room_id, lang_code, booth_id)

        assert ws.send_bytes.call_count == 1
        frame = ws.send_bytes.call_args[0][0]
        # Frame has 1 byte version + 4 byte len + json header
        header_len = int.from_bytes(frame[1:5], "big")
        header = json.loads(frame[5 : 5 + header_len].decode("utf-8"))
        assert header["replay"] is True
        assert header["audio_pending"] is False
        assert header["translation"] == "Bonjour"

    async def test_purge_replay_by_event_room_booth(self):
        l_manager = ListenerConnectionManager()
        t_manager = TTSConnectionManager()

        import portal.websockets.manager as wm

        orig_l = wm.listener_manager
        orig_t = wm.tts_manager
        try:
            wm.listener_manager = l_manager
            wm.tts_manager = t_manager

            await l_manager.broadcast("eventa-1-en", {"type": "caption", "status": "final", "text": "A"})
            await l_manager.broadcast("eventb-2-fr", {"type": "caption", "status": "final", "text": "B"})
            await t_manager.broadcast_bundle(1, "es", "eventa-1-floor", b"", "seg-1", 1, "Cap", "Trans")

            assert "eventa-1-en" in l_manager._replay_buffers
            assert "eventb-2-fr" in l_manager._replay_buffers

            # Purge eventa
            purge_replay(event_slug="eventa")
            assert "eventa-1-en" not in l_manager._replay_buffers
            assert "eventb-2-fr" in l_manager._replay_buffers

            # Purge room 2
            purge_replay(room_id=2)
            assert "eventb-2-fr" not in l_manager._replay_buffers
        finally:
            wm.listener_manager = orig_l
            wm.tts_manager = orig_t
