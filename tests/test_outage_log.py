"""Reconcile-loop failures are logged once per outage, not once per tick (#964)."""

from __future__ import annotations

import logging

import pytest

from portal import outage_log
from portal.outage_log import OutageLog

logger = logging.getLogger("tests.outage_log")


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(outage_log, "monotonic", lambda: now["t"])
    return now


def messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == logger.name]


def test_repeated_failures_log_once_then_recovery(caplog, clock):
    outage = OutageLog(logger, "svc list_unavailable")
    with caplog.at_level(logging.WARNING):
        for _ in range(30):
            outage.failed()
            clock["t"] += 2
        outage.recovered()
        outage.recovered()
    assert messages(caplog) == [
        "svc list_unavailable",
        "svc list_unavailable recovered failures=30 duration_s=60",
    ]


def test_long_outage_sends_periodic_reminders(caplog, clock):
    outage = OutageLog(logger, "svc list_unavailable", remind_secs=300)
    with caplog.at_level(logging.WARNING):
        for _ in range(301):  # 600 s of 2 s ticks
            outage.failed()
            clock["t"] += 2
    assert messages(caplog) == [
        "svc list_unavailable",
        "svc list_unavailable ongoing failures=151 duration_s=300",
        "svc list_unavailable ongoing failures=301 duration_s=600",
    ]


def test_new_outage_after_recovery_is_logged_again(caplog, clock):
    outage = OutageLog(logger, "svc list_unavailable")
    with caplog.at_level(logging.WARNING):
        outage.recovered()  # no outage yet: nothing to report
        outage.failed()
        outage.recovered()
        clock["t"] += 10
        outage.failed()
    assert messages(caplog) == [
        "svc list_unavailable",
        "svc list_unavailable recovered failures=1 duration_s=0",
        "svc list_unavailable",
    ]
