"""Remember who a booth's active interpreter is, so a portal restart does not drop them (#1165).

Everything else about a booth lives in memory (``portal.booth_state``). Only this one fact is
written to the ``booths`` table: the ``booth_session_key`` of the person who was active, which
names a user or one invite visit and stays the same when their browser reconnects. It is never
a socket ``participant_id``, and it says *who* was active, never *what role* anyone has.

A failed read or write is logged once per outage (``OutageLog``) and dropped. It must never stop
a booth or delay a caption.
"""

from __future__ import annotations

import logging
from datetime import timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from portal.booth_identity import make_booth_id, parse_booth_id
from portal.database import get_session
from portal.models import DBBooth, Event, Room, utc_now
from portal.outage_log import OutageLog

if TYPE_CHECKING:
    from portal.booth_state import BoothRegistry

logger = logging.getLogger(__name__)

# A stored active interpreter older than this is forgotten at startup instead of reserved.
MAX_AGE = timedelta(hours=24)

_outage = OutageLog(logger, "booth_store.unavailable")


async def save_active(booth_id: str, session_key: str | None) -> None:
    """Store *session_key* as the booth's active interpreter, or clear it with ``None``.

    A booth that has no row (the floor pseudo-booth, a booth deleted meanwhile) is left alone.
    """
    try:
        event_slug, room_id, language_code = parse_booth_id(booth_id)
    except ValueError:
        return
    try:
        async with get_session() as session:
            event_id = select(Event.id).where(Event.slug == event_slug).scalar_subquery()
            await session.execute(
                update(DBBooth)
                .where(DBBooth.event_id == event_id, DBBooth.room_id == room_id, DBBooth.language_code == language_code)
                .values(
                    active_interpreter_session_key=session_key,
                    active_interpreter_since=utc_now() if session_key else None,
                )
            )
    except SQLAlchemyError:
        _outage.failed()
    else:
        _outage.recovered()


async def load_active() -> list[tuple[str, str]]:
    """Return ``(booth_id, session_key)`` for every booth that had an active interpreter.

    Entries older than ``MAX_AGE`` are cleared and left out, and so are booths of a deleted event
    or whose room belongs to another event. Returns ``[]`` if the database cannot be read: the
    booths then start the way they did before this existed.
    """
    stale: list[int] = []
    found: list[tuple[str, str]] = []
    try:
        async with get_session() as session:
            rows = await session.execute(
                select(
                    DBBooth.id,
                    DBBooth.room_id,
                    DBBooth.language_code,
                    DBBooth.active_interpreter_session_key,
                    DBBooth.active_interpreter_since,
                    Event.slug,
                )
                .join(Event, Event.id == DBBooth.event_id)
                .join(Room, Room.id == DBBooth.room_id)
                .where(
                    DBBooth.active_interpreter_session_key.is_not(None),
                    Event.deleted_at.is_(None),
                    Room.event_id == DBBooth.event_id,
                )
            )
            now = utc_now()
            for booth_pk, room_id, language_code, session_key, since, event_slug in rows:
                if since is not None and since.tzinfo is None:
                    since = since.replace(tzinfo=timezone.utc)  # SQLite drops the timezone
                if since is None or now - since > MAX_AGE:
                    stale.append(booth_pk)
                    continue
                try:
                    found.append((make_booth_id(event_slug, room_id, language_code), session_key))
                except ValueError:
                    continue
    except SQLAlchemyError:
        _outage.failed()
        return []
    _outage.recovered()
    if stale:
        await _clear_stale(stale)
    return found


async def _clear_stale(booth_pks: list[int]) -> None:
    """Forget old entries. A failure here only leaves them for the next start; it never costs a reservation."""
    try:
        async with get_session() as session:
            await session.execute(
                update(DBBooth)
                .where(DBBooth.id.in_(booth_pks))
                .values(active_interpreter_session_key=None, active_interpreter_since=None)
            )
    except SQLAlchemyError:
        _outage.failed()


async def enable_persistence(registry: BoothRegistry) -> None:
    """Start storing the active interpreter of every booth, and reserve the slots stored before a restart."""
    registry.attach_persistence(save_active)
    registry.restore_active(await load_active())
