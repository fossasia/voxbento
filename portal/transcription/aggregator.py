from __future__ import annotations

import logging
import time

from sqlalchemy import select, update

from portal import youtube_captions
from portal.database import get_session
from portal.models import Room
from portal.tasks import track_task
from portal.transcription.caption_stream import (
    CaptionEvent,
    CaptionState,
    CaptionStream,
    ClearCaption,
    FinalCaption,
    PartialCaption,
)
from portal.transcription.timing import SegmentTiming, segment_timing, timing_metadata

logger = logging.getLogger(__name__)

__all__ = ["CaptionAggregator", "CaptionState"]


class CaptionAggregator:
    """Applies the side effects a `CaptionStream` event implies.

    The stream owns the buffering rules and stays pure; this class is the seam
    where a settled caption reaches the database, the translation worker and
    the TTS queue.
    """

    def __init__(self, broadcast_callback, room_id: int | None = None):
        logger.info("CaptionAggregator initialized with room_id=%s", room_id)
        self.broadcast_callback = broadcast_callback
        self.room_id = room_id
        self.stream = CaptionStream()

    @property
    def states(self) -> dict[str, CaptionState]:
        """Per-booth buffers, kept on the stream."""
        return self.stream.states

    def _get_state(self, booth_id: str) -> CaptionState:
        return self.stream.state_for(booth_id)

    async def _dispatch(self, events: list[CaptionEvent]) -> None:
        for event in events:
            if isinstance(event, PartialCaption):
                await self.broadcast_callback(
                    event.booth_id,
                    {
                        "type": "caption",
                        "status": "partial",
                        "text": event.text,
                        "segment_id": event.segment_id,
                        **await self.partial_timing(event.booth_id),
                    },
                )
            elif isinstance(event, FinalCaption):
                await self._emit_final(event)
            elif isinstance(event, ClearCaption):
                await self.broadcast_callback(event.booth_id, {"type": "caption", "status": "clear", "text": ""})

    async def handle_partial(self, booth_id: str, text: str):
        await self._dispatch(self.stream.partial(booth_id, text))

    async def handle_chunk(self, booth_id: str, text: str):
        """Used by Local Whisper. Appends finalized chunks and splits on punctuation."""
        await self._dispatch(self.stream.chunk(booth_id, text))

    async def handle_final(self, booth_id: str, text: str):
        await self._dispatch(self.stream.final(booth_id, text))

    async def handle_clear(self, booth_id: str):
        """Called when silence is explicitly detected or a turn ends."""
        await self._dispatch(self.stream.clear(booth_id))

    async def partial_timing(self, booth_id: str) -> dict:
        if self.room_id is None or not booth_id.endswith("-floor"):
            return {}
        async with get_session() as session:
            room = await session.get(Room, self.room_id)
            if room and room.floor_source == "program_ingest":
                now = int(time.time() * 1000)
                return {"source_received_at_ms": now, "server_sent_at_ms": now,
                        "sync_offset_ms": room.program_sync_offset_ms, "timestamp_basis": "stt_receive_wall_clock"}
        return {}

    async def _emit_final(self, event: FinalCaption) -> None:
        """Publish a settled caption, taking the floor sequence from the room.

        A program-ingest floor caption is numbered from `rooms.floor_caption_seq`
        so the sequence survives a worker restart; every other booth uses the
        stream's process-local counter.
        """
        seq = event.seq
        timing = None
        if self.room_id is not None and event.booth_id.endswith("-floor"):
            async with get_session() as session:
                room = await session.scalar(select(Room).where(Room.id == self.room_id))
                if room and room.floor_source == "program_ingest":
                    seq = await session.scalar(update(Room).where(Room.id == room.id).values(
                        floor_caption_seq=Room.floor_caption_seq + 1,
                    ).returning(Room.floor_caption_seq))
                    timing = SegmentTiming(int(event.utterance_start_time * 1000), room.program_sync_offset_ms)

        context_token = segment_timing.set(timing)
        try:
            await self.publish_final(event.booth_id, event.text, event.segment_id, seq)
        finally:
            segment_timing.reset(context_token)

    async def publish_final(self, booth_id: str, final_text: str, segment_id: str, seq: int):

        await self.broadcast_callback(
            booth_id, {"type": "caption", "status": "final", "text": final_text, "segment_id": segment_id, "seq": seq,
                       **timing_metadata()}
        )

        if self.room_id is not None and booth_id.endswith("-floor"):
            # Non-blocking: queues the caption for the room's YouTube caption track, if one is set up.
            timing = segment_timing.get()
            youtube_captions.offer(self.room_id, "floor", final_text, timing.source_received_at_ms if timing else None)

        if self.room_id is not None:
            import asyncio

            from portal.database import save_transcript_segment

            async def _save_and_translate():
                try:
                    db_segment_id = await save_transcript_segment(booth_id, final_text, self.room_id)
                    if db_segment_id is not None:
                        from portal.translations.worker import TranslationWorker

                        worker = TranslationWorker(self.broadcast_callback)
                        await worker.handle_translation(
                            self.room_id, db_segment_id, final_text, booth_id, segment_id, seq
                        )
                except Exception as e:
                    logger.error(f"[{booth_id}] _save_and_translate failed: {e}", exc_info=True)

            track_task(asyncio.create_task(_save_and_translate()))
