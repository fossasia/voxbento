"""Webhook delivery woken by Redis (portal/webhooks/worker.py + portal/redis_queue.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fakeredis import FakeAsyncRedis

import portal.webhooks.worker as worker
from portal import redis_queue
from portal.config import settings
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import DeveloperAccount, WebhookDelivery, WebhookSubscription

# conftest.py replaces worker.enqueue_webhook with a no-op for every test; keep the real one.
real_enqueue_webhook = worker.enqueue_webhook

EVENT = "session.status_changed"


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


async def _subscribe() -> int:
    async with get_session() as db:
        user = await create_user(db, email="dev@example.com", display_name="Developer")
        account = DeveloperAccount(user_id=user.id, status="approved")
        db.add(account)
        await db.flush()
        sub = WebhookSubscription(
            developer_account_id=account.id,
            target_url="https://example.com/webhook",
            event_types=[EVENT],
            secret_key="secret",
            is_active=True,
        )
        db.add(sub)
        await db.flush()
        return sub.id


async def _deliveries() -> list[WebhookDelivery]:
    async with get_session() as db:
        result = await db.execute(WebhookDelivery.__table__.select().order_by(WebhookDelivery.id))
        return list(result.all())


@pytest.mark.anyio
async def test_enqueue_pushes_committed_ids_to_redis(redis: FakeAsyncRedis) -> None:
    await _subscribe()
    await real_enqueue_webhook(EVENT, {"x": 1})
    rows = await _deliveries()
    assert len(rows) == 1
    assert await redis.lrange(redis_queue.ready_key(redis_queue.WEBHOOKS), 0, -1) == [str(rows[0].id).encode()]


@pytest.mark.anyio
async def test_enqueue_without_subscribers_pushes_nothing(redis: FakeAsyncRedis) -> None:
    await real_enqueue_webhook(EVENT, {"x": 1})
    assert await redis.llen(redis_queue.ready_key(redis_queue.WEBHOOKS)) == 0


@pytest.mark.anyio
async def test_enqueue_without_redis_still_writes_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "redis_url", "")
    await _subscribe()
    await real_enqueue_webhook(EVENT, {"x": 1})
    assert len(await _deliveries()) == 1


@pytest.mark.anyio
async def test_failed_delivery_is_scheduled_for_retry_in_redis(
    monkeypatch: pytest.MonkeyPatch, redis: FakeAsyncRedis
) -> None:
    monkeypatch.setattr(worker, "validate_ssrf", AsyncMock(return_value=["93.184.216.34"]))
    await _subscribe()
    await real_enqueue_webhook(EVENT, {"x": 1})
    client = MagicMock()
    client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))

    assert await worker.process_due_webhooks(client) == 1

    (row,) = await _deliveries()
    assert row.status == "failed"
    assert row.attempt_count == 1
    delayed = await redis.zrange(redis_queue.delayed_key(redis_queue.WEBHOOKS), 0, -1, withscores=True)
    assert [member for member, _ in delayed] == [str(row.id).encode()]
    assert delayed[0][1] == pytest.approx(redis_queue.epoch_seconds(row.next_attempt_at), abs=1)
    # Not due yet, so the next scan claims nothing.
    assert await worker.process_due_webhooks(client) == 0


@pytest.mark.anyio
async def test_successful_delivery_schedules_no_retry(monkeypatch: pytest.MonkeyPatch, redis: FakeAsyncRedis) -> None:
    monkeypatch.setattr(worker, "validate_ssrf", AsyncMock(return_value=["93.184.216.34"]))
    await _subscribe()
    await real_enqueue_webhook(EVENT, {"x": 1})
    response = httpx.Response(200, request=httpx.Request("POST", "https://example.com/webhook"))
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    assert await worker.process_due_webhooks(client) == 1

    (row,) = await _deliveries()
    assert row.status == "succeeded"
    assert await redis.zcard(redis_queue.delayed_key(redis_queue.WEBHOOKS)) == 0
