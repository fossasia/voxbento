import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class CaptionState:
    booth_id: str
    current_utterance: str = ""
    utterance_start_time: float = 0.0
    current_word_count: int = 0


class CaptionAggregator:
    def __init__(self, broadcast_callback, room_id: int | None = None):
        self.broadcast_callback = broadcast_callback
        self.room_id = room_id
        self.states: dict[str, CaptionState] = {}

    def _get_state(self, booth_id: str) -> CaptionState:
        if booth_id not in self.states:
            self.states[booth_id] = CaptionState(booth_id=booth_id)
        return self.states[booth_id]

    async def handle_partial(self, booth_id: str, text: str):
        if not text.strip():
            return

        state = self._get_state(booth_id)

        if not state.current_utterance:
            state.utterance_start_time = time.time()

        state.current_utterance = text
        state.current_word_count = len(text.split())

        # Check forced finalization rules (15s or 50 words)
        if state.current_word_count >= 50 or (time.time() - state.utterance_start_time) >= 15.0:
            logger.info(f"[{booth_id}] Forced utterance finalization triggered (words: {state.current_word_count})")
            await self.handle_final(booth_id, text)
            return

        await self.broadcast_callback(
            booth_id, {"type": "caption", "status": "partial", "text": state.current_utterance}
        )

    async def handle_chunk(self, booth_id: str, text: str):
        """Used by Local Whisper. Appends finalized chunks and splits on punctuation."""
        if not text.strip():
            return

        state = self._get_state(booth_id)

        if not state.current_utterance:
            state.utterance_start_time = time.time()

        if state.current_utterance:
            state.current_utterance += " " + text.strip()
        else:
            state.current_utterance = text.strip()

        state.current_word_count = len(state.current_utterance.split())

        import re

        has_finalized = False
        while True:
            # Find the FIRST sentence boundary
            match = re.search(r"^([^.?!]*[.?!]+)(?:\s+|$)", state.current_utterance)
            if not match:
                break

            sentence = match.group(1).strip()
            split_idx = match.end()

            if sentence:
                await self.broadcast_callback(booth_id, {"type": "caption", "status": "final", "text": sentence})
                has_finalized = True

            state.current_utterance = state.current_utterance[split_idx:].strip()

        state.current_word_count = len(state.current_utterance.split()) if state.current_utterance else 0
        if state.current_utterance:
            state.utterance_start_time = time.time()
        else:
            state.utterance_start_time = 0.0

        if state.current_utterance:
            await self.broadcast_callback(
                booth_id, {"type": "caption", "status": "partial", "text": state.current_utterance}
            )
        elif has_finalized:
            # Only send clear if we just finalized something and have nothing left,
            # ensuring the frontend's partial box is cleared out.
            await self.broadcast_callback(booth_id, {"type": "caption", "status": "clear", "text": ""})
            if state.current_word_count >= 50 or (time.time() - state.utterance_start_time) >= 15.0:
                logger.info(f"[{booth_id}] Forced chunk finalization triggered (words: {state.current_word_count})")
                await self.handle_final(booth_id, state.current_utterance)
                return

            await self.broadcast_callback(
                booth_id, {"type": "caption", "status": "partial", "text": state.current_utterance}
            )

    async def handle_final(self, booth_id: str, text: str):
        state = self._get_state(booth_id)
        final_text = text.strip() or state.current_utterance

        if not final_text:
            return

        await self.broadcast_callback(booth_id, {"type": "caption", "status": "final", "text": final_text})

        if self.room_id is not None:
            import asyncio

            from portal.database import save_transcript_segment
            from portal.tts.worker import enqueue_tts

            # Queue TTS immediately. Supertonic rooms serialize synthesis per room
            # to preserve final-arrival order; Deepgram rooms may overlap segments
            # to preserve low-latency streaming. This runs synchronously before any
            # await that could reorder concurrent finals.
            enqueue_tts(self.room_id, final_text)

            async def _save_and_translate():
                segment_id = await save_transcript_segment(booth_id, final_text, self.room_id)
                if segment_id is not None:
                    from portal.translations.worker import TranslationWorker

                    worker = TranslationWorker(self.broadcast_callback)
                    await worker.handle_translation(self.room_id, segment_id, final_text, booth_id)

            asyncio.create_task(_save_and_translate())

        # Reset state for next utterance
        state.current_utterance = ""
        state.utterance_start_time = 0.0
        state.current_word_count = 0

    async def handle_clear(self, booth_id: str):
        """Called when silence is explicitly detected or a turn ends."""
        state = self._get_state(booth_id)
        if state.current_utterance:
            # If we have a pending partial, finalize it instead of losing it.
            await self.handle_final(booth_id, state.current_utterance)
        else:
            await self.broadcast_callback(booth_id, {"type": "caption", "status": "clear", "text": ""})

    def get_metrics(self, booth_id: str) -> dict:
        state = self._get_state(booth_id)
        return {
            "current_word_count": state.current_word_count,
            "open_duration": time.time() - state.utterance_start_time if state.utterance_start_time else 0.0,
        }
