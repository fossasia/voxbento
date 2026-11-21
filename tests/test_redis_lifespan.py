"""The lifespan picks the Redis or database-polling outbox workers from REDIS_URL."""

from __future__ import annotations

import asyncio

import pytest

from fastapi_app import app, lifespan
from portal.config import settings

pytestmark = pytest.mark.anyio

LOOPS = (
    "portal.webhooks.worker.webhook_worker_loop",
    "portal.webhooks.worker.redis_webhook_worker_loop",
    "portal.notifications.notification_worker_loop",
    "portal.notifications.redis_notification_worker_loop",
)


@pytest.fixture(autouse=True)
def _test_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("portal.config.Settings.validate_production_secrets", lambda self: None)


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    names: set[str] = set()
    for path in LOOPS:

        async def _stub(path: str = path) -> None:
            names.add(path.rsplit(".", 1)[1])
            await asyncio.Event().wait()

        monkeypatch.setattr(path, _stub)
    return names


async def _run_lifespan() -> None:
    async with lifespan(app):
        for _ in range(5):
            await asyncio.sleep(0)


async def test_polling_workers_run_without_redis(monkeypatch: pytest.MonkeyPatch, started: set[str]) -> None:
    monkeypatch.setattr(settings, "redis_url", "")
    await _run_lifespan()
    assert started == {"webhook_worker_loop", "notification_worker_loop"}


async def test_redis_workers_run_with_redis(monkeypatch: pytest.MonkeyPatch, started: set[str]) -> None:
    monkeypatch.setattr(settings, "redis_url", "redis://test:6379/0")
    await _run_lifespan()
    assert started == {"redis_webhook_worker_loop", "redis_notification_worker_loop"}


async def test_a_crashed_redis_worker_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(settings, "redis_url", "redis://test:6379/0")

    async def crash() -> None:
        raise KeyError("bug")

    async def idle() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr("portal.webhooks.worker.redis_webhook_worker_loop", crash)
    monkeypatch.setattr("portal.notifications.redis_notification_worker_loop", idle)
    with caplog.at_level("ERROR", logger="portal.tasks"):
        await _run_lifespan()
    assert any("redis-webhook-worker" in record.getMessage() for record in caplog.records)
