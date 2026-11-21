from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from portal.meeting_analysis.constants import SESSION_GAP


@dataclass(frozen=True)
class Session:
    first_id: int
    started_at: datetime
    ended_at: datetime
    lines: int


def split_sessions(rows: Sequence[tuple[int, datetime]], gap: timedelta = SESSION_GAP) -> list[Session]:
    sessions: list[Session] = []
    first_id: int | None = None
    started = ended = None
    lines = 0
    for segment_id, created_at in rows:
        if first_id is not None and created_at - ended > gap:
            sessions.append(Session(first_id, started, ended, lines))
            first_id = None
        if first_id is None:
            first_id, started, lines = segment_id, created_at, 0
        ended = created_at
        lines += 1
    if first_id is not None:
        sessions.append(Session(first_id, started, ended, lines))
    return sessions[::-1]


def describe_range(started: datetime, ended: datetime) -> str:
    if started.date() == ended.date():
        return f"{started.strftime('%b %d, %H:%M')}–{ended.strftime('%H:%M')} UTC"
    return f"{started.strftime('%b %d, %H:%M')} – {ended.strftime('%b %d, %H:%M')} UTC"
