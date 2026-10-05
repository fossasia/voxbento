from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from portal.transcription.providers.base import ProviderConfig, TranscriptionProvider

# TranscriptionProvider.run_stream reads 3 s of 16 kHz 16-bit mono PCM per chunk.
CHUNK_SIZE = 16000 * 2 * 3

OVERLOAD_MESSAGE = "[Server overloaded - transcription temporarily paused]"
FAILURE_MESSAGE = "[Transcription provider failed. Check logs.]"


def make_chunk(tag: int, size: int = CHUNK_SIZE) -> bytes:
    return bytes([tag]) * size


class PacedStdout:
    """Hands out one chunk per read and sleeps first, so inference keeps up and nothing is dropped."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def readexactly(self, n):
        await asyncio.sleep(0.01)
        if not self._chunks:
            raise asyncio.IncompleteReadError(b"", n)
        chunk = self._chunks.pop(0)
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        return chunk


def burst_stdout(chunks) -> asyncio.StreamReader:
    """All audio is already buffered, so the reader outruns inference."""
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    reader.feed_eof()
    return reader


def fake_process(stdout):
    return SimpleNamespace(stdout=stdout, returncode=None)


class RecordingProvider(TranscriptionProvider):
    """Uses the shared run_stream and records what reaches process_chunk."""

    def __init__(self, results=None):
        self.chunks = []
        self.booth_state = None
        self._results = list(results or [])

    async def process_chunk(self, chunk, language_code, model_variant, config, booth_state=None):
        self.chunks.append(chunk)
        self.booth_state = booth_state
        result = self._results.pop(0) if self._results else ""
        if isinstance(result, Exception):
            raise result
        return result


def system_messages(broadcast):
    return [c.args[1] for c in broadcast.await_args_list if isinstance(c.args[1], str)]


async def run(provider, stdout, broadcast):
    await asyncio.wait_for(
        provider.run_stream(fake_process(stdout), "en", "base", ProviderConfig(None), broadcast, "booth1"),
        timeout=5,
    )


@pytest.mark.anyio
async def test_run_stream_processes_every_chunk_and_flushes_partial_on_eof():
    provider = RecordingProvider()
    broadcast = AsyncMock()
    partial = make_chunk(3, size=1000)
    chunks = [make_chunk(1), make_chunk(2), partial]

    await run(provider, PacedStdout(chunks), broadcast)

    assert provider.chunks == chunks
    assert provider.booth_state.booth_id == "booth1"
    assert provider.booth_state.chunks_dropped_total == 0
    assert system_messages(broadcast) == []


@pytest.mark.anyio
async def test_run_stream_forwards_transcripts_to_captions():
    provider = RecordingProvider(results=["hello world"])
    broadcast = AsyncMock()

    await run(provider, PacedStdout([make_chunk(1)]), broadcast)

    captions = [c.args[1] for c in broadcast.await_args_list if isinstance(c.args[1], dict)]
    assert any("hello world" in caption.get("text", "") for caption in captions)


@pytest.mark.anyio
async def test_run_stream_drops_oldest_chunk_when_inference_lags():
    provider = RecordingProvider()
    broadcast = AsyncMock()
    chunks = [make_chunk(i) for i in range(4)]

    await run(provider, burst_stdout(chunks), broadcast)

    # The queue holds two chunks, so the two oldest are dropped and the newest survive.
    assert provider.chunks == chunks[2:]
    assert provider.booth_state.chunks_dropped_total == 2
    assert OVERLOAD_MESSAGE not in system_messages(broadcast)


@pytest.mark.anyio
async def test_run_stream_pauses_inference_on_sustained_overload():
    provider = RecordingProvider()
    broadcast = AsyncMock()
    chunks = [make_chunk(i) for i in range(6)]

    with patch("portal.transcription.providers.base.asyncio.sleep", new=AsyncMock()) as sleep:
        await run(provider, burst_stdout(chunks), broadcast)

    # More than three drops in a row: the backlog is discarded and inference pauses for 10 s.
    assert provider.chunks == []
    assert system_messages(broadcast) == [OVERLOAD_MESSAGE]
    sleep.assert_awaited_once_with(10)


@pytest.mark.anyio
async def test_run_stream_stops_after_three_consecutive_provider_errors():
    provider = RecordingProvider(results=[RuntimeError("boom")] * 5)
    broadcast = AsyncMock()

    await run(provider, PacedStdout([make_chunk(i) for i in range(5)]), broadcast)

    assert len(provider.chunks) == 3
    assert system_messages(broadcast) == [FAILURE_MESSAGE]


@pytest.mark.anyio
async def test_run_stream_success_resets_provider_error_count():
    boom = RuntimeError("boom")
    provider = RecordingProvider(results=[boom, boom, "", boom, boom])
    broadcast = AsyncMock()

    await run(provider, PacedStdout([make_chunk(i) for i in range(5)]), broadcast)

    assert len(provider.chunks) == 5
    assert FAILURE_MESSAGE not in system_messages(broadcast)
