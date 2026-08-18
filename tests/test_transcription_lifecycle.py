import asyncio
import os
import signal
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from portal.config import settings
from portal.transcription.process import FfmpegProcess
from portal.transcription.worker import (
    State,
    active_workers,
    start_transcription_worker,
    stop_transcription_worker,
)


@pytest.fixture(autouse=True)
async def clean_registry():
    # Stop all existing tasks instead of just clearing the dict
    for booth_id, session in list(active_workers.items()):
        session.stop()
        await session.wait_until_stopped()
    active_workers.clear()
    yield
    for booth_id, session in list(active_workers.items()):
        session.stop()
        await session.wait_until_stopped()
    active_workers.clear()

@pytest.fixture
def mock_provider():
    provider = MagicMock()
    provider.run_stream = AsyncMock()
    return provider

@pytest.fixture
def mock_providers(mock_provider):
    with patch("portal.transcription.worker.PROVIDERS", {"local": mock_provider}):
        yield {"local": mock_provider}

@pytest.fixture(autouse=True)
def mock_ffmpeg_subprocess():
    """
    Patch FfmpegProcess to use a dummy bash sleep command instead of ffmpeg.
    This prevents FileNotFoundError in CI environments where ffmpeg is not installed,
    while still allowing the real __aexit__ cleanup logic to be tested.
    """
    old_cmd = getattr(FfmpegProcess, "__aenter__")

    async def dummy_aenter(self):
        self.process = await asyncio.create_subprocess_exec(
            "bash", "-c", "sleep 1000",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True
        )
        self.stderr_task = asyncio.create_task(self._log_stderr())
        return self.process

    try:
        FfmpegProcess.__aenter__ = dummy_aenter
        yield
    finally:
        FfmpegProcess.__aenter__ = old_cmd


@pytest.mark.anyio
async def test_transcription_worker_cancellation(mock_providers):
    """
    Test that when a task is cancelled midway, the __aexit__ cleanup is still run.
    """
    booth_id = "test_booth_cancel"

    # We will cancel it while it's in run_stream
    async def mock_run_stream(*args, **kwargs):
        await asyncio.sleep(10)

    mock_providers["local"].run_stream = mock_run_stream

    await start_transcription_worker(
        "evt", "fr", booth_id, None, "local", "base", None
    )

    assert booth_id in active_workers
    worker = active_workers[booth_id]

    # Wait for state to become RUNNING
    for _ in range(10):
        if worker.state == State.RUNNING:
            break
        await asyncio.sleep(0.1)

    assert worker.state == State.RUNNING

    # Stop the worker (cancels the task and drops lock to wait)
    # Stop is an async function, we run it concurrently
    stop_task = asyncio.create_task(stop_transcription_worker(booth_id))

    # Wait for the stop to finish
    await stop_task

    # Verify cleanup
    assert worker.state == State.STOPPED
    assert booth_id not in active_workers


@pytest.mark.anyio
async def test_transcription_worker_retry_race(mock_providers):
    """
    Simulate an immediate ffmpeg crash at the exact moment a shutdown request is issued.
    Verify the explicit state prevents a retry.
    """
    booth_id = "test_booth_retry_race"

    run_stream_count = 0

    async def mock_run_stream(*args, **kwargs):
        nonlocal run_stream_count
        run_stream_count += 1
        raise Exception("Simulated crash")

    mock_providers["local"].run_stream = mock_run_stream

    await start_transcription_worker(
        "evt", "fr", booth_id, None, "local", "base", None
    )

    worker = active_workers[booth_id]

    # Immediately trigger stop while it's attempting to retry
    await asyncio.sleep(0.01)
    await stop_transcription_worker(booth_id)

    assert worker.state == State.STOPPED
    # Should only run once, not retry infinitely
    assert run_stream_count == 1


@pytest.mark.anyio
async def test_transcription_worker_unexpected_exception(mock_providers):
    """
    Force an unexpected exception during the provider stream.
    Verify __aexit__ executes, reaches STOPPED, clears registry, and replacement starts.
    """
    booth_id = "test_booth_exception"

    async def mock_run_stream(*args, **kwargs):
        raise RuntimeError("Unexpected boom")

    mock_providers["local"].run_stream = mock_run_stream

    await start_transcription_worker(
        "evt", "fr", booth_id, None, "local", "base", None
    )

    worker = active_workers[booth_id]

    # Wait for the crash and retry
    await asyncio.sleep(0.1)

    # Instead of letting it retry forever, stop it
    await stop_transcription_worker(booth_id)

    assert worker.state == State.STOPPED
    assert booth_id not in active_workers

    # Replacement can start
    await start_transcription_worker(
        "evt", "fr", booth_id, None, "local", "base", None
    )
    assert booth_id in active_workers
    assert active_workers[booth_id].state == State.STARTING


@pytest.mark.anyio
async def test_serialized_replacement(mock_providers):
    """
    Assert that for a given booth_id, the number of active transcription sessions/process groups
    must never exceed one.
    """
    booth_id = "test_booth_serialize"
    old_worker = None

    async def mock_run_stream(*args, **kwargs):
        # The key assertion: if this is the replacement worker running its stream,
        # the old worker MUST be completely stopped.
        if old_worker is not None and active_workers[booth_id] is not old_worker:
            assert old_worker.state == State.STOPPED
        await asyncio.sleep(0.5)

    mock_providers["local"].run_stream = mock_run_stream

    # Start first
    await start_transcription_worker("evt", "fr", booth_id, None, "local", "base", None)
    old_worker = active_workers[booth_id]

    # Wait for it to be running
    await asyncio.sleep(0.1)

    # Concurrently stop and start
    stop_task = asyncio.create_task(stop_transcription_worker(booth_id))

    # Wait a tiny bit so stop_task begins execution and marks it STOPPING
    await asyncio.sleep(0.01)

    # Now start another replacement concurrently.
    # Because of our strict serialization, this must wait for stop_task to finish
    # tearing down the old worker before it spawns a new one.
    start_task = asyncio.create_task(
        start_transcription_worker("evt", "fr", booth_id, None, "local", "base", None)
    )

    # Wait for both
    await stop_task
    await start_task

    assert old_worker.state == State.STOPPED

    new_worker = active_workers[booth_id]
    assert new_worker is not old_worker
    assert new_worker.state != State.STOPPED

    # Cleanup
    await stop_transcription_worker(booth_id)


@pytest.mark.anyio
async def test_real_subprocess_integration():
    """
    Integration test using a real lightweight subprocess to verify
    start_new_session=True puts the process in its own group
    SIGTERM via os.killpg() kills descendant processes (grandchildren), not only the direct child
    SIGKILL escalation functions properly at the Unix process level
    """
    booth_id = "integration_booth"

    old_cmd = getattr(FfmpegProcess, "__aenter__")

    async def mock_aenter(self):
        # The bash script spawns a grandchild (sleep 1000) and prints its PID to stdout.
        # We read that PID before cleanup so we can explicitly assert it is dead afterward.
        bash_script = "sleep 1000 & echo $!; wait"
        self.process = await asyncio.create_subprocess_exec(
            "bash", "-c", bash_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True
        )
        self.stderr_task = asyncio.create_task(self._log_stderr())
        return self.process

    try:
        FfmpegProcess.__aenter__ = mock_aenter

        proc_manager = FfmpegProcess("dummy", "16000", booth_id)
        process = await proc_manager.__aenter__()

        assert process is not None
        assert process.pid > 0

        # Read the grandchild PID that bash printed to stdout
        grandchild_pid_line = await asyncio.wait_for(process.stdout.readline(), timeout=2.0)
        grandchild_pid = int(grandchild_pid_line.strip())
        assert grandchild_pid > 0, "Bash must have printed the grandchild PID"

        # Confirm grandchild is currently alive before cleanup
        os.kill(grandchild_pid, 0)  # raises ProcessLookupError if not alive

        # Trigger cleanup — os.killpg() must kill the entire process group
        await proc_manager.__aexit__(None, None, None)

        # Assert the direct child (bash) is dead
        assert process.returncode is not None, "bash process must have exited"

        # Assert the grandchild (sleep 1000) is ALSO dead —
        # this is the key invariant: process-group signals reach descendants.
        # We poll for up to 1 second to allow the kernel to reap the process.
        for _ in range(10):
            try:
                os.kill(grandchild_pid, 0)
                await asyncio.sleep(0.1)
            except ProcessLookupError:
                break  # Expected: grandchild was terminated by process group signal
        else:
            pytest.fail(
                f"Grandchild process {grandchild_pid} is still alive after process-group cleanup"
            )

    finally:
        FfmpegProcess.__aenter__ = old_cmd


@pytest.mark.anyio
async def test_escalation_zombie():
    """
    Mock the subprocess to ignore SIGTERM, forcing SIGKILL escalation.
    """
    booth_id = "zombie_booth"

    async def mock_aenter(self):
        # trap SIGTERM so it doesn't die, forcing timeout -> SIGKILL
        bash_script = "trap '' SIGTERM; sleep 1000"
        self.process = await asyncio.create_subprocess_exec(
            "bash", "-c", bash_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True
        )
        self.stderr_task = asyncio.create_task(self._log_stderr())
        return self.process

    # We patch the instance instead of pydantic Settings

    old_cmd = getattr(FfmpegProcess, "__aenter__")
    try:
        FfmpegProcess.__aenter__ = mock_aenter
        proc_manager = FfmpegProcess("dummy", "16000", booth_id)
        proc_manager.termination_timeout = 0.5

        process = await proc_manager.__aenter__()

        # Exiting should hit the 0.5s timeout on SIGTERM, then escalate to SIGKILL
        await proc_manager.__aexit__(None, None, None)

        # The process should be dead because SIGKILL cannot be trapped
        assert process.returncode is not None

    finally:
        FfmpegProcess.__aenter__ = old_cmd
