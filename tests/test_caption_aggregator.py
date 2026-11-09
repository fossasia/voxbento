from __future__ import annotations

import asyncio

import pytest

from portal.transcription.aggregator import CaptionAggregator, CaptionState


async def collect_broadcasts(booth_id, message, received: list):
    received.append((booth_id, message))


@pytest.mark.anyio
class TestCaptionAggregator:
    async def test_handle_partial_emits_partial_status(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_partial("booth-1", "Hello world")

        assert len(received) == 1
        assert received[0][0] == "booth-1"
        assert received[0][1]["status"] == "partial"
        assert received[0][1]["text"] == "Hello world"
        assert received[0][1]["segment_id"]

    async def test_handle_partial_ignores_whitespace_only_text(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_partial("booth-1", "   ")

        assert len(received) == 0

    async def test_handle_chunk_splits_on_sentence_boundary(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_chunk("booth-1", "This is a very long sentence that has more than ten words. Goodbye.")

        # Expect at least one 'final' with text 'This is a very long sentence that has more than ten words.'
        finals = [msg for bid, msg in received if msg.get("status") == "final"]
        assert len(finals) >= 1
        assert finals[0]["text"] == "This is a very long sentence that has more than ten words."

    async def test_handle_chunk_no_sentence_boundary_emits_partial(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_chunk("booth-1", "Hello world without ending")

        partials = [msg for bid, msg in received if msg.get("status") == "partial"]
        assert len(partials) >= 1

    async def test_handle_final_resets_state(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_partial("b", "some text")
        await aggregator.handle_final("b", "some text")

        assert aggregator.states["b"].current_utterance == ""
        assert aggregator.states["b"].current_word_count == 0

    async def test_handle_final_emits_final_status(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_final("booth-1", "Some finalized text")

        finals = [msg for bid, msg in received if msg.get("status") == "final"]
        assert len(finals) == 1

    async def test_forced_finalization_at_50_words(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        text = " ".join(["word"] * 50)
        await aggregator.handle_partial("b", text)

        finals = [msg for bid, msg in received if msg.get("status") == "final"]
        assert len(finals) >= 1

    async def test_multi_booth_state_isolation(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_partial("booth-A", "text A")
        await aggregator.handle_partial("booth-B", "text B")

        assert aggregator.states["booth-A"].current_utterance == "text A"
        assert aggregator.states["booth-B"].current_utterance == "text B"

    async def test_handle_clear_finalizes_pending_partial(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_partial("b", "in progress")

        # clear before clearing received list, so it has 1 message so far
        received.clear()

        await aggregator.handle_clear("b")

        finals = [msg for bid, msg in received if msg.get("status") == "final"]
        assert len(finals) == 1
        assert finals[0]["text"] == "in progress"

    async def test_handle_chunk_splits_short_sentence_at_5_word_boundary(self):
        """Sentences with >= 5 words should trigger finalization (lowered from 10)."""
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        # "Thank you so much everyone." has 5 words and ends with a period.
        # Under the old 10-word threshold this would NOT finalize; under the
        # new 5-word threshold it MUST finalize.
        await aggregator.handle_chunk("booth-1", "Thank you so much everyone.")

        finals = [msg for bid, msg in received if msg.get("status") == "final"]
        assert len(finals) >= 1
        assert "Thank you so much everyone." in finals[0]["text"]

    async def test_handle_chunk_does_not_split_very_short_sentence(self):
        """Sentences with < 5 words should not trigger finalization from handle_chunk."""
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        # "Hi there." has only 2 words — should remain as partial, not finalize.
        await aggregator.handle_chunk("booth-1", "Hi there.")

        finals = [msg for bid, msg in received if msg.get("status") == "final"]
        assert len(finals) == 0

    async def test_a_failed_publish_keeps_the_utterance_buffered(self):
        """Matches dev, which reset state only after publish_final returned."""

        async def failing_callback(booth_id, message):
            raise RuntimeError("broadcast is down")

        aggregator = CaptionAggregator(failing_callback)
        aggregator.stream.state_for("b").current_utterance = "do not lose me"

        with pytest.raises(RuntimeError):
            await aggregator.handle_clear("b")

        # Without the restore the buffer would be empty and the caption gone.
        assert aggregator.states["b"].current_utterance == "do not lose me"

    async def test_a_recovered_broadcast_can_still_publish_the_utterance(self):
        failing = True
        received = []

        async def flaky_callback(booth_id, message):
            if failing:
                raise RuntimeError("broadcast is down")
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(flaky_callback)
        aggregator.stream.state_for("b").current_utterance = "retry me"

        with pytest.raises(RuntimeError):
            await aggregator.handle_clear("b")

        failing = False
        await aggregator.handle_clear("b")

        finals = [msg for bid, msg in received if msg.get("status") == "final"]
        assert [m["text"] for m in finals] == ["retry me"]

    async def test_a_failed_final_restores_the_rest_of_its_batch(self):
        """A chunk can produce two FinalCaptions in one dispatch batch — a
        settled sentence then a force-finalised remainder past the word cap.
        If the first fails the loop used to terminate and the second was lost.
        Both must be restored so the next clear publishes them whole."""
        from portal.transcription.caption_stream import FinalCaption

        received = []
        finals_seen = {"n": 0}

        async def fail_first_final(booth_id, message):
            if message.get("status") == "final":
                finals_seen["n"] += 1
                if finals_seen["n"] == 1:
                    raise RuntimeError("broadcast dropped")
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fail_first_final)
        # Construct the two-finals batch by hand: a settled sentence followed
        # by a force-finalised long remainder, exactly what chunk() produces
        # when a split leaves 50+ words behind.
        events = [
            FinalCaption("b", "First sentence here.", "seg-1", seq=1, utterance_start_time=1000.0),
            FinalCaption("b", " ".join(f"word{n}" for n in range(55)), "seg-2", seq=2, utterance_start_time=1005.0),
        ]

        with pytest.raises(RuntimeError):
            await aggregator._dispatch(events)

        # Both finals must now sit in the buffer awaiting a retry; the second
        # would have been lost before the fix.
        buffered = aggregator.states["b"].current_utterance
        assert "First sentence here." in buffered
        assert "word54" in buffered

        # Draining to a real broadcast proves the combined caption actually
        # reaches listeners.
        await aggregator.handle_clear("b")
        final_payloads = [m for _, m in received if m.get("status") == "final"]
        assert any(
            "First sentence here." in m["text"] and "word54" in m["text"] for m in final_payloads
        ), "the batch-restored finals must publish together on retry"
