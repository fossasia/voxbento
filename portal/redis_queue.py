"""Optional Redis dispatch for the webhook and email outboxes.

The database row stays the durable record of every message. Redis carries only row
IDs, which wake the worker as soon as a row is committed, instead of the worker
polling the database every second. The worker then runs the existing atomic
database claim, so a lost, duplicated or stale ID can never lose or double a
message. A slow timer still scans the database, which delivers rows whose ID never
reached Redis (a Redis restart, an outage, or rows queued before Redis was enabled).

Retries are kept in a sorted set scored by their due time and moved to the ready
list when due.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from portal.config import settings
from portal.outage_log import OutageLog
from portal.tasks import track_task

logger = logging.getLogger(__name__)

WEBHOOKS = "webhooks"
EMAIL = "email"

# Database scan interval while Redis is healthy; it only catches IDs Redis never received.
RECONCILE_SECS = 30.0
# Database scan interval while Redis is unreachable, close to the polling workers' cadence.
OUTAGE_SCAN_SECS = 5.0
# How long one blocking pop waits before due retries are moved again.
WAIT_SECS = 1.0
# IDs drained per wake-up; one database scan serves the whole burst.
DRAIN_BATCH = 100

# Failures a scan can recover from by trying again: the database or Redis being unreachable
# or timing out. Anything else is a bug and stops the worker so it is noticed, not retried.
RECOVERABLE_SCAN_ERRORS = (SQLAlchemyError, RedisError, OSError)

_KEY_PREFIX = "vb:queue"
_PENDING_INFO_KEY = "vb_redis_queue_pending"

_client: Redis | None = None
_client_error_logged = False
_outage = OutageLog(logger, "redis_queue unavailable")


def get_client() -> Redis | None:
    """Return the shared client, or None when Redis is not configured or its URL is unusable.

    Settings validation rejects malformed URLs at startup; this guard keeps the workers on
    database scans rather than stopping them if a client still cannot be built.
    """
    global _client, _client_error_logged
    if not settings.redis_enabled:
        return None
    if _client is None:
        try:
            _client = Redis.from_url(settings.redis_url.strip(), socket_timeout=5, socket_connect_timeout=5)
        except ValueError:
            if not _client_error_logged:
                _client_error_logged = True
                logger.error("redis_queue REDIS_URL is unusable; delivering from database scans only")
            return None
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def ready_key(name: str) -> str:
    return f"{_KEY_PREFIX}:{name}:ready"


def delayed_key(name: str) -> str:
    return f"{_KEY_PREFIX}:{name}:delayed"


def parse_id(raw: bytes | str) -> int | None:
    """Values read from Redis are untrusted: accept only positive integer IDs."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def epoch_seconds(due: datetime) -> float:
    # SQLite returns naive datetimes for timezone-aware columns; they are stored in UTC.
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due.timestamp()


async def enqueue(name: str, ids: Iterable[int], due: datetime | None = None) -> None:
    """Wake the worker for these rows, now or at ``due``. Best effort: never raises.

    A failure is logged once per outage; the worker's database scan still delivers the rows.
    """
    client = get_client()
    id_list = list(ids)
    if client is None or not id_list:
        return
    try:
        if due is None or epoch_seconds(due) <= time.time():
            await client.rpush(ready_key(name), *id_list)
        else:
            score = epoch_seconds(due)
            await client.zadd(delayed_key(name), {str(item_id): score for item_id in id_list})
    except (RedisError, OSError):
        _outage.failed()
        return
    _outage.recovered()


@dataclass(frozen=True)
class PendingEnqueue:
    name: str
    item_id: int
    due: datetime | None
    # The innermost transaction open when the row was written; a rollback of it, or of
    # any transaction enclosing it, means the row was never committed.
    transaction: SessionTransaction | None


def enqueue_after_commit(session: AsyncSession, name: str, item_id: int, due: datetime | None = None) -> None:
    """Enqueue once the caller's outermost transaction commits; a rollback enqueues nothing.

    Lets a row be written in its trigger's transaction (the outbox pattern) without the
    worker being woken for a row that may never exist. A savepoint commit is not enough:
    the row only becomes visible to the worker when the outer transaction commits.
    """
    if not settings.redis_enabled:
        return
    sync_session = session.sync_session
    transaction = sync_session.get_nested_transaction() or sync_session.get_transaction()
    pending: list[PendingEnqueue] = sync_session.info.setdefault(_PENDING_INFO_KEY, [])
    pending.append(PendingEnqueue(name, item_id, due, transaction))
    if not event.contains(sync_session, "after_commit", _enqueue_pending):
        event.listen(sync_session, "after_commit", _enqueue_pending)
        event.listen(sync_session, "after_soft_rollback", _drop_rolled_back)


def _enqueue_pending(sync_session: Session) -> None:
    if sync_session.get_nested_transaction() is not None:
        return  # a savepoint committed; wait for the outer transaction
    pending: list[PendingEnqueue] | None = sync_session.info.pop(_PENDING_INFO_KEY, None)
    if not pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("redis_queue no running loop; %d rows wait for the database scan", len(pending))
        return
    track_task(loop.create_task(_enqueue_all(pending), name="redis-queue-enqueue"))


def _within(transaction: SessionTransaction | None, rolled_back: SessionTransaction) -> bool:
    while transaction is not None:
        if transaction is rolled_back:
            return True
        transaction = transaction.parent
    return False


def _drop_rolled_back(sync_session: Session, previous_transaction: SessionTransaction) -> None:
    """Drop rows written in the rolled-back transaction or its savepoints; keep the rest."""
    pending: list[PendingEnqueue] | None = sync_session.info.get(_PENDING_INFO_KEY)
    if not pending:
        return
    kept = [item for item in pending if not _within(item.transaction, previous_transaction)]
    if previous_transaction.parent is None or not kept:
        sync_session.info.pop(_PENDING_INFO_KEY, None)
    else:
        sync_session.info[_PENDING_INFO_KEY] = kept


async def _enqueue_all(pending: list[PendingEnqueue]) -> None:
    for item in pending:
        await enqueue(item.name, [item.item_id], item.due)


async def move_due(client: Redis, name: str) -> int:
    """Move retries whose time has come from the delayed set to the ready list."""
    members = await client.zrangebyscore(delayed_key(name), "-inf", time.time())
    moved = 0
    for member in members:
        # Only the caller that removes the member pushes it, so it is never moved twice.
        if await client.zrem(delayed_key(name), member):
            await client.rpush(ready_key(name), member)
            moved += 1
    return moved


async def wait_for_ids(client: Redis, name: str, timeout: float) -> list[int]:
    """Block up to ``timeout`` seconds for IDs, then drain any others already queued."""
    item = await client.blpop([ready_key(name)], timeout=timeout)
    if item is None:
        return []
    raw_ids = [item[1]]
    more = await client.lpop(ready_key(name), DRAIN_BATCH)
    if more:
        raw_ids.extend(more)
    ids = []
    for raw in raw_ids:
        item_id = parse_id(raw)
        if item_id is None:
            logger.warning("redis_queue ignored invalid id queue=%s", name)
            continue
        ids.append(item_id)
    return ids


async def run_worker(
    name: str,
    process_due: Callable[[], Awaitable[int]],
    *,
    reconcile_secs: float = RECONCILE_SECS,
    outage_scan_secs: float = OUTAGE_SCAN_SECS,
    wait_secs: float = WAIT_SECS,
) -> None:
    """Call ``process_due`` when IDs arrive, and on a slow timer as a safety net.

    Recoverable failures (``RECOVERABLE_SCAN_ERRORS``) are retried; any other exception
    propagates and ends the worker.

    ``process_due`` claims and processes one bounded batch of due rows from the database
    and returns how many it handled; it is called again until a batch finds none.
    """
    client = get_client()
    scan_outage = OutageLog(logger, f"redis_queue {name} scan failed")
    last_scan = float("-inf")
    healthy = client is not None
    while True:
        woke = False
        if client is None:
            # No usable client (malformed REDIS_URL): keep delivering from database scans.
            await asyncio.sleep(wait_secs)
        else:
            try:
                await move_due(client, name)
                woke = bool(await wait_for_ids(client, name, wait_secs))
                healthy = True
                _outage.recovered()
            except (RedisError, OSError):
                healthy = False
                _outage.failed()
                await asyncio.sleep(wait_secs)

        interval = reconcile_secs if healthy else outage_scan_secs
        if not woke and time.monotonic() - last_scan < interval:
            continue
        last_scan = time.monotonic()
        try:
            # Drain the whole backlog, for example rows whose IDs a Redis restart lost, one
            # bounded batch at a time, yielding between batches so other tasks keep running.
            while await process_due():
                await asyncio.sleep(0)
        except RECOVERABLE_SCAN_ERRORS:
            scan_outage.failed()
            # Retry the scan after the back-off instead of waiting for the next reconcile.
            last_scan = float("-inf")
            await asyncio.sleep(outage_scan_secs)
        else:
            scan_outage.recovered()
