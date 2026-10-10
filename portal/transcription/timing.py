"""Per-segment metadata inherited by translation tasks, never by other rooms."""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class SegmentTiming:
    source_received_at_ms: int
    sync_offset_ms: int


segment_timing: ContextVar[SegmentTiming | None] = ContextVar("segment_timing", default=None)


def timing_metadata() -> dict[str, int | str]:
    timing = segment_timing.get()
    if timing is None:
        return {}
    return {
        "source_received_at_ms": timing.source_received_at_ms,
        "timestamp_basis": "stt_receive_wall_clock",
        "sync_offset_ms": timing.sync_offset_ms,
        "server_sent_at_ms": int(time.time() * 1000),
    }
