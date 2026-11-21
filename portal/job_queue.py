"""Optional Redis scheduling hints. Database rows remain authoritative and durable."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from portal.config import settings

logger = logging.getLogger(__name__)
JobKind = Literal["webhook", "email", "analysis"]
PENDING_KEY = "voxbento_jobs_pending"
COMMITTED_KEY = "voxbento_jobs_committed"
BATCH_SIZE = 100

# A popped hint is recoverable from the DB if the worker stops before claiming it.
POP_DUE = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, ARGV[2])
if #ids > 0 then redis.call('ZREM', KEYS[1], unpack(ids)) end
return ids
"""


@dataclass(frozen=True)
class Job:
    kind: JobKind
    id: int
    run_at: datetime


def timestamp(value: datetime) -> float:
    # SQLite returns naive UTC datetimes even for timezone-aware columns.
    return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()


def defer_job(session: AsyncSession, kind: JobKind, job_id: int, run_at: datetime) -> None:
    """Stage a hint on its transaction, including savepoint commit/rollback boundaries."""
    if settings.job_queue_backend != "redis":
        return
    sync = session.sync_session
    transaction = sync.get_nested_transaction() or sync.get_transaction()
    if transaction is None:
        raise RuntimeError("Job scheduling requires a database transaction")
    pending = sync.info.setdefault(PENDING_KEY, {})
    pending.setdefault(transaction, []).append(Job(kind, job_id, run_at))


@event.listens_for(Session, "after_commit")
def collect_committed_jobs(session: Session) -> None:
    transaction = session.get_nested_transaction() or session.get_transaction()
    jobs = session.info.get(PENDING_KEY, {}).pop(transaction, [])
    if jobs:
        if transaction.nested:
            session.info[PENDING_KEY].setdefault(transaction.parent, []).extend(jobs)
        else:
            session.info.setdefault(COMMITTED_KEY, []).extend(jobs)


@event.listens_for(Session, "after_rollback")
def discard_rolled_back_jobs(session: Session) -> None:
    transaction = session.get_nested_transaction() or session.get_transaction()
    session.info.get(PENDING_KEY, {}).pop(transaction, None)


async def publish_committed_jobs(session: AsyncSession) -> None:
    jobs = session.info.pop(COMMITTED_KEY, [])
    session.info.pop(PENDING_KEY, None)
    if jobs:
        await job_queue.schedule(jobs)


class RedisJobQueue:
    def __init__(self) -> None:
        self._redis: Redis | None = None
        self._reconciled: dict[JobKind, float] = {}
        self._unavailable = False

    def client(self) -> Redis:
        if self._redis is None:
            self._redis = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
                retry=Retry(NoBackoff(), 0),
                max_connections=10,
            )
        return self._redis

    def key(self, kind: JobKind) -> str:
        return f"{settings.job_queue_prefix}:{kind}"

    def unavailable(self) -> None:
        if not self._unavailable:
            # Never log the connection URL or exception; either can contain credentials.
            logger.warning("Job queue unavailable; using database polling")
        self._unavailable = True
        self._reconciled.clear()

    async def schedule(self, jobs: list[Job]) -> bool:
        if settings.job_queue_backend != "redis":
            return False
        try:
            async with self.client().pipeline(transaction=False) as pipeline:
                for job in jobs:
                    pipeline.zadd(self.key(job.kind), {str(job.id): timestamp(job.run_at)})
                await pipeline.execute()
            return True
        except (RedisError, OSError):
            self.unavailable()
            return False

    async def ready(self, kind: JobKind, limit: int = 10) -> list[int] | None:
        """None selects DB polling; an empty list means a healthy, idle Redis queue."""
        if settings.job_queue_backend != "redis":
            return None
        try:
            now = time.monotonic()
            if now - self._reconciled.get(kind, float("-inf")) >= settings.job_queue_reconcile_seconds:
                await self.client().ping()
                if not await self.reconcile(kind):
                    return None
                self._reconciled[kind] = now
            values = await self.client().eval(POP_DUE, 1, self.key(kind), datetime.now(timezone.utc).timestamp(), limit)
            ids = []
            for value in values:
                if isinstance(value, str) and value.isascii() and value.isdecimal() and 0 < int(value) <= 2**63 - 1:
                    ids.append(int(value))
                else:
                    logger.warning("Discarded an invalid job queue entry in %s", kind)
            if self._unavailable:
                logger.info("Job queue recovered; database reconciliation resumed")
                self._unavailable = False
            return ids
        except (RedisError, OSError):
            self.unavailable()
            return None

    async def reconcile(self, kind: JobKind) -> bool:
        # Local imports avoid the database -> queue -> database initialization cycle.
        from portal.database import get_session
        from portal.models import EmailDelivery, MeetingReport, WebhookDelivery, utc_now

        if kind == "email":
            from portal.notifications import recover_email_deliveries

            await recover_email_deliveries()
            model, due, statuses = EmailDelivery, EmailDelivery.scheduled_at, ("pending",)
        elif kind == "webhook":
            model, due, statuses = WebhookDelivery, WebhookDelivery.next_attempt_at, ("pending", "failed")
        else:
            model, due, statuses = MeetingReport, MeetingReport.created_at, ("pending",)
        horizon = utc_now() + timedelta(seconds=settings.job_queue_reconcile_seconds)
        after_id = 0
        while True:
            async with get_session() as session:
                rows = (
                    await session.execute(
                        select(model.id, due)
                        .where(model.id > after_id, model.status.in_(statuses), due <= horizon)
                        .order_by(model.id)
                        .limit(BATCH_SIZE)
                    )
                ).all()
            if not rows:
                return True
            if not await self.schedule([Job(kind, job_id, run_at) for job_id, run_at in rows]):
                return False
            after_id = rows[-1][0]

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
        self._reconciled.clear()
        self._unavailable = False


job_queue = RedisJobQueue()
