"""Optional Redis dispatch for the webhook and email outboxes (portal/redis_queue.py)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from portal import redis_queue
from portal.config import settings


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    monkeypatch.setattr(settings, "redis_url", "redis://test:6379/0")
    monkeypatch.setattr(redis_queue, "_client", client)
    return client


@pytest.mark.parametrize(("raw", "expected"), [(b"42", 42), ("7", 7), (b"0", None), (b"-3", None), (b"x", None)])
def test_parse_id_accepts_only_positive_integers(raw: bytes | str, expected: int | None) -> None:
    assert redis_queue.parse_id(raw) == expected


def test_naive_datetimes_are_treated_as_utc() -> None:
    aware = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    assert redis_queue.epoch_seconds(aware.replace(tzinfo=None)) == aware.timestamp()


@pytest.mark.anyio
async def test_enqueue_is_a_no_op_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(redis_queue, "_client", None)
    await redis_queue.enqueue(redis_queue.WEBHOOKS, [1, 2])
    assert redis_queue.get_client() is None


@pytest.mark.anyio
async def test_due_ids_go_to_ready_and_future_ids_to_delayed(redis: FakeAsyncRedis) -> None:
    await redis_queue.enqueue(redis_queue.EMAIL, [1, 2])
    await redis_queue.enqueue(redis_queue.EMAIL, [3], datetime.now(timezone.utc) + timedelta(minutes=5))
    assert await redis.lrange(redis_queue.ready_key(redis_queue.EMAIL), 0, -1) == [b"1", b"2"]
    assert await redis.zrange(redis_queue.delayed_key(redis_queue.EMAIL), 0, -1) == [b"3"]


@pytest.mark.anyio
async def test_move_due_moves_only_due_retries(redis: FakeAsyncRedis) -> None:
    name = redis_queue.WEBHOOKS
    await redis.zadd(redis_queue.delayed_key(name), {"5": 1.0, "6": 4_000_000_000.0})
    assert await redis_queue.move_due(redis, name) == 1
    assert await redis.lrange(redis_queue.ready_key(name), 0, -1) == [b"5"]
    assert await redis.zrange(redis_queue.delayed_key(name), 0, -1) == [b"6"]


@pytest.mark.anyio
async def test_wait_drains_burst_and_skips_invalid_ids(redis: FakeAsyncRedis) -> None:
    name = redis_queue.WEBHOOKS
    await redis.rpush(redis_queue.ready_key(name), "1", "bogus", "2")
    assert await redis_queue.wait_for_ids(redis, name, 0.1) == [1, 2]
    assert await redis_queue.wait_for_ids(redis, name, 0.1) == []


@pytest.mark.anyio
async def test_enqueue_survives_redis_outage(monkeypatch: pytest.MonkeyPatch, redis: FakeAsyncRedis) -> None:
    async def down(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "rpush", down)
    await redis_queue.enqueue(redis_queue.EMAIL, [1])


async def _session() -> tuple[AsyncSession, object]:
    engine = create_async_engine("sqlite+aiosqlite://")
    return AsyncSession(engine), engine


@pytest.mark.anyio
async def test_enqueue_after_commit_waits_for_commit(redis: FakeAsyncRedis) -> None:
    session, engine = await _session()
    async with session:
        await session.begin()
        redis_queue.enqueue_after_commit(session, redis_queue.EMAIL, 9)
        assert await redis.llen(redis_queue.ready_key(redis_queue.EMAIL)) == 0
        await session.commit()
    await asyncio.sleep(0.05)
    assert await redis.lrange(redis_queue.ready_key(redis_queue.EMAIL), 0, -1) == [b"9"]
    await engine.dispose()


@pytest.mark.anyio
async def test_enqueue_after_commit_drops_ids_on_rollback(redis: FakeAsyncRedis) -> None:
    session, engine = await _session()
    async with session:
        await session.begin()
        redis_queue.enqueue_after_commit(session, redis_queue.EMAIL, 9)
        await session.rollback()
        await session.begin()
        await session.commit()
    await asyncio.sleep(0.05)
    assert await redis.llen(redis_queue.ready_key(redis_queue.EMAIL)) == 0
    await engine.dispose()


async def _ready_ids(redis: FakeAsyncRedis) -> list[bytes]:
    await asyncio.sleep(0.05)  # the post-commit push runs as a background task
    return await redis.lrange(redis_queue.ready_key(redis_queue.EMAIL), 0, -1)


@pytest.mark.anyio
async def test_savepoint_commit_waits_for_the_outer_commit(redis: FakeAsyncRedis) -> None:
    session, engine = await _session()
    async with session:
        await session.begin()
        savepoint = await session.begin_nested()
        redis_queue.enqueue_after_commit(session, redis_queue.EMAIL, 9)
        await savepoint.commit()
        assert await _ready_ids(redis) == []  # the outer transaction is still open
        await session.commit()
    assert await _ready_ids(redis) == [b"9"]
    await engine.dispose()


@pytest.mark.anyio
async def test_savepoint_rollback_drops_only_its_own_rows(redis: FakeAsyncRedis) -> None:
    session, engine = await _session()
    async with session:
        await session.begin()
        redis_queue.enqueue_after_commit(session, redis_queue.EMAIL, 1)
        savepoint = await session.begin_nested()
        redis_queue.enqueue_after_commit(session, redis_queue.EMAIL, 2)
        inner = await session.begin_nested()
        redis_queue.enqueue_after_commit(session, redis_queue.EMAIL, 3)
        await inner.commit()
        await savepoint.rollback()  # discards rows 2 and 3, including 3's committed savepoint
        redis_queue.enqueue_after_commit(session, redis_queue.EMAIL, 4)
        await session.commit()
    assert await _ready_ids(redis) == [b"1", b"4"]
    await engine.dispose()


@pytest.mark.anyio
async def test_outer_rollback_drops_rows_from_committed_savepoints(redis: FakeAsyncRedis) -> None:
    session, engine = await _session()
    async with session:
        await session.begin()
        savepoint = await session.begin_nested()
        redis_queue.enqueue_after_commit(session, redis_queue.EMAIL, 5)
        await savepoint.commit()
        await session.rollback()
        await session.begin()
        await session.commit()
    assert await _ready_ids(redis) == []
    await engine.dispose()


@pytest.mark.anyio
async def test_worker_scans_on_start_on_wake_and_keeps_draining(redis: FakeAsyncRedis) -> None:
    calls: list[str] = []
    backlog = [3, 0]  # first wake finds rows, then the scan is repeated until it finds none

    async def process_due() -> int:
        calls.append("scan")
        return backlog.pop(0) if backlog else 0

    task = asyncio.create_task(
        redis_queue.run_worker(redis_queue.WEBHOOKS, process_due, reconcile_secs=60, wait_secs=0.05)
    )
    await asyncio.sleep(0.1)
    assert len(calls) == 2  # start-up scan, repeated once because it found rows
    await redis_queue.enqueue(redis_queue.WEBHOOKS, [1])
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) == 3


@pytest.mark.anyio
async def test_worker_falls_back_to_database_scans_when_redis_is_down(
    monkeypatch: pytest.MonkeyPatch, redis: FakeAsyncRedis
) -> None:
    async def down(*args: object, **kwargs: object) -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "zrangebyscore", down)
    calls = 0

    async def process_due() -> int:
        nonlocal calls
        calls += 1
        return 0

    task = asyncio.create_task(
        redis_queue.run_worker(redis_queue.EMAIL, process_due, reconcile_secs=60, outage_scan_secs=0.05, wait_secs=0.02)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls >= 3


@pytest.mark.anyio
async def test_worker_survives_processing_errors(redis: FakeAsyncRedis) -> None:
    calls = 0

    async def process_due() -> int:
        nonlocal calls
        calls += 1
        raise RuntimeError("database unavailable")

    task = asyncio.create_task(
        redis_queue.run_worker(
            redis_queue.EMAIL, process_due, reconcile_secs=0.05, outage_scan_secs=0.05, wait_secs=0.02
        )
    )
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls >= 2


@pytest.mark.anyio
async def test_unusable_url_keeps_the_worker_on_database_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    # Bypasses settings validation, which rejects this URL at startup.
    monkeypatch.setattr(settings, "redis_url", "redis://redis:abc/0")
    monkeypatch.setattr(redis_queue, "_client", None)
    monkeypatch.setattr(redis_queue, "_client_error_logged", False)
    assert redis_queue.get_client() is None
    await redis_queue.enqueue(redis_queue.EMAIL, [1])  # a no-op, not an error
    calls = 0

    async def process_due() -> int:
        nonlocal calls
        calls += 1
        return 0

    task = asyncio.create_task(
        redis_queue.run_worker(redis_queue.EMAIL, process_due, reconcile_secs=60, outage_scan_secs=0.05, wait_secs=0.02)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert calls >= 3


@pytest.mark.anyio
async def test_worker_drains_a_large_backlog_in_one_scan(redis: FakeAsyncRedis) -> None:
    backlog = 1_000  # far more batches than one wake-up used to allow
    calls = 0

    async def process_due() -> int:
        nonlocal backlog, calls
        calls += 1
        batch = min(backlog, 10)
        backlog -= batch
        return batch

    task = asyncio.create_task(
        redis_queue.run_worker(redis_queue.WEBHOOKS, process_due, reconcile_secs=60, wait_secs=0.05)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert backlog == 0
    assert calls == 101  # 100 full batches, then one that finds nothing


@pytest.mark.anyio
async def test_failed_scan_is_retried_without_waiting_for_the_reconcile(redis: FakeAsyncRedis) -> None:
    calls = 0

    async def process_due() -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("database unreachable")
        return 0

    task = asyncio.create_task(
        redis_queue.run_worker(redis_queue.EMAIL, process_due, reconcile_secs=60, outage_scan_secs=0.05, wait_secs=0.02)
    )
    await asyncio.sleep(0.3)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert calls == 2  # retried after the back-off, with Redis healthy and no new IDs
