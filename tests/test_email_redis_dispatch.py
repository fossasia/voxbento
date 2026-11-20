"""Email outbox woken by Redis (portal/notifications.py + portal/redis_queue.py)."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fakeredis import FakeAsyncRedis

from portal import redis_queue
from portal.config import settings
from portal.database import configure, dispose, get_session, init_db
from portal.models import EmailDelivery, utc_now
from portal.notifications import process_due_deliveries, queue_notification

LINK = {"verification_link": "https://example.invalid/v"}


@pytest.fixture(autouse=True)
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeAsyncRedis:
    client = FakeAsyncRedis()
    monkeypatch.setattr(settings, "redis_url", "redis://test:6379/0")
    monkeypatch.setattr(redis_queue, "_client", client)
    return client


async def _ready(redis: FakeAsyncRedis) -> list[bytes]:
    await asyncio.sleep(0.05)  # the post-commit push runs as a background task
    return await redis.lrange(redis_queue.ready_key(redis_queue.EMAIL), 0, -1)


@pytest.mark.anyio
async def test_committed_email_is_pushed_to_redis(redis: FakeAsyncRedis) -> None:
    async with get_session() as session:
        delivery = await queue_notification(session, "verification", "person@example.com", LINK)
        delivery_id = delivery.id
        assert await redis.llen(redis_queue.ready_key(redis_queue.EMAIL)) == 0  # not before commit
    assert await _ready(redis) == [str(delivery_id).encode()]


@pytest.mark.anyio
async def test_rolled_back_email_is_never_pushed(redis: FakeAsyncRedis) -> None:
    class Abort(Exception):
        pass

    with pytest.raises(Abort):
        async with get_session() as session:
            await queue_notification(session, "verification", "person@example.com", LINK)
            raise Abort
    assert await _ready(redis) == []


@pytest.mark.anyio
async def test_scheduled_email_waits_in_the_delayed_set(redis: FakeAsyncRedis) -> None:
    async with get_session() as session:
        await queue_notification(
            session, "verification", "person@example.com", LINK, scheduled_at=utc_now() + timedelta(hours=1)
        )
    assert await _ready(redis) == []
    assert await redis.zcard(redis_queue.delayed_key(redis_queue.EMAIL)) == 1


@pytest.mark.anyio
async def test_idempotent_requeue_pushes_once(redis: FakeAsyncRedis) -> None:
    for _ in range(2):
        async with get_session() as session:
            await queue_notification(session, "verification", "person@example.com", LINK, idempotency_key="once")
    assert len(await _ready(redis)) == 1


@pytest.mark.anyio
async def test_failed_send_is_scheduled_for_retry(monkeypatch: pytest.MonkeyPatch, redis: FakeAsyncRedis) -> None:
    monkeypatch.setattr("portal.notifications.settings.smtp_host", "smtp.example.invalid")

    async def fail(self: object, message: object) -> None:
        raise ConnectionError("refused")

    monkeypatch.setattr("portal.notifications.FastMail.send_message", fail)
    async with get_session() as session:
        delivery = await queue_notification(session, "verification", "person@example.com", LINK)
        delivery_id = delivery.id

    assert await process_due_deliveries() == 1

    async with get_session() as session:
        stored = await session.get(EmailDelivery, delivery_id)
        assert stored.status == "pending"
        retry_at = stored.scheduled_at
    delayed = await redis.zrange(redis_queue.delayed_key(redis_queue.EMAIL), 0, -1, withscores=True)
    assert [member for member, _ in delayed] == [str(delivery_id).encode()]
    assert delayed[0][1] == pytest.approx(redis_queue.epoch_seconds(retry_at), abs=1)


@pytest.mark.anyio
async def test_email_is_queued_normally_without_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "redis_url", "")
    async with get_session() as session:
        await queue_notification(session, "verification", "person@example.com", LINK)
    async with get_session() as session:
        assert len((await session.execute(EmailDelivery.__table__.select())).all()) == 1
