from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ["API_KEY_ENCRYPTION_KEY"] = "test-encryption-key-value-for-all-tests"
os.environ["BOOTH_ACCESS_TOKEN"] = ""
# Most tests expect a closed booth socket to leave at once and every app start to
# begin with empty booths. tests/test_booth_recovery.py turns both on where it tests them.
os.environ["BOOTH_RECONNECT_GRACE_SECONDS"] = "0"
os.environ["BOOTH_STATE_RECOVERY_ENABLED"] = "false"


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
    from portal.rate_limit import reset_login_limiter

    reset_login_limiter()

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
    reset_login_limiter()
    portal.webhooks.worker.webhook_worker_loop = original_loop
    portal.webhooks.worker.enqueue_webhook = original_enqueue


@pytest.fixture(params=["asyncio"])
def anyio_backend(request):
    return request.param


@pytest.fixture
def auto_csrf(monkeypatch):
    """Send a valid CSRF token with every state-changing request made through httpx.

    For test modules about something other than CSRF: their management requests carry the
    token a real page would, bound to the session cookie in the request. CSRF rejection is
    covered in tests/test_admin_panel.py, which does not use this fixture.
    """
    from http.cookies import SimpleCookie

    import httpx
    import jwt

    from portal.auth import ADMIN_CSRF_COOKIE, decode_token, generate_admin_csrf_token

    def add_token(request):
        if request.method in {"GET", "HEAD", "OPTIONS"} or "x-csrf-token" in request.headers:
            return
        cookies = SimpleCookie(request.headers.get("cookie", ""))
        session_id = "anonymous"
        for name in ("user_token", "session_token"):
            if name in cookies:
                try:
                    session_id = str(decode_token(cookies[name].value).get("jti") or session_id)
                except jwt.InvalidTokenError:
                    continue
                break
        token = generate_admin_csrf_token(session_id)
        pairs = [f"{name}={morsel.value}" for name, morsel in cookies.items() if name != ADMIN_CSRF_COOKIE]
        request.headers["cookie"] = "; ".join([*pairs, f"{ADMIN_CSRF_COOKIE}={token}"])
        request.headers["x-csrf-token"] = token

    async_send, sync_send = httpx.AsyncClient.send, httpx.Client.send

    async def send_async(self, request, **kwargs):
        add_token(request)
        return await async_send(self, request, **kwargs)

    def send_sync(self, request, **kwargs):
        add_token(request)
        return sync_send(self, request, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", send_async)
    monkeypatch.setattr(httpx.Client, "send", send_sync)
