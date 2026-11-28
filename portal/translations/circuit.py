"""Per-room primary-engine recovery; no credentials or transcript text are retained."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

FAILURE_THRESHOLD = 3
RECOVERY_SECONDS = 30.0


@dataclass
class CircuitAttempt:
    circuit: CircuitBreaker
    generation: int
    probe: bool
    finished: bool = False

    def finish(self, succeeded: bool | None) -> None:
        """Record an outcome once. None abandons a cancelled or unstarted attempt."""
        if self.finished:
            return
        self.finished = True
        self.circuit.complete(self, succeeded)


class CircuitBreaker:
    def __init__(
        self,
        *,
        threshold: int = FAILURE_THRESHOLD,
        recovery: float = RECOVERY_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1 or recovery <= 0:
            raise ValueError("threshold and recovery must be positive")
        self.threshold = threshold
        self.recovery = recovery
        self.clock = clock
        self.failures = 0
        self.opened_until: float | None = None
        self.generation = 0
        self.probing = False
        self.active = 0
        self.last_used = clock()

    def start(self) -> CircuitAttempt | None:
        self.last_used = self.clock()
        probe = self.opened_until is not None
        if probe:
            if self.last_used < self.opened_until or self.probing:
                return None
            self.probing = True
        self.active += 1
        return CircuitAttempt(self, self.generation, probe)

    def complete(self, attempt: CircuitAttempt, succeeded: bool | None) -> None:
        self.active -= 1
        self.last_used = self.clock()
        # A late result from before the circuit opened must not cancel its recovery window.
        if attempt.generation != self.generation:
            return
        if attempt.probe:
            self.probing = False
        if succeeded is None:
            return
        if succeeded:
            self.failures = 0
            if attempt.probe:
                self.opened_until = None
                self.generation += 1
            return
        self.failures += 1
        if attempt.probe or self.failures >= self.threshold:
            self.opened_until = self.clock() + self.recovery
            self.generation += 1
            self.probing = False


CircuitKey = tuple[int, str, str, str, str]
CIRCUITS: dict[CircuitKey, CircuitBreaker] = {}


def circuit_for(room_id: int, provider: str, model: str, fallback_provider: str, fallback_model: str) -> CircuitBreaker:
    key = (room_id, provider, model, fallback_provider, fallback_model)
    now = time.monotonic()
    for old, circuit in list(CIRCUITS.items()):
        if not circuit.active and (
            (old[:3] == key[:3] and old != key)
            or (circuit.opened_until is None and now - circuit.last_used > 2 * circuit.recovery)
        ):
            del CIRCUITS[old]
    if key not in CIRCUITS:
        CIRCUITS[key] = CircuitBreaker()
    return CIRCUITS[key]
