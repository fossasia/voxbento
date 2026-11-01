"""Shared, bounded failed-code budget for HTTP and caption WebSocket access."""

from __future__ import annotations

import ipaddress
import math
import secrets
import time
from collections import OrderedDict

JOIN_CODE_RATE_LIMIT = 10
JOIN_CODE_RATE_WINDOW_SECONDS = 60
MAX_CLIENTS = 10000
# Ordered by window start: entries are only added with start=now and never moved.
_join_code_attempts: OrderedDict[str, tuple[int, float]] = OrderedDict()


class JoinCodeThrottled(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("Join-code attempt budget exhausted")


def _client_key(client_ip: str) -> str:
    """IPv6 clients share one budget per /64, since a single host can rotate addresses inside it."""
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return client_ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((address, 64), strict=False))
    return str(address)


def _purge_expired(now: float) -> None:
    # Expired windows are at the front, so stop at the first live one.
    while _join_code_attempts:
        _, start = next(iter(_join_code_attempts.values()))
        if now - start < JOIN_CODE_RATE_WINDOW_SECONDS:
            return
        _join_code_attempts.popitem(last=False)


def verify_join_code(client_ip: str, submitted: str | None, expected: str | None) -> bool:
    """Check the budget before comparing any submitted guess, including cookies.

    Calls contain no awaits, so checking and recording are atomic in the portal's
    single event loop. When the bounded table is full the oldest window is
    evicted, so a flood of new addresses cannot lock out other clients.
    Account authentication bypasses this budget.
    """
    if not submitted:
        return False
    now = time.monotonic()
    _purge_expired(now)
    key = _client_key(client_ip)
    count, start = _join_code_attempts.get(key, (0, now))
    if count >= JOIN_CODE_RATE_LIMIT:
        raise JoinCodeThrottled(max(1, math.ceil(JOIN_CODE_RATE_WINDOW_SECONDS - (now - start))))
    # A correct code must not reset the budget, or a code for one event would refill guesses for another.
    if expected and secrets.compare_digest(submitted.encode(), expected.encode()):
        return True
    if key not in _join_code_attempts and len(_join_code_attempts) >= MAX_CLIENTS:
        _join_code_attempts.popitem(last=False)
    _join_code_attempts[key] = (count + 1, start)
    return False
