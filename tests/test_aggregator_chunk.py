import asyncio
from unittest.mock import patch

import pytest

from portal.transcription.aggregator import CaptionAggregator


@pytest.mark.anyio
async def test_forced_finalization_at_50_words_in_handle_chunk():
    """Test that handle_chunk forcefully finalizes an utterance when it exceeds 50 words without punctuation."""
    received = []

    async def fake_callback(booth_id, message):
        """Mock callback to collect broadcasted caption messages."""
        received.append(message)

    aggregator = CaptionAggregator(fake_callback)

    # 60 words with no punctuation
    long_text = "word " * 60

    await aggregator.handle_chunk("booth-1", long_text)

    finals = [m for m in received if m.get("status") == "final"]

    # Verify that the chunk is forcefully finalized after exceeding 50 words without punctuation
    assert len(finals) > 0, "Expected a final caption to be emitted due to word count limit"


@pytest.mark.anyio
@patch("time.time")
async def test_forced_finalization_at_15_seconds_in_handle_chunk(mock_time):
    """Test that handle_chunk forcefully finalizes an utterance when 15 seconds have passed since the start."""
    received = []

    async def fake_callback(booth_id, message):
        """Mock callback to collect broadcasted caption messages."""
        received.append(message)

    aggregator = CaptionAggregator(fake_callback)

    # Start at time 0
    mock_time.return_value = 0.0
    await aggregator.handle_chunk("booth-1", "first chunk ")

    # Jump 20 seconds into the future
    mock_time.return_value = 20.0
    await aggregator.handle_chunk("booth-1", "second chunk")

    finals = [m for m in received if m.get("status") == "final"]

    # Verify that the chunk is forcefully finalized after 15 seconds have passed
    assert len(finals) > 0, "Expected a final caption to be emitted due to time limit"
