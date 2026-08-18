from __future__ import annotations

import asyncio

import pytest

from portal.transcription.providers.base import AudioFrame, ChunkedProvider, ContinuousProvider, ProviderConfig


class MockAggregator:
    def __init__(self):
        self.chunks = []
        self.clears = []
        self.system_messages = []

    async def handle_chunk(self, booth_id, text):
        self.chunks.append(text)

    async def handle_clear(self, booth_id):
        self.clears.append(booth_id)

    async def broadcast_callback(self, booth_id, msg):
        self.system_messages.append(msg)

async def mock_audio_generator(frames):
    for f in frames:
        yield f
        await asyncio.sleep(0.001)

class DummyContinuousProvider(ContinuousProvider):
    def __init__(self, should_fail=False, max_retries_fail=False, fail_delay=0.0):
        super().__init__()
        self.connected_count = 0
        self.should_fail = should_fail
        self.max_retries_fail = max_retries_fail
        self.fail_delay = fail_delay
        self.received_frames = []

    async def connect_and_stream(self):
        self.connected_count += 1
        if self.should_fail:
            self.should_fail = False
            if self.fail_delay:
                await asyncio.sleep(self.fail_delay)
            raise Exception("Simulated disconnect")
        if self.max_retries_fail:
            raise Exception("Simulated permanent failure")

        while True:
            frame = await self.queue.get()
            if frame is None:
                break
            self.received_frames.append(frame)

class DummyChunkedProvider(ChunkedProvider):
    def __init__(self, processing_delay=0.0):
        super().__init__()
        self.processed_blocks = []
        self.processing_delay = processing_delay

    async def process_block(self, audio_bytes: bytes, start_timestamp: float, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        if self.processing_delay > 0:
            await asyncio.sleep(self.processing_delay)
        self.processed_blocks.append((audio_bytes, start_timestamp))
        return f"transcribed {len(audio_bytes)} bytes"

@pytest.mark.anyio
async def test_chunked_provider_exact_block_size_and_flush():
    provider = DummyChunkedProvider()
    aggregator = MockAggregator()

    gaps = []
    async def notify_gap(start, end):
        gaps.append((start, end))

    frames = []
    for i in range(25):
        frames.append(AudioFrame(data=b"a"*4096, start_timestamp=i*0.128, duration=0.128, seq=i))

    await provider.process_stream(
        mock_audio_generator(frames),
        aggregator,
        notify_gap,
        "en", "test", ProviderConfig(None), "booth1"
    )

    assert len(provider.processed_blocks) == 2
    b1_bytes, b1_ts = provider.processed_blocks[0]
    assert len(b1_bytes) == 24 * 4096
    assert b1_ts == 0.0

    b2_bytes, b2_ts = provider.processed_blocks[1]
    assert len(b2_bytes) == 1 * 4096
    assert b2_ts == 24 * 0.128
    assert len(gaps) == 0

@pytest.mark.anyio
async def test_continuous_provider_reconnect_and_gap():
    # should_fail triggers a failure on first connection attempt,
    # causing it to back off and buffer frames
    provider = DummyContinuousProvider(should_fail=True, fail_delay=0.1)
    aggregator = MockAggregator()

    gaps = []
    async def notify_gap(start, end):
        gaps.append((start, end))

    async def infinite_mock_generator():
        # Produce exactly 15 seconds of audio overall
        # 15s / 0.128s = 118 frames
        for i in range(118):
            yield AudioFrame(data=b"a"*4096, start_timestamp=i*0.128, duration=0.128, seq=i)
            await asyncio.sleep(0.001)

        # Wait until it connects a second time before sending EOF
        while provider.connected_count < 2:
            await asyncio.sleep(0.01)

    await provider.process_stream(
        infinite_mock_generator(),
        aggregator,
        notify_gap,
        "en", "test", ProviderConfig(None), "booth1"
    )

    assert provider.connected_count == 2
    assert len(gaps) == 1

    gap_start, gap_end = gaps[0]
    assert gap_start == 0.0
    # We pushed ~15s of audio, max queue is 10s. So roughly 5s should be dropped.
    assert 4.5 < gap_end < 5.5

@pytest.mark.anyio
async def test_continuous_provider_terminal_error():
    provider = DummyContinuousProvider(max_retries_fail=True)
    aggregator = MockAggregator()

    gaps = []
    async def notify_gap(start, end):
        gaps.append((start, end))

    async def infinite_mock_generator():
        seq = 0
        while True:
            yield AudioFrame(data=b"a"*4096, start_timestamp=seq*0.128, duration=0.128, seq=seq)
            seq += 1
            await asyncio.sleep(0.001)

    # Speed up sleep for test
    original_sleep = asyncio.sleep
    async def fast_sleep(t):
        await original_sleep(0.001)
    asyncio.sleep = fast_sleep

    try:
        await provider.process_stream(
            infinite_mock_generator(),
            aggregator,
            notify_gap,
            "en", "test", ProviderConfig(None), "booth1"
        )
    finally:
        asyncio.sleep = original_sleep

    assert provider.connected_count == 6 # initial + 5 retries
    assert len(aggregator.system_messages) == 1
    assert "transcription failed" in aggregator.system_messages[0]

@pytest.mark.anyio
async def test_chunked_provider_overload_drops_oldest():
    # Make processing extremely slow so it backs up
    provider = DummyChunkedProvider(processing_delay=0.2)
    aggregator = MockAggregator()

    gaps = []
    async def notify_gap(start, end):
        gaps.append((start, end))

    # Push 15 seconds of audio extremely fast
    # 15 / 0.128 = 118 frames
    # That's 5 blocks. Max pending blocks is 3. So it should drop at least 1-2 blocks.
    frames = []
    for i in range(118):
        frames.append(AudioFrame(data=b"a"*4096, start_timestamp=i*0.128, duration=0.128, seq=i))

    await provider.process_stream(
        mock_audio_generator(frames),
        aggregator,
        notify_gap,
        "en", "test", ProviderConfig(None), "booth1"
    )

    assert len(gaps) > 0
    # Block 1 (0.0) is instantly popped and begins processing.
    # Block 2 (3.072), Block 3 (6.144), and Block 4 (9.216) fill the queue.
    # When Block 5 is assembled, Block 2 is dropped.
    assert gaps[0][0] == 3.072
