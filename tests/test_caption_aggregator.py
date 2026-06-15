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
        assert received[0] == ("booth-1", {"type": "caption", "status": "partial", "text": "Hello world"})

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
        await aggregator.handle_chunk("booth-1", "Hello world. Goodbye.")

        # Expect at least one 'final' with text 'Hello world.'
        finals = [msg for bid, msg in received if msg.get("status") == "final"]
        assert len(finals) >= 1
        assert finals[0]["text"] == "Hello world."

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

    async def test_get_metrics_returns_word_count(self):
        received = []

        async def fake_callback(booth_id, message):
            await collect_broadcasts(booth_id, message, received)

        aggregator = CaptionAggregator(fake_callback)
        await aggregator.handle_partial("b", "one two three")

        metrics = aggregator.get_metrics("b")
        assert metrics["current_word_count"] == 3
