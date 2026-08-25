import asyncio
import json
import struct

import pytest

from portal.websockets.manager import TTSConnectionManager


class MockWebSocket:
    def __init__(self):
        self.sent_messages = []

    async def send_bytes(self, data):
        self.sent_messages.append(data)


@pytest.mark.anyio
async def test_tts_binary_framing():
    manager = TTSConnectionManager()
    ws = MockWebSocket()

    manager.add(ws, 1, "es", "floor_1")

    # Send seq 1 bundle
    await manager.broadcast_bundle(
        room_id=1,
        language_code="es",
        booth_id="floor_1",
        audio_bytes=b"fakeaudio",
        segment_id="1234-uuid",
        seq=1,
        caption="hello",
        translation="hola",
        error=None,
    )

    assert len(ws.sent_messages) == 1
    frame = ws.sent_messages[0]

    # Verify version byte
    assert frame[0] == 1

    # Verify length header
    json_length = struct.unpack(">I", frame[1:5])[0]

    # Verify JSON content
    header_bytes = frame[5 : 5 + json_length]
    header = json.loads(header_bytes.decode("utf-8"))

    assert header["segment_id"] == "1234-uuid"
    assert header["seq"] == 1
    assert header["caption"] == "hello"
    assert header["translation"] == "hola"
    assert header["error"] is None

    # Verify audio content
    audio = frame[5 + json_length :]
    assert audio == b"fakeaudio"


@pytest.mark.anyio
async def test_tts_manager_does_not_buffer():
    manager = TTSConnectionManager()
    ws = MockWebSocket()

    manager.add(ws, 1, "es", "floor_1")

    # Send seq 2
    await manager.broadcast_bundle(
        room_id=1,
        language_code="es",
        booth_id="floor_1",
        audio_bytes=b"audio2",
        segment_id="uuid-2",
        seq=2,
        caption="world",
        translation="mundo",
        error=None,
    )

    # It should send immediately, frontend handles ordering now
    assert len(ws.sent_messages) == 1
    assert b"audio2" in ws.sent_messages[0]
