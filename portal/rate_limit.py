from __future__ import annotations

import asyncio
import time
from threading import Lock
from typing import Dict, Tuple

# Simple in-memory rate limiter for auth endpoints.
# Format: { "action:identifier": [timestamp1, timestamp2, ...] }
_rates: dict[str, list[float]] = {}
_lock = Lock()


def check_rate_limit(action: str, identifier: str, max_requests: int, window_seconds: int = 3600) -> bool:
    """Check if the given action/identifier has exceeded the rate limit.

    Returns True if allowed, False if rate limited.
    """
    key = f"{action}:{identifier}"
    now = time.time()
    cutoff = now - window_seconds

    with _lock:
        if key not in _rates:
            _rates[key] = []

        # Filter out old requests
        _rates[key] = [ts for ts in _rates[key] if ts > cutoff]

        if len(_rates[key]) >= max_requests:
            return False

        _rates[key].append(now)
        return True


class InMemoryRateLimiter:
    """
    Async-safe in-memory sliding window rate limiter.

    Suitable for single-process ASGI event loops. Note that this state
    is lost on restart and does not scale horizontally. If VoxBento scales
    out, this should be replaced with a Redis-backed token bucket.
    """
    def __init__(self, max_requests: int, window_seconds: int):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        # key -> (count, reset_time)
        self._store: Dict[str, Tuple[int, float]] = {}
        self._lock = asyncio.Lock()

    async def is_rate_limited(self, key: str) -> bool:
        now = time.time()

        async with self._lock:
            if key in self._store:
                count, reset_time = self._store[key]
                if now > reset_time:
                    # Window expired, reset
                    self._store[key] = (1, now + self.window_seconds)
                    return False

                if count >= self.max_requests:
                    return True

                # Increment count
                self._store[key] = (count + 1, reset_time)
                return False
            else:
                self._store[key] = (1, now + self.window_seconds)
                return False

    async def cleanup(self):
        """Periodic cleanup to prevent unbounded memory growth."""
        now = time.time()
        async with self._lock:
            # Create list of keys to delete to avoid modifying dict while iterating
            to_delete = [k for k, v in self._store.items() if now > v[1]]
            for k in to_delete:
                del self._store[k]

# Global instances
# 10 requests per minute per IP for authorization
auth_rate_limiter = InMemoryRateLimiter(max_requests=10, window_seconds=60)

# 100 requests per minute per client_id for token exchange
token_rate_limiter = InMemoryRateLimiter(max_requests=100, window_seconds=60)
