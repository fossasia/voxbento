"""The concurrent-transcription cap must come from settings, not a constant.

Regression test for the hardcoded ``MAX_TOTAL_WORKERS`` limit that used to live
in ``portal/transcription/worker.py``. The cap is now
``settings.max_transcription_workers`` so operators can raise it per deployment
without editing source, and this test fails if the value stops being honoured.
"""

from __future__ import annotations

import os

os.environ.setdefault("BOOTH_ACCESS_TOKEN", "")
os.environ.setdefault("API_KEY_ENCRYPTION_KEY", "test-key-encryption-key-for-transcription")

import pytest

from portal.config import settings
from portal.transcription import worker

pytestmark = pytest.mark.anyio


class _StubSession:
    """Stand-in for ``TranscriptionWorkerSession``; only its presence matters."""

    def stop(self) -> None: ...

    async def wait_until_stopped(self) -> None: ...


@pytest.fixture
def worker_registry():
    """Isolate the module-global worker registry for the duration of a test."""
    saved = dict(worker.active_workers)
    worker.active_workers.clear()
    yield worker.active_workers
    worker.active_workers.clear()
    worker.active_workers.update(saved)


def _start(booth_id: str):
    return worker.start_transcription_worker(
        event_slug="event-alpha",
        language_code="en",
        booth_id=booth_id,
        broadcast_callback=None,
        provider="openai",
        model_size="base",
        config=None,
    )


async def test_capacity_limit_uses_configured_value(monkeypatch, worker_registry):
    # A configured cap of 2 must reject the third worker. With the old hardcoded
    # limit of 10 this call would not raise at all.
    monkeypatch.setattr(settings, "max_transcription_workers", 2)
    worker_registry["booth-a"] = _StubSession()
    worker_registry["booth-b"] = _StubSession()

    with pytest.raises(ValueError, match=r"System at maximum capacity \(2 concurrent"):
        await _start("booth-c")


async def test_capacity_error_reports_the_configured_value(monkeypatch, worker_registry):
    monkeypatch.setattr(settings, "max_transcription_workers", 3)
    for index in range(3):
        worker_registry[f"booth-{index}"] = _StubSession()

    with pytest.raises(ValueError) as excinfo:
        await _start("booth-overflow")

    assert "(3 concurrent transcription booths)" in str(excinfo.value)


def test_worker_module_no_longer_hardcodes_the_limit():
    assert not hasattr(worker, "MAX_TOTAL_WORKERS")
