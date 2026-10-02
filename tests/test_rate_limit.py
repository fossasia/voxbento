"""Regression tests for portal.rate_limit.

Covers three defects in the in-memory limiters:

- ``check_rate_limit`` never removed keys, so a caller that controls the
  identifier (an email address on /login, an IP on /api/v1/tokens/listener)
  could grow ``_rates`` without bound.
- ``InMemoryRateLimiter._store`` had the same unbounded growth, and the
  ``cleanup()`` that was meant to reclaim it had no callers anywhere.
- ``InMemoryRateLimiter`` documented a sliding window but implemented a fixed
  one, so a caller straddling a window boundary got two full allowances.

The limiters are clock-driven, so these tests drive a fake clock instead of
sleeping: deterministic, and it keeps the suite fast.
"""

from __future__ import annotations

import pytest

from portal import rate_limit
from portal.rate_limit import InMemoryRateLimiter, check_rate_limit


class FakeClock:
    """Stands in for the ``time`` module inside portal.rate_limit."""

    def __init__(self, start: float = 1_000_000.0):
        self._now = start

    def time(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def clock(monkeypatch):
    """Replace the ``time`` reference in portal.rate_limit with a fake clock.

    Patching the module attribute keeps the fake scoped to portal.rate_limit
    rather than shadowing time.time() process-wide.
    """
    fake = FakeClock()
    monkeypatch.setattr(rate_limit, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def reset_module_state():
    """``_rates`` and the sweep clock are module-level and shared across tests."""
    rate_limit._rates.clear()
    rate_limit._last_sweep = 0.0
    yield
    rate_limit._rates.clear()
    rate_limit._last_sweep = 0.0


# ---------------------------------------------------------------------------
# check_rate_limit — existing behaviour must not regress
# ---------------------------------------------------------------------------


class TestCheckRateLimitBehaviour:
    def test_allows_up_to_the_limit_then_denies(self, clock):
        for _ in range(3):
            assert check_rate_limit("act", "id", max_requests=3, window_seconds=60) is True
        assert check_rate_limit("act", "id", max_requests=3, window_seconds=60) is False

    def test_identifiers_are_counted_independently(self, clock):
        assert check_rate_limit("act", "a", max_requests=1, window_seconds=60) is True
        assert check_rate_limit("act", "a", max_requests=1, window_seconds=60) is False
        # A different identifier must still have its own full allowance.
        assert check_rate_limit("act", "b", max_requests=1, window_seconds=60) is True

    def test_actions_are_counted_independently(self, clock):
        assert check_rate_limit("login", "id", max_requests=1, window_seconds=60) is True
        assert check_rate_limit("login", "id", max_requests=1, window_seconds=60) is False
        assert check_rate_limit("magic_link", "id", max_requests=1, window_seconds=60) is True

    def test_allowance_returns_once_the_window_passes(self, clock):
        assert check_rate_limit("act", "id", max_requests=1, window_seconds=60) is True
        assert check_rate_limit("act", "id", max_requests=1, window_seconds=60) is False
        clock.advance(61)
        assert check_rate_limit("act", "id", max_requests=1, window_seconds=60) is True

    def test_partial_window_does_not_restore_the_allowance(self, clock):
        assert check_rate_limit("act", "id", max_requests=1, window_seconds=60) is True
        clock.advance(59)
        assert check_rate_limit("act", "id", max_requests=1, window_seconds=60) is False

    def test_denial_does_not_extend_the_window(self, clock):
        """A blocked request must not count as a hit, or the window never drains."""
        assert check_rate_limit("act", "id", max_requests=1, window_seconds=60) is True
        for _ in range(5):
            clock.advance(1)
            assert check_rate_limit("act", "id", max_requests=1, window_seconds=60) is False
        # 61s after the single successful hit, the allowance is back.
        clock.advance(56)
        assert check_rate_limit("act", "id", max_requests=1, window_seconds=60) is True


# ---------------------------------------------------------------------------
# D1: check_rate_limit key eviction
# ---------------------------------------------------------------------------


class TestCheckRateLimitEviction:
    def test_expired_keys_are_evicted_on_sweep(self, clock):
        check_rate_limit("login", "victim@example.com", max_requests=10, window_seconds=60)
        assert "login:victim@example.com" in rate_limit._rates

        clock.advance(61)
        check_rate_limit("login", "other@example.com", max_requests=10, window_seconds=60)

        assert "login:victim@example.com" not in rate_limit._rates, (
            "a key whose window fully elapsed can never deny a request again and must be dropped"
        )

    def test_live_keys_survive_a_sweep(self, clock):
        check_rate_limit("login", "active@example.com", max_requests=10, window_seconds=3600)

        clock.advance(61)
        check_rate_limit("login", "other@example.com", max_requests=10, window_seconds=3600)

        assert "login:active@example.com" in rate_limit._rates

    def test_sweep_does_not_restore_a_throttled_allowance(self, clock):
        """Eviction must not hand a still-throttled identifier a fresh allowance."""
        assert check_rate_limit("act", "id", max_requests=2, window_seconds=3600) is True
        assert check_rate_limit("act", "id", max_requests=2, window_seconds=3600) is True

        clock.advance(61)  # crosses the sweep interval, not the 3600s window
        assert check_rate_limit("act", "id", max_requests=2, window_seconds=3600) is False

    def test_sweep_respects_each_keys_own_window(self, clock):
        """Call sites pass different windows (60s and 3600s); a sweep must not
        evict a long-window key just because a short-window one expired."""
        check_rate_limit("api_token_provision", "1.2.3.4", max_requests=60, window_seconds=60)
        check_rate_limit("login", "user@example.com", max_requests=10, window_seconds=3600)

        clock.advance(61)
        check_rate_limit("login", "other@example.com", max_requests=10, window_seconds=3600)

        assert "api_token_provision:1.2.3.4" not in rate_limit._rates
        assert "login:user@example.com" in rate_limit._rates

    def test_flood_of_identifiers_is_reclaimed_once_windows_elapse(self, clock):
        """/login keys on the submitted email, so the key space is caller-chosen.
        Growth must be bounded by the identifiers seen within one window."""
        for i in range(1000):
            check_rate_limit("login", f"flood{i}@example.com", max_requests=10, window_seconds=60)
        assert len(rate_limit._rates) == 1000

        clock.advance(61)
        check_rate_limit("login", "next@example.com", max_requests=10, window_seconds=60)

        assert len(rate_limit._rates) == 1, (
            f"expected the flood to be reclaimed once its windows elapsed, {len(rate_limit._rates)} keys remain"
        )

    def test_evict_expired_removes_only_expired_entries(self, clock):
        """The helper must never drop a live entry.

        A size cap has to evict live entries, and evicting a live entry hands
        that identifier a fresh allowance — worst for a currently throttled key,
        since a denied request appends no timestamp and so looks stale by any
        recency measure. This pins the helper to expiry-only eviction.
        """
        now = clock.time()
        store = {
            "live-just-hit": (3600.0, [now]),
            "live-near-edge": (3600.0, [now - 3599]),
            "expired": (60.0, [now - 61]),
            "expired-empty": (60.0, []),
        }

        rate_limit._evict_expired(store, now)

        assert set(store) == {"live-just-hit", "live-near-edge"}

    def test_unrelated_traffic_does_not_reset_a_throttled_allowance(self, clock):
        for _ in range(3):
            assert check_rate_limit("login", "victim@example.com", max_requests=3, window_seconds=3600) is True
        assert check_rate_limit("login", "victim@example.com", max_requests=3, window_seconds=3600) is False

        # Heavy unrelated traffic with newer timestamps, across several sweeps.
        for round_ in range(3):
            clock.advance(61)
            for i in range(200):
                check_rate_limit("login", f"other{round_}-{i}@example.com", max_requests=3, window_seconds=3600)

        assert check_rate_limit("login", "victim@example.com", max_requests=3, window_seconds=3600) is False, (
            "unrelated traffic must not hand a still-throttled identifier a fresh allowance"
        )


# ---------------------------------------------------------------------------
# D3: InMemoryRateLimiter sliding window
# ---------------------------------------------------------------------------


class TestInMemoryRateLimiterWindow:
    @pytest.mark.anyio
    async def test_allows_up_to_the_limit_then_denies(self, clock):
        limiter = InMemoryRateLimiter(max_requests=3, window_seconds=60)
        for _ in range(3):
            assert await limiter.is_rate_limited("ip") is False
        assert await limiter.is_rate_limited("ip") is True

    @pytest.mark.anyio
    async def test_keys_are_counted_independently(self, clock):
        limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60)
        assert await limiter.is_rate_limited("ip-a") is False
        assert await limiter.is_rate_limited("ip-a") is True
        assert await limiter.is_rate_limited("ip-b") is False

    @pytest.mark.anyio
    async def test_window_boundary_does_not_grant_a_second_allowance(self, clock):
        """The fixed-window implementation reset the whole counter the moment the
        window elapsed, so hits made late in a window were forgiven early.

        Spread the hits so the two implementations diverge: one hit at t=0 and
        four at t=59. At t=61 only the t=0 hit has aged out, so a sliding window
        may allow exactly one more request. The fixed window instead reset the
        counter wholesale and allowed a further five.
        """
        limiter = InMemoryRateLimiter(max_requests=5, window_seconds=60)

        assert await limiter.is_rate_limited("ip") is False  # t=0
        clock.advance(59)
        for _ in range(4):
            assert await limiter.is_rate_limited("ip") is False  # t=59, five hits total
        assert await limiter.is_rate_limited("ip") is True

        clock.advance(2)  # t=61 — where the old fixed window reset

        allowed = 0
        for _ in range(5):
            if not await limiter.is_rate_limited("ip"):
                allowed += 1

        assert allowed == 1, (
            f"expected 1 further request (only the t=0 hit aged out), got {allowed} — window is not sliding"
        )

    @pytest.mark.anyio
    async def test_allowance_returns_once_hits_slide_out(self, clock):
        limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60)
        assert await limiter.is_rate_limited("ip") is False
        assert await limiter.is_rate_limited("ip") is True
        clock.advance(61)
        assert await limiter.is_rate_limited("ip") is False

    @pytest.mark.anyio
    async def test_denial_does_not_extend_the_window(self, clock):
        limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60)
        assert await limiter.is_rate_limited("ip") is False
        for _ in range(5):
            clock.advance(1)
            assert await limiter.is_rate_limited("ip") is True
        clock.advance(56)
        assert await limiter.is_rate_limited("ip") is False


# ---------------------------------------------------------------------------
# D2: InMemoryRateLimiter memory growth / cleanup
# ---------------------------------------------------------------------------


class TestInMemoryRateLimiterMemory:
    @pytest.mark.anyio
    async def test_store_is_swept_without_an_external_scheduler(self, clock):
        """cleanup() had no callers anywhere, so _store only ever grew.
        is_rate_limited must reclaim expired keys on its own."""
        limiter = InMemoryRateLimiter(max_requests=10, window_seconds=60)
        for i in range(50):
            await limiter.is_rate_limited(f"ip{i}")
        assert len(limiter._store) == 50

        clock.advance(61)
        await limiter.is_rate_limited("trigger")

        assert len(limiter._store) == 1, f"expected only the triggering key to remain, got {len(limiter._store)}"

    @pytest.mark.anyio
    async def test_store_is_reclaimed_once_windows_elapse(self, clock):
        limiter = InMemoryRateLimiter(max_requests=10, window_seconds=60)
        for i in range(1000):
            await limiter.is_rate_limited(f"ip{i}")
        assert len(limiter._store) == 1000

        clock.advance(61)
        await limiter.is_rate_limited("next")

        assert len(limiter._store) == 1, f"expected the flood to be reclaimed, {len(limiter._store)} keys remain"

    @pytest.mark.anyio
    async def test_unrelated_traffic_does_not_reset_a_throttled_key(self, clock):
        limiter = InMemoryRateLimiter(max_requests=2, window_seconds=3600)
        assert await limiter.is_rate_limited("victim") is False
        assert await limiter.is_rate_limited("victim") is False
        assert await limiter.is_rate_limited("victim") is True

        for round_ in range(3):
            clock.advance(61)
            for i in range(200):
                await limiter.is_rate_limited(f"other{round_}-{i}")

        assert await limiter.is_rate_limited("victim") is True, (
            "unrelated traffic must not hand a still-throttled key a fresh allowance"
        )

    @pytest.mark.anyio
    async def test_cleanup_drops_expired_keys_and_keeps_live_ones(self, clock):
        limiter = InMemoryRateLimiter(max_requests=10, window_seconds=60)
        await limiter.is_rate_limited("expiring")
        clock.advance(61)
        await limiter.is_rate_limited("live")

        await limiter.cleanup()

        assert "expiring" not in limiter._store
        assert "live" in limiter._store

    @pytest.mark.anyio
    async def test_cleanup_does_not_reset_a_live_allowance(self, clock):
        limiter = InMemoryRateLimiter(max_requests=1, window_seconds=3600)
        assert await limiter.is_rate_limited("ip") is False

        await limiter.cleanup()

        assert await limiter.is_rate_limited("ip") is True
