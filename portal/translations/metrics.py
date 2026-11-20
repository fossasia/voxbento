from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

WINDOW_SECONDS = 300.0
FAILING_MIN_FAILURES = 2

TRANSLATED = "translated"
FAILED = "failed"
STALE = "stale"

IDLE = "idle"
HEALTHY = "healthy"
LAGGING = "lagging"
FAILING = "failing"


@dataclass(frozen=True, slots=True)
class Outcome:
    at: float
    provider: str
    model: str
    room_id: int
    kind: str
    start_lag: float | None
    latency: float | None


@dataclass(frozen=True)
class ModelStats:
    provider: str
    model: str
    translated: int
    failed: int
    stale: int
    start_lag_p50: float | None
    start_lag_p95: float | None
    latency_p50: float | None
    latency_p95: float | None


@dataclass(frozen=True)
class RoomHealth:
    state: str
    translated: int
    failed: int
    stale: int
    start_lag_p95: float | None


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def room_state(translated: int, failed: int, stale: int, start_lag_p95: float | None, lag_budget: float | None) -> str:
    total = translated + failed + stale
    if total == 0:
        return IDLE
    if failed >= FAILING_MIN_FAILURES and failed * 2 >= total:
        return FAILING
    if stale or (lag_budget and start_lag_p95 is not None and start_lag_p95 >= lag_budget / 2):
        return LAGGING
    return HEALTHY


class TranslationMetrics:
    def __init__(
        self,
        *,
        window: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window = window
        self._clock = clock
        self._outcomes: deque[Outcome] = deque()

    def record(
        self,
        provider: str,
        model: str,
        room_id: int,
        kind: str,
        *,
        start_lag: float | None = None,
        latency: float | None = None,
    ) -> None:
        self._outcomes.append(Outcome(self._clock(), provider, model, room_id, kind, start_lag, latency))
        self._forget_old()

    def recent(self) -> list[Outcome]:
        self._forget_old()
        return list(self._outcomes)

    def _forget_old(self) -> None:
        cutoff = self._clock() - self.window
        while self._outcomes and self._outcomes[0].at < cutoff:
            self._outcomes.popleft()

    def by_model(self) -> list[ModelStats]:
        groups: dict[tuple[str, str], list[Outcome]] = {}
        for outcome in self.recent():
            groups.setdefault((outcome.provider, outcome.model), []).append(outcome)
        return [_model_stats(provider, model, outcomes) for (provider, model), outcomes in sorted(groups.items())]

    def room(self, room_id: int, lag_budget: float | None) -> RoomHealth:
        outcomes = [outcome for outcome in self.recent() if outcome.room_id == room_id]
        counts = _counts(outcomes)
        lag_p95 = percentile(_values(outcomes, "start_lag"), 0.95)
        return RoomHealth(room_state(*counts, lag_p95, lag_budget), *counts, lag_p95)

    def clear(self) -> None:
        self._outcomes.clear()


def _counts(outcomes: list[Outcome]) -> tuple[int, int, int]:
    kinds = [outcome.kind for outcome in outcomes]
    return kinds.count(TRANSLATED), kinds.count(FAILED), kinds.count(STALE)


def _values(outcomes: list[Outcome], field: str) -> list[float]:
    return [value for outcome in outcomes if (value := getattr(outcome, field)) is not None]


def _model_stats(provider: str, model: str, outcomes: list[Outcome]) -> ModelStats:
    lags = _values(outcomes, "start_lag")
    latencies = _values(outcomes, "latency")
    return ModelStats(
        provider,
        model,
        *_counts(outcomes),
        start_lag_p50=percentile(lags, 0.5),
        start_lag_p95=percentile(lags, 0.95),
        latency_p50=percentile(latencies, 0.5),
        latency_p95=percentile(latencies, 0.95),
    )


translation_metrics = TranslationMetrics()
