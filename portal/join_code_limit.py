"""Per-IP limit on failed listener join-code attempts.

Shared by the listener HTTP routes and the caption WebSocket so a client cannot
sidestep the HTTP limit by guessing codes over ``/ws/captions``.
"""

from __future__ import annotations

import time

JOIN_CODE_RATE_LIMIT = 10
JOIN_CODE_RATE_WINDOW_SECONDS = 60
_join_code_attempts: dict[str, tuple[int, float]] = {}


def register_failed_attempt(client_ip: str) -> bool:
    """Record a failed join-code attempt for `client_ip`.

    Returns True if the client has exceeded the allowed attempts within the window.
    """
    now = time.monotonic()
    count, window_start = _join_code_attempts.get(client_ip, (0, now))
    if now - window_start > JOIN_CODE_RATE_WINDOW_SECONDS:
        count, window_start = 0, now
    count += 1
    _join_code_attempts[client_ip] = (count, window_start)
    return count > JOIN_CODE_RATE_LIMIT


def reset_attempts(client_ip: str) -> None:
    _join_code_attempts.pop(client_ip, None)
