"""Interpreter attendance and interpreting time (#1215).

The booth registry tells the tracker about every booth change (``observe``), while it
holds its lock. The tracker keeps one open *visit* per participant in memory: when they
joined, and how long they have been interpreting. Interpreting means being the booth's
active interpreter with the stream connected and the microphone on (``mic_active``,
which also follows a device mute, #1214).

Nothing here touches the database on the live path. ``flush`` saves open and finished
visits in one transaction every ``FLUSH_INTERVAL_SECS`` and at shutdown, so a crash
loses at most one interval. A failed save is logged once per outage
(``portal.outage_log``) and retried on the next flush.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, tzinfo
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from portal.database import get_session
from portal.models import Event, InterpreterAttendance, User
from portal.outage_log import OutageLog

if TYPE_CHECKING:
    from portal.booth_state import Booth

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SECS = 60.0
# Finished visits kept for retry while the database is down; the oldest go first.
MAX_PENDING_CLOSED = 5000
# The last flush at shutdown has no next interval to fall back on, so it gets a few tries.
SHUTDOWN_FLUSH_ATTEMPTS = 3
SHUTDOWN_RETRY_DELAY_SECS = 0.5


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite drops the timezone; every stored time is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass
class Visit:
    id: str
    booth_id: str
    event_slug: str
    room_id: int | None
    language_code: str
    participant_id: str
    user_id: int | None
    invite_public_id: str | None
    display_name: str
    role: str
    joined_at: datetime
    left_at: datetime | None = None
    interpreting_seconds: float = 0.0
    interpreting_since: datetime | None = None

    def interpreting_total(self, now: datetime) -> float:
        """Seconds interpreted so far, including the stretch still running."""
        running = (now - self.interpreting_since).total_seconds() if self.interpreting_since else 0.0
        return self.interpreting_seconds + max(running, 0.0)

    def as_row(self, now: datetime) -> dict:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "language_code": self.language_code,
            "user_id": self.user_id,
            "invite_public_id": self.invite_public_id,
            "display_name": self.display_name[:200],
            "role": self.role[:32],
            "joined_at": self.joined_at,
            "left_at": self.left_at,
            "last_seen_at": self.left_at or now,
            "interpreting_seconds": round(self.interpreting_total(self.left_at or now)),
        }


def is_interpreting(booth: Booth, participant_id: str) -> bool:
    participant = booth.participants.get(participant_id)
    return (
        participant is not None
        and booth.active_interpreter_id == participant_id
        and participant.ingest_connected
        and participant.mic_active
    )


@dataclass
class AttendanceTracker:
    clock: Callable[[], datetime] = _utc_now
    _open: dict[tuple[str, str], Visit] = field(default_factory=dict)
    _closed: list[Visit] = field(default_factory=list)
    _outage: OutageLog = field(default_factory=lambda: OutageLog(logger, "attendance_flush_failed"))

    # ── Live path (synchronous, called under the booth registry lock) ─────────

    def observe(self, booth: Booth) -> None:
        """Bring the visits of *booth* up to date with its participants."""
        if not booth.event_slug:
            return  # legacy free-form booth id: no event to report under
        now = self.clock()
        present = booth.participants
        for participant_id, participant in present.items():
            key = (booth.booth_id, participant_id)
            visit = self._open.get(key)
            if visit is None:
                visit = Visit(
                    id=uuid4().hex,
                    booth_id=booth.booth_id,
                    event_slug=booth.event_slug,
                    room_id=booth.room_id,
                    language_code=booth.language_code,
                    participant_id=participant_id,
                    user_id=participant.user_id,
                    invite_public_id=participant.invite_public_id,
                    display_name=participant.display_name,
                    role=str(participant.role),
                    joined_at=now,
                )
                self._open[key] = visit
            self._set_interpreting(visit, is_interpreting(booth, participant_id), now)

        gone = [key for key in self._open if key[0] == booth.booth_id and key[1] not in present]
        for key in gone:
            self._close(self._open.pop(key), now)

    def forget_booth(self, booth_id: str) -> None:
        """Close every visit of a booth that is being removed."""
        now = self.clock()
        for key in [key for key in self._open if key[0] == booth_id]:
            self._close(self._open.pop(key), now)

    def close_all(self) -> None:
        """Close every open visit (the portal is stopping, so every socket goes with it)."""
        now = self.clock()
        for visit in self._open.values():
            self._close(visit, now)
        self._open.clear()

    def open_visits(self, event_slug: str) -> list[Visit]:
        return [visit for visit in self._open.values() if visit.event_slug == event_slug]

    def _set_interpreting(self, visit: Visit, interpreting: bool, now: datetime) -> None:
        if interpreting and visit.interpreting_since is None:
            visit.interpreting_since = now
        elif not interpreting and visit.interpreting_since is not None:
            visit.interpreting_seconds = visit.interpreting_total(now)
            visit.interpreting_since = None

    def _close(self, visit: Visit, now: datetime) -> None:
        self._set_interpreting(visit, False, now)
        visit.left_at = now
        self._closed.append(visit)
        if len(self._closed) > MAX_PENDING_CLOSED:
            del self._closed[: len(self._closed) - MAX_PENDING_CLOSED]

    # ── Persistence (background) ──────────────────────────────────────────────

    async def flush(self) -> int:
        """Save open and finished visits. Returns how many rows were written."""
        now = self.clock()
        closed = list(self._closed)
        visits = closed + list(self._open.values())
        if not visits:
            return 0
        try:
            written = await save_visits(visits, now)
        except (SQLAlchemyError, OSError):
            self._outage.failed()
            logger.debug("Attendance flush failed", exc_info=True)
            return 0
        self._outage.recovered()
        # Visits closed while the save ran stay queued for the next flush.
        done = {id(visit) for visit in closed}
        self._closed = [visit for visit in self._closed if id(visit) not in done]
        return written

    async def run(self, interval: float = FLUSH_INTERVAL_SECS) -> None:
        # Recovery sits inside the try, so a shutdown while it runs still saves the visits.
        try:
            try:
                await close_stale_visits()
            except (SQLAlchemyError, OSError):
                logger.warning("attendance_recovery_failed", exc_info=True)
            while True:
                await asyncio.sleep(interval)
                await self.flush()
        finally:
            self.close_all()
            for attempt in range(SHUTDOWN_FLUSH_ATTEMPTS):
                if attempt:
                    await asyncio.sleep(SHUTDOWN_RETRY_DELAY_SECS)
                await self.flush()
                if not self._closed:
                    break
            else:
                logger.warning("attendance_shutdown_flush_failed unsaved=%d", len(self._closed))


async def save_visits(visits: Iterable[Visit], now: datetime) -> int:
    """Insert or update one row per visit, in one transaction."""
    visits = list(visits)
    slugs = {visit.event_slug for visit in visits}
    written = 0
    async with get_session() as session:
        result = await session.execute(select(Event.slug, Event.id).where(Event.slug.in_(slugs)))
        event_ids = dict(result.all())
        # A user deleted since joining must not fail the whole batch on the foreign key. The
        # visit is kept without the account; if the deletion races this check, this flush
        # fails and the next one, reading users again, saves it.
        wanted = {visit.user_id for visit in visits if visit.user_id is not None}
        existing = set((await session.execute(select(User.id).where(User.id.in_(wanted)))).scalars()) if wanted else set()
        for visit in visits:
            if visit.user_id is not None and visit.user_id not in existing:
                visit.user_id = None
            event_id = event_ids.get(visit.event_slug)
            if event_id is None:
                continue  # the event was deleted; nothing to report under
            row = visit.as_row(now)
            changed = await session.execute(
                update(InterpreterAttendance)
                .where(InterpreterAttendance.id == row["id"])
                .values(**{k: v for k, v in row.items() if k != "id"})
            )
            if changed.rowcount == 0:
                session.add(InterpreterAttendance(event_id=event_id, **row))
            written += 1
    return written


async def close_stale_visits() -> int:
    """Close visits left open by a previous run at the last time they were saved.

    The portal runs one replica and keeps booths in memory, so after a restart no
    saved visit can still be open.
    """
    async with get_session() as session:
        result = await session.execute(
            update(InterpreterAttendance)
            .where(InterpreterAttendance.left_at.is_(None))
            .values(left_at=InterpreterAttendance.last_seen_at)
        )
        return result.rowcount or 0


# ── Report ────────────────────────────────────────────────────────────────────


@dataclass
class VisitView:
    room_id: int | None
    language_code: str
    joined_at: datetime
    left_at: datetime | None
    present_seconds: int
    interpreting_seconds: int

    @property
    def is_open(self) -> bool:
        return self.left_at is None


@dataclass
class PersonAttendance:
    key: str
    name: str
    detail: str
    visits: list[VisitView] = field(default_factory=list)

    @property
    def first_joined(self) -> datetime:
        return min(visit.joined_at for visit in self.visits)

    @property
    def last_left(self) -> datetime | None:
        if any(visit.is_open for visit in self.visits):
            return None
        return max(visit.left_at for visit in self.visits if visit.left_at is not None)

    @property
    def is_here(self) -> bool:
        return self.last_left is None

    @property
    def present_seconds(self) -> int:
        """Time in a booth. Overlapping visits (two tabs, two booths) count once."""
        spans = sorted(
            (visit.joined_at.timestamp(), visit.joined_at.timestamp() + visit.present_seconds)
            for visit in self.visits
        )
        total = 0.0
        start, end = spans[0]
        for span_start, span_end in spans[1:]:
            if span_start > end:
                total += end - start
                start, end = span_start, span_end
            else:
                end = max(end, span_end)
        return round(total + end - start)

    @property
    def interpreting_seconds(self) -> int:
        # Only one person can be active in a booth, so their visits never overlap on air.
        return min(sum(visit.interpreting_seconds for visit in self.visits), self.present_seconds)

    @property
    def interpreting_share(self) -> int:
        """Interpreting time as a whole percentage of time in a booth."""
        return round(100 * self.interpreting_seconds / self.present_seconds) if self.present_seconds else 0

    @property
    def booths(self) -> list[tuple[int | None, str]]:
        return sorted({(visit.room_id, visit.language_code) for visit in self.visits}, key=lambda b: (b[0] or 0, b[1]))


@dataclass
class AttendanceRow:
    """One visit, merged from the database and the live tracker."""

    id: str
    room_id: int | None
    language_code: str
    user_id: int | None
    invite_public_id: str | None
    display_name: str
    role: str
    joined_at: datetime
    left_at: datetime | None
    interpreting_seconds: float


def rows_for_event(db_rows: Iterable, open_visits: Iterable[Visit], now: datetime) -> list[AttendanceRow]:
    """Saved visits, with live visits replacing their (up to a minute old) saved copy."""
    rows: dict[str, AttendanceRow] = {}
    for row in db_rows:
        rows[row.id] = AttendanceRow(
            id=row.id,
            room_id=row.room_id,
            language_code=row.language_code,
            user_id=row.user_id,
            invite_public_id=row.invite_public_id,
            display_name=row.display_name,
            role=row.role,
            joined_at=_aware(row.joined_at),
            left_at=_aware(row.left_at or row.last_seen_at),
            interpreting_seconds=row.interpreting_seconds,
        )
    for visit in open_visits:
        rows[visit.id] = AttendanceRow(
            id=visit.id,
            room_id=visit.room_id,
            language_code=visit.language_code,
            user_id=visit.user_id,
            invite_public_id=visit.invite_public_id,
            display_name=visit.display_name,
            role=visit.role,
            joined_at=visit.joined_at,
            left_at=None,
            interpreting_seconds=visit.interpreting_total(now),
        )
    return sorted(rows.values(), key=lambda r: r.joined_at)


def booth_key(room_id: int | None, language_code: str) -> str:
    """Stable filter value for one booth: ``"{room_id}-{language_code}"``."""
    return f"{room_id if room_id is not None else 0}-{language_code}"


def local_day(value: datetime, tz: tzinfo) -> date:
    return value.astimezone(tz).date()


def days_with_visits(rows: Iterable[AttendanceRow], tz: tzinfo) -> list[date]:
    """Days (in the viewer's time zone) on which a visit started, oldest first."""
    return sorted({local_day(row.joined_at, tz) for row in rows})


def filter_rows(
    rows: Iterable[AttendanceRow], tz: tzinfo, *, day: date | None = None, booth: str | None = None
) -> list[AttendanceRow]:
    """Visits that started on *day* (viewer's time zone) and/or were in *booth*."""
    return [
        row
        for row in rows
        if (day is None or local_day(row.joined_at, tz) == day)
        and (booth is None or booth_key(row.room_id, row.language_code) == booth)
    ]


def summarize(
    rows: Iterable[AttendanceRow],
    now: datetime,
    *,
    users: dict[int, tuple[str, str]] | None = None,
    invite_labels: dict[str, str] | None = None,
) -> list[PersonAttendance]:
    """Group visits by person: an account, or a name on one invite link.

    ``users`` maps user id to ``(display name, email)``; ``invite_labels`` maps an invite
    link's public id to its label. Names typed on the booth page are the fallback.
    """
    users = users or {}
    invite_labels = invite_labels or {}
    people: dict[str, PersonAttendance] = {}
    for row in rows:
        typed_name = row.display_name.strip() or "Interpreter"
        if row.user_id is not None:
            key = f"user:{row.user_id}"
            name, email = users.get(row.user_id, (typed_name, ""))
            detail = email
        elif row.invite_public_id:
            key = f"invite:{row.invite_public_id}:{typed_name.casefold()}"
            name = typed_name
            label = invite_labels.get(row.invite_public_id, "")
            detail = f"Invite link · {label}" if label else "Invite link"
        else:
            key = f"name:{typed_name.casefold()}"
            name, detail = typed_name, ""
        person = people.setdefault(key, PersonAttendance(key=key, name=name, detail=detail))
        end = row.left_at or now
        present = max(round((end - row.joined_at).total_seconds()), 0)
        person.visits.append(
            VisitView(
                room_id=row.room_id,
                language_code=row.language_code,
                joined_at=row.joined_at,
                left_at=row.left_at,
                present_seconds=present,
                interpreting_seconds=min(round(row.interpreting_seconds), present),
            )
        )
    return sorted(people.values(), key=lambda p: p.first_joined)


def format_duration(seconds: int | float) -> str:
    """Short, readable duration: ``0m``, ``45s``, ``12m``, ``1h 05m``."""
    seconds = max(int(round(seconds)), 0)
    if seconds < 60:
        return f"{seconds}s" if seconds else "0m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60:02d}m"
