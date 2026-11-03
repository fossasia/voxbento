"""Text-level transcript validation (#299 sub-task 6).

Whisper invents speech over silence and room noise. The local provider's
decoder thresholds cut most of it, but short confident phrases still finalise,
so the aggregator drops them before they reach listeners, the stored
transcript, the translation LLM and TTS.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from portal.transcript_processing import (
    MAX_WORD_LENGTH,
    has_no_printable_content,
    has_overlong_word,
    is_hallucination,
)

# ---------------------------------------------------------------------------
# Rejected: invented speech
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Click click click",
        "Cough cough",
        "click click",
        "Click. Click. Click.",
        "yes yes",
    ],
)
def test_repeated_single_word_is_rejected(text):
    assert is_hallucination(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Thank you for watching",
        "thanks for watching",
        "Please subscribe",
        "Subscribe to my channel",
        "[Music]",
        "(applause)",
        "[ Silence ]",
        "Inaudible",
        "Thank you.",
        "THANK YOU VERY MUCH!",
    ],
)
def test_known_subtitle_phrases_are_rejected(text):
    assert is_hallucination(text) is True


@pytest.mark.parametrize("text", ["you", "Yeah", "okay", "Hmm", "uh", "Um.", "Oh!", "mm"])
def test_single_word_fillers_are_rejected(text):
    assert is_hallucination(text) is True


@pytest.mark.parametrize("text", ["", "   ", "\n\t ", "...", "???", "—", "!!!", "​"])
def test_empty_and_unprintable_text_is_rejected(text):
    assert is_hallucination(text) is True


def test_word_longer_than_the_cap_is_rejected():
    assert is_hallucination("a" * (MAX_WORD_LENGTH + 1)) is True


def test_overlong_word_is_rejected_inside_otherwise_valid_speech():
    assert is_hallucination(f"the speaker said {'x' * (MAX_WORD_LENGTH + 1)} just now") is True


# ---------------------------------------------------------------------------
# Kept: real speech
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Hello world, welcome to the conference.",
        "Thank you for joining us today at the conference.",
        "Please subscribe to the newsletter if you want the slides.",
        "The the cat sat on the mat.",
        "Bonjour, bienvenue a la conference.",
        "We will now hear the next speaker.",
        "Okay, so let us move on to the second point.",
    ],
)
def test_real_speech_is_kept(text):
    assert is_hallucination(text) is False


def test_a_phrase_is_only_rejected_when_it_is_the_whole_utterance():
    """"Thank you" alone is invented; the same words inside a sentence are not."""
    assert is_hallucination("Thank you") is True
    assert is_hallucination("Thank you all for coming to this session.") is False


def test_word_at_exactly_the_cap_is_kept():
    assert has_overlong_word("a" * MAX_WORD_LENGTH) is False
    assert has_overlong_word("a" * (MAX_WORD_LENGTH + 1)) is True


def test_non_latin_speech_is_kept():
    assert is_hallucination("これは会議です。") is False
    assert is_hallucination("Привет, добро пожаловать.") is False


@pytest.mark.parametrize("text", ["room 1 room 2", "word0 word1 word2", "booth 3 booth 4 booth 5"])
def test_digits_keep_otherwise_identical_words_distinct(text):
    """Normalising must not strip digits, or these collapse into one repeated word."""
    assert is_hallucination(text) is False


def test_numbers_are_real_content():
    assert is_hallucination("2026") is False
    assert is_hallucination("5 5 5") is True, "a repeated number is still a repetition"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_has_no_printable_content():
    assert has_no_printable_content("...") is True
    assert has_no_printable_content("???") is True
    assert has_no_printable_content("a") is False
    assert has_no_printable_content("1") is False
    assert has_no_printable_content("日") is False


# ---------------------------------------------------------------------------
# Aggregator integration
# ---------------------------------------------------------------------------


def _aggregator(broadcast):
    from portal.transcription.aggregator import CaptionAggregator

    # room_id=None keeps the test off the database: publish_final only saves
    # and translates when a room is attached.
    return CaptionAggregator(broadcast, room_id=None)


@pytest.mark.anyio
async def test_hallucinated_final_is_not_published():
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_final("ev-1-en", "Click click click")

    statuses = [call.args[1]["status"] for call in broadcast.await_args_list]
    assert "final" not in statuses, "an invented transcript must not be published as final"
    assert statuses == ["clear"], "the partial box is cleared instead"


@pytest.mark.anyio
async def test_real_final_is_published():
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_final("ev-1-en", "Hello world, welcome to the conference.")

    finals = [c.args[1] for c in broadcast.await_args_list if c.args[1]["status"] == "final"]
    assert len(finals) == 1
    assert finals[0]["text"] == "Hello world, welcome to the conference."


@pytest.mark.anyio
async def test_a_discarded_final_does_not_consume_a_sequence_number():
    """Listeners order captions by seq, so a dropped utterance must not leave a gap."""
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_final("ev-1-en", "First real sentence here.")
    await agg.handle_final("ev-1-en", "Click click click")
    await agg.handle_final("ev-1-en", "Second real sentence here.")

    seqs = [c.args[1]["seq"] for c in broadcast.await_args_list if c.args[1]["status"] == "final"]
    assert seqs == [1, 2]


@pytest.mark.anyio
async def test_a_discarded_final_resets_the_utterance_state():
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_partial("ev-1-en", "Cough")
    before = agg._get_state("ev-1-en").segment_id
    await agg.handle_final("ev-1-en", "Cough cough")

    state = agg._get_state("ev-1-en")
    assert state.current_utterance == ""
    assert state.current_word_count == 0
    assert state.utterance_start_time == 0.0
    assert state.segment_id != before, "the next utterance starts a new segment"


@pytest.mark.anyio
async def test_handle_clear_does_not_publish_a_hallucinated_pending_partial():
    """Silence finalises whatever is pending; an invented pending phrase is dropped."""
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_partial("ev-1-en", "Thank you")
    broadcast.reset_mock()
    await agg.handle_clear("ev-1-en")

    statuses = [c.args[1]["status"] for c in broadcast.await_args_list]
    assert "final" not in statuses
