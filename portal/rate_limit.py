from __future__ import annotations

import time
from threading import Lock

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
