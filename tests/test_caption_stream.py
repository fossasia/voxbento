"""The buffering rules, tested without a database, broadcast or worker queue.

Every test here is synchronous and constructs nothing but a CaptionStream —
which is the point of #352. Before the split these rules could only be reached
through CaptionAggregator, so exercising them meant tolerating its side effects.
"""

from __future__ import annotations

import pytest

from portal.transcription.caption_stream import (
    MAX_UTTERANCE_SECONDS,
    MAX_UTTERANCE_WORDS,
    MIN_SENTENCE_WORDS,
    CaptionStream,
    ClearCaption,
    FinalCaption,
    PartialCaption,
)


class FakeClock:
    """A clock the test moves by hand, so timing rules are deterministic."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def stream(clock) -> CaptionStream:
    ids = iter(f"seg-{n}" for n in range(1, 1000))
    return CaptionStream(clock=clock, new_segment_id=lambda: next(ids))


def texts(events) -> list[str]:
    return [getattr(event, "text", "") for event in events]


class TestPartial:
    def test_a_partial_is_emitted_for_in_progress_text(self, stream):
        events = stream.partial("b", "hello world")
        assert len(events) == 1
        assert isinstance(events[0], PartialCaption)
        assert events[0].text == "hello world"

    def test_whitespace_only_text_emits_nothing(self, stream):
        assert stream.partial("b", "   ") == []

    def test_a_later_partial_replaces_the_buffer(self, stream):
        stream.partial("b", "hello")
        events = stream.partial("b", "hello world")
        assert events[0].text == "hello world"
        assert stream.state_for("b").current_utterance == "hello world"

    def test_the_segment_id_is_stable_across_partials(self, stream):
        first = stream.partial("b", "hello")[0]
        second = stream.partial("b", "hello world")[0]
        assert first.segment_id == second.segment_id

    def test_the_first_partial_is_not_forced_by_the_clock(self, stream, clock):
        # The utterance starts now, so no time has elapsed against it yet.
        clock.advance(10_000)
        events = stream.partial("b", "hello")
        assert isinstance(events[0], PartialCaption)


class TestForcedFinalisation:
    def test_an_utterance_is_forced_at_the_word_cap(self, stream):
        events = stream.partial("b", " ".join(f"word{n}" for n in range(MAX_UTTERANCE_WORDS)))
        assert isinstance(events[0], FinalCaption)

    def test_an_utterance_just_under_the_word_cap_stays_partial(self, stream):
        events = stream.partial("b", " ".join(f"word{n}" for n in range(MAX_UTTERANCE_WORDS - 1)))
        assert isinstance(events[0], PartialCaption)

    def test_an_utterance_is_forced_once_it_runs_too_long(self, stream, clock):
        stream.partial("b", "still going")
        clock.advance(MAX_UTTERANCE_SECONDS)
        events = stream.partial("b", "still going and going")
        assert isinstance(events[0], FinalCaption)
        assert events[0].text == "still going and going"

    def test_an_utterance_under_the_time_cap_stays_partial(self, stream, clock):
        stream.partial("b", "still going")
        clock.advance(MAX_UTTERANCE_SECONDS - 0.1)
        assert isinstance(stream.partial("b", "still going on")[0], PartialCaption)


class TestChunk:
    def test_a_long_enough_sentence_is_settled(self, stream):
        events = stream.chunk("b", "This is a sentence with plenty of words. Goodbye.")
        finals = [e for e in events if isinstance(e, FinalCaption)]
        assert finals[0].text == "This is a sentence with plenty of words."

    def test_the_remainder_continues_as_a_partial(self, stream):
        events = stream.chunk("b", "This is a sentence with plenty of words. And more")
        assert isinstance(events[-1], PartialCaption)
        assert events[-1].text == "And more"

    def test_a_sentence_at_the_minimum_length_is_settled(self, stream):
        events = stream.chunk("b", "Thank you so much everyone.")
        assert [e.text for e in events if isinstance(e, FinalCaption)] == ["Thank you so much everyone."]

    def test_a_sentence_below_the_minimum_keeps_buffering(self, stream):
        events = stream.chunk("b", "Hi there.")
        assert not [e for e in events if isinstance(e, FinalCaption)]
        assert isinstance(events[0], PartialCaption)

    def test_a_short_sentence_settles_once_later_words_arrive(self, stream):
        stream.chunk("b", "Hi there.")
        events = stream.chunk("b", "How are you all doing today?")
        assert [e for e in events if isinstance(e, FinalCaption)]

    def test_chunks_accumulate_with_a_separating_space(self, stream):
        stream.chunk("b", "hello")
        stream.chunk("b", "world")
        assert stream.state_for("b").current_utterance == "hello world"

    def test_settling_with_no_remainder_clears_the_partial_line(self, stream):
        events = stream.chunk("b", "This is a sentence with plenty of words.")
        assert isinstance(events[-1], ClearCaption)

    def test_whitespace_only_chunk_emits_nothing(self, stream):
        assert stream.chunk("b", "  ") == []

    def test_a_settled_chunk_emits_final_before_clear(self, stream):
        events = stream.chunk("b", "This is a sentence with plenty of words.")
        kinds = [type(e).__name__ for e in events]
        # The viewer must see the line settle before the partial box empties.
        assert kinds == ["FinalCaption", "ClearCaption"]


class TestFinalAndClear:
    def test_final_settles_the_given_text(self, stream):
        events = stream.final("b", "all done")
        assert isinstance(events[0], FinalCaption)
        assert events[0].text == "all done"

    def test_final_falls_back_to_the_buffer_when_text_is_empty(self, stream):
        stream.partial("b", "buffered text")
        assert stream.final("b", "")[0].text == "buffered text"

    def test_final_on_an_empty_buffer_emits_nothing(self, stream):
        assert stream.final("b", "") == []

    def test_final_resets_the_buffer(self, stream):
        stream.partial("b", "some text")
        stream.final("b", "some text")
        state = stream.state_for("b")
        assert state.current_utterance == ""
        assert state.current_word_count == 0
        assert state.utterance_start_time == 0.0

    def test_final_starts_a_new_segment(self, stream):
        first = stream.partial("b", "one")[0].segment_id
        stream.final("b", "one")
        assert stream.partial("b", "two")[0].segment_id != first

    def test_clear_settles_a_pending_partial_rather_than_losing_it(self, stream):
        stream.partial("b", "in progress")
        events = stream.clear("b")
        assert isinstance(events[0], FinalCaption)
        assert events[0].text == "in progress"

    def test_clear_with_nothing_buffered_just_clears(self, stream):
        assert isinstance(stream.clear("b")[0], ClearCaption)


class TestInjectedSegmentIds:
    def test_the_first_segment_id_comes_from_the_injected_factory(self, stream):
        # Regression: state_for used the dataclass UUID default, so the very
        # first caption for a booth ignored the caller's factory.
        assert stream.partial("b", "hello")[0].segment_id == "seg-1"

    def test_the_first_final_also_uses_the_injected_factory(self, stream):
        assert stream.final("b", "hello")[0].segment_id == "seg-1"

    def test_later_segment_ids_keep_following_the_factory(self, stream):
        stream.final("b", "one")
        assert stream.partial("b", "two")[0].segment_id == "seg-2"


class TestRestore:
    def test_a_failed_publish_can_put_the_utterance_back(self, stream):
        stream.partial("b", "in progress")
        event = stream.final("b", "in progress")[0]
        assert stream.state_for("b").current_utterance == ""

        stream.restore(event)

        state = stream.state_for("b")
        assert state.current_utterance == "in progress"
        assert state.segment_id == event.segment_id
        assert state.current_word_count == 2

    def test_a_restored_utterance_settles_again_on_the_next_clear(self, stream):
        event = stream.final("b", "keep me")[0]
        stream.restore(event)
        assert stream.clear("b")[0].text == "keep me"

    def test_restore_prepends_rather_than_discarding(self, stream):
        # Discarding on a non-empty buffer loses the settled sentence, which is
        # the loss restore() exists to prevent.
        event = stream.final("b", "stale")[0]
        stream.partial("b", "newer words")

        stream.restore(event)

        assert stream.state_for("b").current_utterance == "stale newer words"

    def test_restore_keeps_a_settled_sentence_when_chunk_left_a_remainder(self, stream):
        """chunk() stores the tail of the same fragment before returning.

        That tail is not newer speech, so bailing out on a non-empty buffer
        dropped the sentence the final event carried.
        """
        events = stream.chunk("b", "This is a sentence with plenty of words. And more")
        final = next(e for e in events if isinstance(e, FinalCaption))
        assert stream.state_for("b").current_utterance == "And more"

        stream.restore(final)

        # The original utterance is whole again, in its original order.
        assert stream.state_for("b").current_utterance == (
            "This is a sentence with plenty of words. And more"
        )

    def test_a_restored_chunk_remainder_settles_on_the_next_clear(self, stream):
        events = stream.chunk("b", "This is a sentence with plenty of words. And more")
        stream.restore(next(e for e in events if isinstance(e, FinalCaption)))
        assert stream.clear("b")[0].text == "This is a sentence with plenty of words. And more"

    def test_restore_preserves_the_original_start_time(self, stream, clock):
        started = clock.now
        stream.partial("b", "hello")
        clock.advance(3.0)
        event = stream.final("b", "hello")[0]
        stream.restore(event)
        assert stream.state_for("b").utterance_start_time == started


class TestSequenceAndIsolation:
    def test_the_sequence_increases_across_finals(self, stream):
        first = stream.final("b", "one")[0].seq
        second = stream.final("b", "two")[0].seq
        assert second == first + 1

    def test_the_sequence_is_shared_across_booths(self, stream):
        # One counter per stream, so a room's captions stay totally ordered.
        first = stream.final("booth-a", "one")[0].seq
        second = stream.final("booth-b", "two")[0].seq
        assert second == first + 1

    def test_the_sequence_has_no_gap_when_nothing_is_emitted(self, stream):
        first = stream.final("b", "one")[0].seq
        assert stream.final("b", "") == []
        assert stream.final("b", "two")[0].seq == first + 1

    def test_booth_buffers_are_independent(self, stream):
        stream.partial("booth-a", "text A")
        stream.partial("booth-b", "text B")
        assert stream.state_for("booth-a").current_utterance == "text A"
        assert stream.state_for("booth-b").current_utterance == "text B"

    def test_booth_segment_ids_are_independent(self, stream):
        a = stream.partial("booth-a", "text A")[0].segment_id
        b = stream.partial("booth-b", "text B")[0].segment_id
        assert a != b

    def test_settling_one_booth_leaves_the_other_buffered(self, stream):
        stream.partial("booth-a", "text A")
        stream.partial("booth-b", "text B")
        stream.final("booth-a", "text A")
        assert stream.state_for("booth-b").current_utterance == "text B"


class TestTimingHandoff:
    def test_a_final_carries_the_utterance_start_for_the_caller(self, stream, clock):
        started = clock.now
        stream.partial("b", "hello")
        clock.advance(2.0)
        assert stream.final("b", "hello")[0].utterance_start_time == started

    def test_a_final_without_a_buffer_times_from_now(self, stream, clock):
        clock.advance(5.0)
        assert stream.final("b", "direct")[0].utterance_start_time == clock.now


def test_the_thresholds_are_named_not_inlined():
    """The issue's complaint was buried magic numbers; keep them importable."""
    assert (MAX_UTTERANCE_SECONDS, MAX_UTTERANCE_WORDS, MIN_SENTENCE_WORDS) == (15.0, 50, 5)
