"""Tests for InMemoryRateLimiter and periodic cleanup (ISSUE-6).

Covers:
- Sliding window rate limiting allow / block behavior
- Independent tracking across different keys
- Window expiry and counter reset
- InMemoryRateLimiter.cleanup() purges expired keys and retains active keys
- Deterministic repeated calls to cleanup() (idempotency check)
- _rate_limit_cleanup_loop periodically executes cleanup on global instances
- Lifespan context manager in fastapi_app starts and terminates the cleanup task
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest

import fastapi_app
from fastapi_app import _rate_limit_cleanup_loop, app, lifespan
from portal.config import settings
from portal.rate_limit import InMemoryRateLimiter, auth_rate_limiter, token_rate_limiter

pytestmark = pytest.mark.anyio


@pytest.fixture
def limiter() -> InMemoryRateLimiter:
    """Provide a fresh InMemoryRateLimiter instance for test isolation."""
    return InMemoryRateLimiter(max_requests=3, window_seconds=60)


@pytest.fixture(autouse=True)
def _reset_global_rate_limiters():
    """Ensure global limiters have empty stores before and after each test."""
    auth_rate_limiter._store.clear()
    token_rate_limiter._store.clear()
    yield
    auth_rate_limiter._store.clear()
    token_rate_limiter._store.clear()


@pytest.fixture(autouse=True)
def _set_test_secrets(monkeypatch):
    """Ensure test secrets and settings are configured so lifespan secret validation succeeds."""
    monkeypatch.setattr(settings, "jwt_secret", "test-secret-key-that-is-long-enough-for-testing-purposes-1234567890")
    monkeypatch.setattr(settings, "secret_key", "test-secret-key-that-is-long-enough-for-testing-purposes-1234567890")
    monkeypatch.setattr(settings, "mediamtx_auth_hook_secret", "test-mediamtx-auth-hook-secret-for-testing-1234567890")
    monkeypatch.setattr(settings, "booth_access_token", "test-booth-access-token-for-testing-purposes-1234567890")
    monkeypatch.setenv("BOOTH_ACCESS_TOKEN", "test-booth-access-token-for-testing-purposes-1234567890")
    monkeypatch.setattr("portal.config.Settings.validate_production_secrets", lambda self: None)


class TestInMemoryRateLimiterBasic:
    """Tests for core rate limiting rules and sliding window behavior."""

    async def test_allows_up_to_max_requests(self, limiter: InMemoryRateLimiter):
        """Verify that requests within max_requests threshold are permitted."""
        assert await limiter.is_rate_limited("client1") is False
        assert await limiter.is_rate_limited("client1") is False
        assert await limiter.is_rate_limited("client1") is False

    async def test_blocks_on_exceeding_max_requests(self, limiter: InMemoryRateLimiter):
        """Verify that requests exceeding max_requests are throttled."""
        for _ in range(3):
            assert await limiter.is_rate_limited("client1") is False
        assert await limiter.is_rate_limited("client1") is True

    async def test_different_keys_tracked_independently(self, limiter: InMemoryRateLimiter):
        """Verify that different client keys have separate rate limit counters."""
        for _ in range(3):
            assert await limiter.is_rate_limited("client1") is False
        assert await limiter.is_rate_limited("client1") is True
        assert await limiter.is_rate_limited("client2") is False

    async def test_window_expiry_resets_count(self, limiter: InMemoryRateLimiter):
        """Verify that elapsed window time resets the request count and permits traffic."""
        for _ in range(3):
            await limiter.is_rate_limited("client1")
        assert await limiter.is_rate_limited("client1") is True

        # Simulate expiration of the sliding window
        limiter._store["client1"] = (limiter.window_seconds, [time.time() - 100])
        assert await limiter.is_rate_limited("client1") is False


class TestInMemoryRateLimiterCleanup:
    """Tests for InMemoryRateLimiter.cleanup() method."""

    async def test_cleanup_removes_expired_entries(self, limiter: InMemoryRateLimiter):
        """Verify that cleanup() removes entries whose reset time is in the past."""
        await limiter.is_rate_limited("user1")
        await limiter.is_rate_limited("user2")
        now = time.time()
        limiter._store["user1"] = (limiter.window_seconds, [now - 100])
        limiter._store["user2"] = (limiter.window_seconds, [now - 100])

        assert len(limiter._store) == 2
        await limiter.cleanup()
        assert len(limiter._store) == 0
        assert "user1" not in limiter._store
        assert "user2" not in limiter._store

    async def test_cleanup_keeps_active_entries(self, limiter: InMemoryRateLimiter):
        """Verify that cleanup() preserves entries whose reset time is in the future."""
        await limiter.is_rate_limited("expired_user")
        await limiter.is_rate_limited("active_user")
        now = time.time()
        limiter._store["expired_user"] = (limiter.window_seconds, [now - 100])
        limiter._store["active_user"] = (limiter.window_seconds, [now])

        assert len(limiter._store) == 2
        await limiter.cleanup()
        assert len(limiter._store) == 1
        assert "expired_user" not in limiter._store
        assert "active_user" in limiter._store
        assert limiter._store["active_user"] == (limiter.window_seconds, [now])

    async def test_cleanup_on_empty_store_is_a_no_op(self, limiter: InMemoryRateLimiter):
        """Verify that cleanup() completes safely on an empty store."""
        assert len(limiter._store) == 0
        await limiter.cleanup()
        assert len(limiter._store) == 0

    async def test_repeated_cleanup_is_idempotent(self, limiter: InMemoryRateLimiter):
        """Verify that calling cleanup() multiple times safely evicts on first run and is a no-op afterwards."""
        await limiter.is_rate_limited("expired_1")
        await limiter.is_rate_limited("active_1")
        now = time.time()
        limiter._store["expired_1"] = (limiter.window_seconds, [now - 100])
        limiter._store["active_1"] = (limiter.window_seconds, [now])

        # First cleanup call removes expired entry
        await limiter.cleanup()
        assert "expired_1" not in limiter._store
        assert "active_1" in limiter._store
        assert len(limiter._store) == 1

        # Second cleanup call is idempotent and safe (no-op)
        await limiter.cleanup()
        assert "expired_1" not in limiter._store
        assert "active_1" in limiter._store
        assert len(limiter._store) == 1

    async def test_cleanup_does_not_affect_rate_limit_logic(self, limiter: InMemoryRateLimiter):
        """Verify that cleanup() does not corrupt or alter rate limit evaluation for active keys."""
        await limiter.is_rate_limited("user")
        now = time.time()
        limiter._store["user"] = (limiter.window_seconds, [now])
        await limiter.cleanup()

        assert await limiter.is_rate_limited("user") is False
        assert await limiter.is_rate_limited("user") is False
        assert await limiter.is_rate_limited("user") is True


class TestRateLimitCleanupLoop:
    """Tests for the periodic background cleanup loop."""

    async def test_cleanup_loop_purges_global_limiters(self):
        """Verify that the background cleanup loop purges expired keys across global limiters."""
        now = time.time()
        auth_rate_limiter._store["ip_expired"] = (auth_rate_limiter.window_seconds, [now - 100])
        auth_rate_limiter._store["ip_active"] = (auth_rate_limiter.window_seconds, [now])
        token_rate_limiter._store["token_expired"] = (token_rate_limiter.window_seconds, [now - 100])
        token_rate_limiter._store["token_active"] = (token_rate_limiter.window_seconds, [now])

        task = asyncio.create_task(_rate_limit_cleanup_loop(interval_seconds=0.01))
        try:

            async def _wait_for_purge():
                while "ip_expired" in auth_rate_limiter._store or "token_expired" in token_rate_limiter._store:
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(_wait_for_purge(), timeout=5.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert "ip_expired" not in auth_rate_limiter._store
        assert "ip_active" in auth_rate_limiter._store
        assert "token_expired" not in token_rate_limiter._store
        assert "token_active" in token_rate_limiter._store

    async def test_cleanup_loop_cancels_cleanly(self):
        """Verify that cancelling the background cleanup task terminates cleanly."""
        task = asyncio.create_task(_rate_limit_cleanup_loop(interval_seconds=100))
        await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert task.cancelled()


class TestLifespanRateLimitCleanup:
    """Tests for FastAPI lifespan integration with rate limit cleanup."""

    async def test_lifespan_starts_and_stops_cleanup_loop(self, monkeypatch):
        """Assert that FastAPI lifespan creates and cancels the rate limit cleanup task."""
        loop_started = asyncio.Event()
        loop_cancelled = asyncio.Event()

        async def _stub_cleanup_loop():
            loop_started.set()
            try:
                while True:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                loop_cancelled.set()
                raise

        monkeypatch.setattr(fastapi_app, "_rate_limit_cleanup_loop", _stub_cleanup_loop)

        async with lifespan(app):
            await asyncio.sleep(0)
            assert loop_started.is_set()

        assert loop_cancelled.is_set()

    async def test_lifespan_completes_shutdown_when_webhook_task_fails(self, monkeypatch):
        """Assert that webhook failure does not prevent cleanup-task shutdown."""
        cleanup_started = asyncio.Event()
        cleanup_cancelled = asyncio.Event()

        async def _failing_webhook_loop():
            raise RuntimeError("webhook worker failed")

        async def _stub_cleanup_loop():
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_cancelled.set()
                raise

        monkeypatch.setattr("portal.webhooks.worker.webhook_worker_loop", _failing_webhook_loop)
        monkeypatch.setattr(fastapi_app, "_rate_limit_cleanup_loop", _stub_cleanup_loop)

        async with lifespan(app):
            await cleanup_started.wait()

        assert cleanup_cancelled.is_set()
