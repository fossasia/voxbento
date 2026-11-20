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
from datetime import datetime, timezone

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

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
# Upper bound on back-to-back scans after a wake-up, so a large backlog cannot starve the loop.
MAX_SCANS_PER_WAKE = 50
# IDs drained per wake-up; one database scan serves the whole burst.
DRAIN_BATCH = 100

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


def enqueue_after_commit(session: AsyncSession, name: str, item_id: int, due: datetime | None = None) -> None:
    """Enqueue once the caller's transaction commits; a rollback enqueues nothing.

    Lets a row be written in its trigger's transaction (the outbox pattern) without the
    worker being woken for a row that may never exist.
    """
    if not settings.redis_enabled:
        return
    sync_session = session.sync_session
    pending: list[tuple[str, int, datetime | None]] = sync_session.info.setdefault(_PENDING_INFO_KEY, [])
    pending.append((name, item_id, due))
    if not event.contains(sync_session, "after_commit", _enqueue_pending):
        event.listen(sync_session, "after_commit", _enqueue_pending)
        event.listen(sync_session, "after_rollback", _drop_pending)


def _enqueue_pending(sync_session: Session) -> None:
    pending = sync_session.info.pop(_PENDING_INFO_KEY, None)
    if not pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("redis_queue no running loop; %d rows wait for the database scan", len(pending))
        return
    track_task(loop.create_task(_enqueue_all(pending), name="redis-queue-enqueue"))


def _drop_pending(sync_session: Session) -> None:
    sync_session.info.pop(_PENDING_INFO_KEY, None)


async def _enqueue_all(pending: list[tuple[str, int, datetime | None]]) -> None:
    for name, item_id, due in pending:
        await enqueue(name, [item_id], due)


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

    ``process_due`` claims and processes due rows from the database and returns how many
    it handled; it is called again while it keeps finding rows.
    """
    client = get_client()
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
            for _ in range(MAX_SCANS_PER_WAKE):
                if not await process_due():
                    break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the worker must keep running; rows stay in the database
            logger.exception("redis_queue %s worker error", name)
            await asyncio.sleep(outage_scan_secs)
