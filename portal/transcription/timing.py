"""Per-segment metadata inherited by translation tasks, never by other rooms."""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SegmentTiming:
    source_received_at_ms: int
    sync_offset_ms: int | None = None
    caption_received_at_ms: int | None = None


segment_timing: ContextVar[SegmentTiming | None] = ContextVar("segment_timing", default=None)


def timing_metadata(*, for_caption: bool = False) -> dict[str, int | str]:
    timing = segment_timing.get()
    if timing is None:
        return {}
    received_ms = (
        timing.caption_received_at_ms
        if for_caption and timing.caption_received_at_ms is not None
        else timing.source_received_at_ms
    )
    meta: dict[str, int | str] = {
        "source_received_at_ms": received_ms,
        "timestamp_basis": "stt_receive_wall_clock",
        "server_sent_at_ms": int(time.time() * 1000),
    }
    if timing.sync_offset_ms is not None:
        meta["sync_offset_ms"] = timing.sync_offset_ms
    return meta
