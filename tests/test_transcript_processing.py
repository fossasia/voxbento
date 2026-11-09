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
    _normalise,
    hallucination_reason,
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
        " ".join(["Click"] * 10),
        " ".join(["cough"] * 15),
        ". ".join(["Click"] * 12),
        " ".join(["yes"] * 10),
    ],
)
def test_long_repetition_is_rejected(text):
    """Only the long runs are now treated as structural garbage. Short
    repetitions like 'no no no' or 'yes yes' are left alone, since real
    speech uses them as emphasis and rejecting them on word count misses
    more than it catches."""
    assert is_hallucination(text) is True


@pytest.mark.parametrize("text", ["Click click click", "Cough cough", "yes yes", "no no no no"])
def test_short_repetition_is_kept_as_possible_real_speech(text):
    """Previously rejected at 2+ reps, which cleared legitimate emphasis."""
    assert is_hallucination(text) is False


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


# ---------------------------------------------------------------------------
# Scripts written without spaces between words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "这是一个关于人工智能和机器翻译的非常重要的会议演讲内容今天我们将讨论很多有趣的技术话题和研究成果",
        "これは人工知能と機械翻訳に関する非常に重要な会議の講演内容です今日は多くの話題について議論します",
        "นี่คือการประชุมที่สำคัญมากเกี่ยวกับปัญญาประดิษฐ์และการแปลภาษาด้วยเครื่อง",
    ],
)
def test_long_unspaced_script_speech_is_kept(text):
    """A whole sentence is one token in these scripts, so the cap must not apply."""
    assert len(text) > MAX_WORD_LENGTH
    assert has_overlong_word(text) is False
    assert is_hallucination(text) is False


def test_latin_garbage_is_still_capped_alongside_unspaced_text():
    assert has_overlong_word("x" * (MAX_WORD_LENGTH + 1)) is True


@pytest.mark.parametrize("ch", ["你", "の", "ก"])
def test_single_character_repetition_in_unspaced_scripts_is_rejected(ch):
    """The whole sentence is one token in these scripts, so the word-level
    repetition check cannot see it; a dedicated character-level check must."""
    text = ch * 50
    assert is_hallucination(text) is True
    assert is_hallucination(text, complete_turn=False) is True


def test_short_single_character_runs_in_unspaced_scripts_still_pass():
    """Legitimate speech never actually looks like this, but the threshold
    matches the word-level rule for consistency."""
    assert is_hallucination("你" * 5) is False


def test_combining_marks_survive_normalisation():
    """Stripping marks mangles Devanagari and Thai and can fake a repeated word."""
    hindi = "यह सम्मेलन कृत्रिम बुद्धिमत्ता के बारे में है"
    assert _normalise(hindi) == hindi
    assert is_hallucination(hindi) is False


def test_words_differing_only_by_marks_stay_distinct():
    thai = "ที่ ทิ"
    assert len(set(_normalise(thai).split())) == 2
    assert is_hallucination(thai) is False


@pytest.mark.parametrize("text", ["room 1 room 2", "word0 word1 word2", "booth 3 booth 4 booth 5"])
def test_digits_keep_otherwise_identical_words_distinct(text):
    """Normalising must not strip digits, or these collapse into one repeated word."""
    assert is_hallucination(text) is False


def test_numbers_are_real_content():
    assert is_hallucination("2026") is False
    assert is_hallucination(" ".join(["5"] * 10)) is True, "a long number repetition is still a repetition"


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

    await agg.handle_final("ev-1-en", " ".join(["Click"] * 12))

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
    await agg.handle_final("ev-1-en", " ".join(["Click"] * 12))
    await agg.handle_final("ev-1-en", "Second real sentence here.")

    seqs = [c.args[1]["seq"] for c in broadcast.await_args_list if c.args[1]["status"] == "final"]
    assert seqs == [1, 2]


@pytest.mark.anyio
async def test_a_discarded_final_resets_the_utterance_state():
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_partial("ev-1-en", "Cough")
    before = agg._get_state("ev-1-en").segment_id
    await agg.handle_final("ev-1-en", " ".join(["Cough"] * 12))

    state = agg._get_state("ev-1-en")
    assert state.current_utterance == ""
    assert state.current_word_count == 0
    assert state.utterance_start_time == 0.0
    assert state.segment_id != before, "the next utterance starts a new segment"


# ---------------------------------------------------------------------------
# Partial turns (Deepgram is_final before speech_final)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["you", "thank you", "uh"])
def test_mid_turn_fragments_keep_short_phrases(text):
    """A bare is_final can finalise part of a turn, so one short phrase is
    legitimate mid-turn. A complete turn with the same text is rejected as a
    subtitle phrase or filler."""
    assert is_hallucination(text) is True
    assert is_hallucination(text, complete_turn=False) is False


@pytest.mark.parametrize("text", ["a" * (MAX_WORD_LENGTH + 1), "...", "   "])
def test_mid_turn_fragments_still_drop_structural_garbage(text):
    assert is_hallucination(text, complete_turn=False) is True


@pytest.mark.parametrize("word", ["you", "the", "I"])
def test_a_long_single_word_repetition_is_structural_garbage(word):
    """Fifty repetitions survived forced finalization before this was lifted
    above the complete_turn gate."""
    text = " ".join([word] * 50)
    assert hallucination_reason(text, complete_turn=False) == "repeated word"
    assert hallucination_reason(text, complete_turn=True) == "repeated word"


@pytest.mark.parametrize("text", ["you", "you you", "you you you", "I I I"])
def test_a_short_repetition_is_still_allowed_mid_turn(text):
    """Three or fewer identical words mid-turn can be legitimate speech
    (stutters, "you you"), so the structural check keeps a looser bound than
    the complete-turn one."""
    assert hallucination_reason(text, complete_turn=False) is None


@pytest.mark.anyio
async def test_deepgram_mid_turn_chunk_is_published():
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_final("ev-1-en", "thank you", complete_turn=False)

    finals = [c.args[1] for c in broadcast.await_args_list if c.args[1]["status"] == "final"]
    assert len(finals) == 1, "a mid-turn fragment of real speech must survive"


@pytest.mark.anyio
async def test_deepgram_complete_turn_is_still_filtered():
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_final("ev-1-en", "thank you", complete_turn=True)

    statuses = [c.args[1]["status"] for c in broadcast.await_args_list]
    assert "final" not in statuses


@pytest.mark.anyio
async def test_a_forced_finalization_is_not_judged_as_a_whole_turn():
    """Crossing the time cap cuts a turn; it does not end one.

    The fragment left in the buffer is mid-sentence, so the whole-utterance
    rules must not reject it as invented speech.
    """
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_partial("ev-1-en", "thank you")
    # Age the utterance past the 15s forced-finalization threshold.
    agg.states["ev-1-en"].utterance_start_time -= 20.0
    await agg.handle_partial("ev-1-en", "thank you")

    finals = [c.args[1] for c in broadcast.await_args_list if c.args[1]["status"] == "final"]
    assert [f["text"] for f in finals] == ["thank you"]


@pytest.mark.anyio
async def test_a_deliberate_end_of_turn_is_still_filtered():
    """The counterpart: the same text really ending a turn is still dropped."""
    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    await agg.handle_partial("ev-1-en", "thank you")
    await agg.handle_clear("ev-1-en")

    statuses = [c.args[1]["status"] for c in broadcast.await_args_list]
    assert "final" not in statuses


def test_deepgram_passes_speech_final_as_the_turn_flag():
    """The bare is_final branch must not be filtered as a complete turn."""
    import inspect

    from portal.transcription.providers import deepgram

    src = inspect.getsource(deepgram)
    assert "complete_turn=speech_final" in src


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_reason_names_a_category_and_not_the_text():
    secret = "Thanks for watching"
    reason = hallucination_reason(secret)
    assert reason == "subtitle phrase"
    assert secret not in reason


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "empty"),
        ("...", "no printable content"),
        ("a" * (MAX_WORD_LENGTH + 1), "overlong word"),
        ("Thank you", "subtitle phrase"),
        (" ".join(["Click"] * 12), "repeated word"),
        ("Hello world.", None),
    ],
)
def test_reason_categories(text, expected):
    assert hallucination_reason(text) == expected


@pytest.mark.anyio
async def test_discarded_transcript_text_is_not_logged(caplog):
    """The classifier can be wrong, so private speech must not reach the logs."""
    import logging

    broadcast = AsyncMock()
    agg = _aggregator(broadcast)

    with caplog.at_level(logging.INFO, logger="portal.transcription.aggregator"):
        await agg.handle_final("ev-1-en", "Thanks for watching")

    assert caplog.records, "the drop is still recorded"
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "Thanks for watching" not in logged
    assert "subtitle phrase" in logged


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
