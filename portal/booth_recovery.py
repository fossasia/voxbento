"""Keep live booths live across a portal restart (TD-03).

``BoothRegistry`` lives in process memory. Without this module a deploy or crash
during an event forgets every booth's Go Live switch and active interpreter: the
first interpreter to reconnect would take the active slot, and with broadcast
locked again their client would stop publishing.

``run`` saves each booth's live record (``Booth.live_record``) to
``booth_live_state`` shortly after it changes, and refreshes ``updated_at`` on
the rows of live booths every ``HEARTBEAT_SECS`` so their age says how long the
portal has been down. ``restore`` reads the rows back at startup, unless they are
older than ``booth_state_max_age_seconds``. The active interpreter comes back as a
disconnected placeholder that only the same session can reclaim, and expires
after ``booth_restart_grace_seconds`` if nobody does.

Nothing here runs on the live path: a failed save is logged once per outage
(``portal.outage_log``) and retried, and a failed restore starts with empty booths
as before. One portal replica, like the registry itself.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import SQLAlchemyError

from portal.booth_state import BoothRegistry
from portal.database import get_session
from portal.models import BoothLiveState
from portal.outage_log import OutageLog

logger = logging.getLogger(__name__)

# Bursts of changes (a handoff, a reconnect) are saved together.
DEBOUNCE_SECS = 0.2
HEARTBEAT_SECS = 60.0
RETRY_SECS = 2.0

# Columns of booth_live_state filled from Booth.live_record, in addition to booth_id.
_RECORD_FIELDS = (
    "language",
    "channel_id",
    "room_id",
    "broadcast_unlocked",
    "active_participant_id",
    "active_session_key",
    "active_display_name",
    "active_role",
    "active_user_id",
    "active_invite_public_id",
    "active_mic_active",
    "active_ingest_connected",
)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite drops the timezone; every stored time is UTC."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def save_changes(changes: dict[str, dict | None], now: datetime) -> None:
    """Write one row per changed booth, and delete the rows of booths with nothing live."""
    gone = [booth_id for booth_id, record in changes.items() if record is None]
    async with get_session() as session:
        if gone:
            await session.execute(delete(BoothLiveState).where(BoothLiveState.booth_id.in_(gone)))
        for booth_id, record in changes.items():
            if record is None:
                continue
            values = {name: record[name] for name in _RECORD_FIELDS}
            values["updated_at"] = now
            result = await session.execute(
                update(BoothLiveState).where(BoothLiveState.booth_id == booth_id).values(**values)
            )
            if result.rowcount == 0:
                session.add(BoothLiveState(booth_id=booth_id, **values))


async def touch(booth_ids: list[str], now: datetime) -> None:
    """Mark the rows of booths still in memory as current."""
    if not booth_ids:
        return
    async with get_session() as session:
        await session.execute(
            update(BoothLiveState).where(BoothLiveState.booth_id.in_(booth_ids)).values(updated_at=now)
        )


async def load_records(max_age_seconds: float, now: datetime) -> list[dict]:
    """Saved live records young enough to restore. Older rows are deleted."""
    cutoff = now - timedelta(seconds=max_age_seconds)
    records = []
    async with get_session() as session:
        rows = (await session.execute(select(BoothLiveState))).scalars().all()
        stale = []
        for row in rows:
            if _aware(row.updated_at) < cutoff:
                stale.append(row.booth_id)
                continue
            records.append({"booth_id": row.booth_id, **{name: getattr(row, name) for name in _RECORD_FIELDS}})
        if stale:
            await session.execute(delete(BoothLiveState).where(BoothLiveState.booth_id.in_(stale)))
    return records


async def restore(registry: BoothRegistry, *, max_age_seconds: float) -> list[tuple[str, str]]:
    """Rebuild live booths saved by the previous run.

    Returns ``(booth_id, participant_id)`` for each active interpreter restored as a
    placeholder, for the caller to expire. A database failure restores nothing.
    """
    try:
        records = await load_records(max_age_seconds, _utc_now())
    except (SQLAlchemyError, OSError):
        logger.warning("booth_state_restore_failed", exc_info=True)
        return []
    placeholders = await registry.restore(records)
    if records:
        logger.warning(
            "booth_state_restored booths=%d awaiting_interpreters=%d", len(records), len(placeholders)
        )
    return placeholders


class _Saver:
    """Saves changed booths, skipping records identical to the last ones written."""

    def __init__(self, registry: BoothRegistry) -> None:
        self.registry = registry
        self.saved: dict[str, dict | None] = {}
        self.outage = OutageLog(logger, "booth_state_save_failed")

    async def save(self) -> bool:
        changes = await self.registry.take_changes()
        changes = {booth_id: record for booth_id, record in changes.items() if self.saved.get(booth_id, ...) != record}
        if not changes:
            return True
        try:
            await save_changes(changes, _utc_now())
        except asyncio.CancelledError:
            # Shutdown cancelled the save mid-write: hand the batch back so the final save writes it.
            self.registry.requeue_changes(set(changes))
            raise
        except (SQLAlchemyError, OSError):
            self.outage.failed()
            logger.debug("Booth state save failed", exc_info=True)
            self.registry.requeue_changes(set(changes))
            return False
        self.outage.recovered()
        for booth_id, record in changes.items():
            if record is None:
                self.saved.pop(booth_id, None)
            else:
                self.saved[booth_id] = record
        return True

    async def heartbeat(self) -> None:
        live = [booth_id for booth_id, record in self.saved.items() if record is not None]
        try:
            await touch(live, _utc_now())
        except (SQLAlchemyError, OSError):
            self.outage.failed()
            logger.debug("Booth state heartbeat failed", exc_info=True)


async def run(registry: BoothRegistry, *, heartbeat_secs: float = HEARTBEAT_SECS) -> None:
    """Save live booth state as it changes, until cancelled; save once more at shutdown."""
    saver = _Saver(registry)
    loop = asyncio.get_running_loop()
    next_heartbeat = loop.time() + heartbeat_secs
    try:
        while True:
            timeout = max(next_heartbeat - loop.time(), 0.0)
            try:
                await asyncio.wait_for(registry.wait_for_changes(), timeout=timeout)
            except TimeoutError:
                pass
            else:
                await asyncio.sleep(DEBOUNCE_SECS)
                if not await saver.save():
                    await asyncio.sleep(RETRY_SECS)
            if loop.time() >= next_heartbeat:
                next_heartbeat = loop.time() + heartbeat_secs
                await saver.heartbeat()
    finally:
        # A deploy stops the portal this way: save the latest state for the next run.
        await saver.save()
