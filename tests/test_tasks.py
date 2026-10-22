"""Tests for portal.tasks.track_task.

Covers:
- A tracked task is held by a strong reference until it finishes, then released
- An exception raised by a tracked task is logged with its traceback
- A cancelled task is released without logging an error
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from portal import tasks


@pytest.fixture(autouse=True)
def clear_tasks():
    """Isolate the module-level task registry between tests."""
    tasks._tasks.clear()
    yield
    for task in list(tasks._tasks):
        task.cancel()
    tasks._tasks.clear()


def task_logs(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "portal.tasks"]


@pytest.mark.anyio
async def test_task_is_held_until_it_finishes():
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    tasks.track_task(task)

    assert task in tasks._tasks

    release.set()
    await asyncio.wait([task])
    await asyncio.sleep(0)  # let the done callback run

    assert task not in tasks._tasks


@pytest.mark.anyio
async def test_task_exception_is_logged(caplog):
    async def fail():
        raise RuntimeError("boom")

    task = asyncio.create_task(fail())
    tasks.track_task(task)

    with caplog.at_level(logging.ERROR, logger="portal.tasks"):
        await asyncio.wait([task])
        await asyncio.sleep(0)

    assert task not in tasks._tasks
    (record,) = task_logs(caplog)
    assert record.levelno == logging.ERROR
    assert record.exc_info[1] is task.exception()


@pytest.mark.anyio
async def test_cancelled_task_is_released_without_logging(caplog):
    task = asyncio.create_task(asyncio.Event().wait())
    tasks.track_task(task)
    task.cancel()

    with caplog.at_level(logging.ERROR, logger="portal.tasks"):
        await asyncio.wait([task])
        await asyncio.sleep(0)

    assert task.cancelled()
    assert task not in tasks._tasks
    assert task_logs(caplog) == []
