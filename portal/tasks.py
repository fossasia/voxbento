from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Strong references to running background tasks. The event loop only keeps weak
# references, so an untracked task can be garbage collected before it finishes.
_tasks: set[asyncio.Task] = set()


def track_task(task: asyncio.Task) -> None:
    """Keep a strong reference to a background task until it completes, and log
    any exception it raises."""
    _tasks.add(task)

    def _on_done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error("Background task %s failed", t.get_name(), exc_info=t.exception())

    task.add_done_callback(_on_done)
