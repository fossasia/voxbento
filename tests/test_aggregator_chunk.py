from __future__ import annotations

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
    assert len(finals) == 1, "Expected exactly one final caption to be emitted due to word count limit"
    assert finals[0]["text"] == long_text.strip()

    state = aggregator._get_state("booth-1")
    assert state.current_utterance == ""
    assert state.utterance_start_time == 0.0
    assert state.current_word_count == 0


@pytest.mark.anyio
@patch("portal.transcription.aggregator.time.time")
async def test_forced_finalization_at_15_seconds_in_handle_chunk(mock_time):
    """Test that unpunctuated text aging 15s finalizes exactly once and clears state."""
    received = []

    async def fake_callback(booth_id, message):
        """Mock callback to collect broadcasted caption messages."""
        received.append(message)

    aggregator = CaptionAggregator(fake_callback)

    # Start at time 0
    mock_time.return_value = 0.0
    await aggregator.handle_chunk("booth-1", "first chunk ")

    partials = [m for m in received if m.get("status") == "partial"]
    finals = [m for m in received if m.get("status") == "final"]
    assert len(partials) == 1
    assert partials[0]["text"] == "first chunk"
    assert len(finals) == 0

    # Jump 20 seconds into the future
    mock_time.return_value = 20.0
    await aggregator.handle_chunk("booth-1", "second chunk")

    finals = [m for m in received if m.get("status") == "final"]
    assert len(finals) == 1, "Expected exactly one final caption to be emitted due to time limit"
    assert finals[0]["text"] == "first chunk second chunk"

    state = aggregator._get_state("booth-1")
    assert state.current_utterance == ""
    assert state.utterance_start_time == 0.0
    assert state.current_word_count == 0


@pytest.mark.anyio
@patch("portal.transcription.aggregator.time.time")
async def test_handle_chunk_remainder_does_not_inherit_prior_sentence_age(mock_time):
    """Test that when a sentence splits and leaves a remainder, the remainder does not inherit the prior sentence's age."""
    received = []

    async def fake_callback(booth_id, message):
        """Mock callback to collect broadcasted caption messages."""
        received.append(message)

    aggregator = CaptionAggregator(fake_callback)

    # T = 0.0: Start first sentence without punctuation
    mock_time.return_value = 0.0
    await aggregator.handle_chunk("booth-1", "This is the start of a")

    finals = [m for m in received if m.get("status") == "final"]
    assert len(finals) == 0

    # T = 10.0: Sentence completes with punctuation and leaves a remainder
    mock_time.return_value = 10.0
    await aggregator.handle_chunk("booth-1", "sentence with enough words. Here is the remainder")

    # The completed sentence should be finalized
    finals = [m for m in received if m.get("status") == "final"]
    assert len(finals) == 1
    assert finals[0]["text"] == "This is the start of a sentence with enough words."

    # Remainder clock must be reset to T = 10.0 (not inherit T = 0.0)
    state = aggregator._get_state("booth-1")
    assert state.current_utterance == "Here is the remainder"
    assert state.utterance_start_time == 10.0

    # T = 20.0: 20 seconds since T = 0.0, but only 10 seconds since remainder began at T = 10.0
    mock_time.return_value = 20.0
    await aggregator.handle_chunk("booth-1", "still speaking")

    # Remainder must NOT finalize prematurely (10s elapsed < 15s timeout)
    finals = [m for m in received if m.get("status") == "final"]
    assert len(finals) == 1, "Remainder should not have finalized prematurely"

    state = aggregator._get_state("booth-1")
    assert state.current_utterance == "Here is the remainder still speaking"

    # T = 26.0: 16 seconds since remainder began at T = 10.0 (16s elapsed >= 15s timeout)
    mock_time.return_value = 26.0
    await aggregator.handle_chunk("booth-1", "more words")

    finals = [m for m in received if m.get("status") == "final"]
    assert len(finals) == 2, "Remainder should have forcefully finalized after 15s"
    assert finals[1]["text"] == "Here is the remainder still speaking more words"

    # State must be completely cleared after finalization
    state = aggregator._get_state("booth-1")
    assert state.current_utterance == ""
    assert state.utterance_start_time == 0.0
    assert state.current_word_count == 0
