from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ["API_KEY_ENCRYPTION_KEY"] = "test-encryption-key-value-for-all-tests"
os.environ["BOOTH_ACCESS_TOKEN"] = ""


ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest_plugins = ("anyio",)


@pytest.fixture(autouse=True)
def _reset_shared_http_client():
    """Ensure the shared httpx client is reset between tests so a stale client
    bound to a closed event loop never poisons subsequent tests."""
    import portal.globals as pg
    import portal.webhooks.worker

    # Disable the background webhook loop and enqueueing during tests to prevent SQLite lock contention
    # and hanging threads when the test event loop closes.
    original_loop = portal.webhooks.worker.webhook_worker_loop
    original_enqueue = portal.webhooks.worker.enqueue_webhook

    async def _dummy_loop():
        pass
    async def _dummy_enqueue(event_type: str, payload: dict):
        pass

    portal.webhooks.worker.webhook_worker_loop = _dummy_loop
    portal.webhooks.worker.enqueue_webhook = _dummy_enqueue

    pg.shared_http_client = None
    yield
    pg.shared_http_client = None
    portal.webhooks.worker.webhook_worker_loop = original_loop
    portal.webhooks.worker.enqueue_webhook = original_enqueue


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param
