"""Log a failure that repeats on every reconcile tick once per outage (#964).

Reconcile loops retry every few seconds, so logging each failed tick buries every other
line while a dependency stays down. ``OutageLog`` logs the first failure under the
original event name, a reminder while the outage lasts, and the recovery.
"""

from __future__ import annotations

import logging
from time import monotonic

REMIND_SECS = 300.0


class OutageLog:
    def __init__(self, logger: logging.Logger, event: str, remind_secs: float = REMIND_SECS) -> None:
        self.logger = logger
        self.event = event
        self.remind_secs = remind_secs
        self.failures = 0
        self.started = 0.0
        self.reminded = 0.0

    def failed(self) -> None:
        now = monotonic()
        self.failures += 1
        if self.failures == 1:
            self.started = self.reminded = now
            self.logger.warning("%s", self.event)
        elif now - self.reminded >= self.remind_secs:
            self.reminded = now
            self.logger.warning("%s ongoing failures=%d duration_s=%d", self.event, self.failures, now - self.started)

    def recovered(self) -> None:
        if not self.failures:
            return
        # WARNING, like the line that opened the outage: the portal logs nothing below it.
        self.logger.warning(
            "%s recovered failures=%d duration_s=%d", self.event, self.failures, monotonic() - self.started
        )
        self.failures = 0
