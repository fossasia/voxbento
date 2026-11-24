"""Exercise actual hosted workers at EOF without live credentials."""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from portal.live_demo import AudioPipe
from portal.transcription.providers import elevenlabs, openai
from portal.transcription.providers.base import ProviderConfig


class Socket:
    def __init__(self, kind: str, respond: bool):
        self.kind = kind
        self.respond = respond
        self.committed = asyncio.Event()
        self.started = asyncio.Event()
        self.sent = []
        self.final_sent = False
        self.readers = 0
        self.closed = False

    async def __aenter__(self):
        self.started.set()
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def send(self, raw):
        data = json.loads(raw)
        self.sent.append(data)
        if data.get("commit") or data.get("type") == "input_audio_buffer.commit":
            self.committed.set()

    async def recv(self):
        return json.dumps({"message_type": "session_started"})

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.readers += 1
        try:
            await self.committed.wait()
            if self.respond and not self.final_sent:
                await asyncio.sleep(0.01)  # No partial: the last words arrive only after EOF.
                self.final_sent = True
                if self.kind == "elevenlabs":
                    return json.dumps({"message_type": "committed_transcript", "text": "The final sentence."})
                return json.dumps(
                    {
                        "type": "conversation.item.input_audio_transcription.completed",
                        "item_id": "last",
                        "transcript": "The final sentence.",
                    }
                )
            await asyncio.Future()  # Commit never closes a realtime connection.
        finally:
            self.readers -= 1


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["elevenlabs", "openai"])
@pytest.mark.parametrize("respond", [True, False])
async def test_eof_drains_delayed_final_and_bounds_an_open_socket(monkeypatch, kind, respond):
    module = elevenlabs if kind == "elevenlabs" else openai
    provider = module.ElevenLabsProvider() if kind == "elevenlabs" else module.OpenAIProvider()
    socket = Socket(kind, respond)
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: socket)
    monkeypatch.setattr(module, "FINAL_TRANSCRIPT_TIMEOUT_SECONDS", 0.05)
    pipe = AudioPipe()
    pipe.write(b"\x01\x01" * 3000)
    pipe.close()
    messages = []

    async def broadcast(_, message):
        messages.append(message)

    before = asyncio.all_tasks()
    await asyncio.wait_for(
        provider.run_stream(pipe, "en", "gpt-4o-realtime-preview", ProviderConfig("key"), broadcast, "demo"), 1
    )
    assert socket.committed.is_set() and socket.closed and socket.readers == 0
    assert any(m.get("text") == "The final sentence." and m.get("status") == "final" for m in messages) == respond
    assert not (asyncio.all_tasks() - before)
    if kind == "elevenlabs":
        assert socket.sent[-1] == {
            "message_type": "input_audio_chunk",
            "audio_base_64": "",
            "commit": True,
            "sample_rate": 16000,
        }


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["elevenlabs", "openai"])
async def test_cancellation_joins_sender_and_receiver(monkeypatch, kind):
    module = elevenlabs if kind == "elevenlabs" else openai
    provider = module.ElevenLabsProvider() if kind == "elevenlabs" else module.OpenAIProvider()
    socket = Socket(kind, False)
    monkeypatch.setattr(websockets, "connect", lambda *args, **kwargs: socket)
    pipe = AudioPipe()

    async def broadcast(*_):
        pass

    before = asyncio.all_tasks()
    task = asyncio.create_task(
        provider.run_stream(pipe, "en", "gpt-4o-realtime-preview", ProviderConfig("key"), broadcast, "demo")
    )
    await socket.started.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed and socket.readers == 0
    assert not (asyncio.all_tasks() - before)
