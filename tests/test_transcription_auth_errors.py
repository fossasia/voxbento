"""Tests for reporting a rejected provider API key.

A worker that cannot authenticate must stop instead of retrying forever, and
the ``booth.transcription.stopped`` webhook must say why it stopped.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from portal.transcription.errors import TranscriptionAuthError
from portal.transcription.process import FfmpegProcess
from portal.transcription.providers.base import ProviderConfig, TranscriptionProvider
from portal.transcription.worker import TranscriptionWorkerSession, active_workers


@pytest.fixture(autouse=True)
async def _clean_registry():
    active_workers.clear()
    yield
    for session in list(active_workers.values()):
        session.stop()
        await session.wait_until_stopped()
    active_workers.clear()


@pytest.fixture(autouse=True)
def mock_ffmpeg_subprocess():
    """Stand in for ffmpeg, which CI does not have."""
    original = FfmpegProcess.__aenter__

    async def dummy_aenter(self):
        self.process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            "sleep 1000",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.stderr_task = asyncio.create_task(self._log_stderr())
        return self.process

    try:
        FfmpegProcess.__aenter__ = dummy_aenter
        yield
    finally:
        FfmpegProcess.__aenter__ = original


def _session(provider):
    with patch("portal.transcription.worker.PROVIDERS", {"local": provider}):
        return TranscriptionWorkerSession(
            event_slug="pycon2026",
            language_code="fr",
            booth_id="pycon2026-1-fr",
            broadcast_callback=AsyncMock(),
            provider_name="local",
            model_size="tiny",
            config=ProviderConfig(api_key="k"),
            room_id=1,
        )


async def _run_and_collect(provider):
    """Run one worker to completion and return the webhooks it enqueued."""
    calls = []

    async def fake_enqueue(event_type, payload):
        calls.append((event_type, payload))

    session = _session(provider)
    with patch("portal.webhooks.worker.enqueue_webhook", side_effect=fake_enqueue):
        session.start()
        await asyncio.wait_for(session.task, timeout=10)
    return session, calls


@pytest.mark.anyio
async def test_rejected_key_stops_the_worker_and_reports_the_reason():
    provider = MagicMock()
    provider.run_stream = AsyncMock(
        side_effect=TranscriptionAuthError("openai", "OpenAI API key was rejected (401 Unauthorized).", 401)
    )

    session, calls = await _run_and_collect(provider)

    stopped = [payload for event, payload in calls if event == "booth.transcription.stopped"]
    assert stopped, "the worker must announce that it stopped"
    assert stopped[-1]["error_code"] == "auth_failed"
    assert stopped[-1]["error_detail"] == "OpenAI API key was rejected (401 Unauthorized)."
    assert stopped[-1]["booth_id"] == "pycon2026-1-fr"
    # A rejected key cannot be retried away.
    assert provider.run_stream.await_count == 1


@pytest.mark.anyio
async def test_a_normal_stop_carries_no_error_fields():
    provider = MagicMock()

    async def run_until_cancelled(*args, **kwargs):
        await asyncio.Event().wait()

    provider.run_stream = AsyncMock(side_effect=run_until_cancelled)

    calls = []

    async def fake_enqueue(event_type, payload):
        calls.append((event_type, payload))

    session = _session(provider)
    with patch("portal.webhooks.worker.enqueue_webhook", side_effect=fake_enqueue):
        session.start()
        await asyncio.sleep(0.3)
        session.stop()
        await session.wait_until_stopped()

    stopped = [payload for event, payload in calls if event == "booth.transcription.stopped"]
    assert stopped
    assert "error_code" not in stopped[-1]
    assert "error_detail" not in stopped[-1]


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403])
async def test_openai_raises_on_a_rejected_key(status):
    from portal.transcription.providers.openai import OpenAIProvider

    response = MagicMock(status_code=status, reason_phrase="Unauthorized")
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch("portal.transcription.providers.openai.get_http_client", return_value=client):
        with pytest.raises(TranscriptionAuthError) as exc:
            await OpenAIProvider().process_chunk(b"audio", "en", "whisper-1", ProviderConfig(api_key="bad"))

    assert exc.value.error_code == "auth_failed"
    assert str(status) in exc.value.detail


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403])
async def test_elevenlabs_raises_on_a_rejected_key(status):
    from portal.transcription.providers.elevenlabs import ElevenLabsProvider

    response = MagicMock(status_code=status, reason_phrase="Unauthorized")
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with patch("portal.transcription.providers.elevenlabs.get_http_client", return_value=client):
        with pytest.raises(TranscriptionAuthError) as exc:
            await ElevenLabsProvider().process_chunk(b"audio", "en", "scribe_v1", ProviderConfig(api_key="bad"))

    assert exc.value.error_code == "auth_failed"
    assert str(status) in exc.value.detail


@pytest.mark.anyio
async def test_reason_travels_from_process_chunk_through_the_real_run_stream():
    """The provider path the worker actually uses, not a stubbed run_stream."""
    class RejectingProvider(TranscriptionProvider):
        def __init__(self):
            self.chunks = 0

        async def process_chunk(self, chunk, language_code, model_variant, config, booth_state=None):
            self.chunks += 1
            raise TranscriptionAuthError("openai", "OpenAI API key was rejected (401 Unauthorized).", 401)

    provider = RejectingProvider()

    async def feed(self):
        self.process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            "head -c 200000 /dev/zero; sleep 1000",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self.stderr_task = asyncio.create_task(self._log_stderr())
        return self.process

    original = FfmpegProcess.__aenter__
    FfmpegProcess.__aenter__ = feed
    try:
        session, calls = await _run_and_collect(provider)
    finally:
        FfmpegProcess.__aenter__ = original

    stopped = [payload for event, payload in calls if event == "booth.transcription.stopped"]
    assert stopped[-1]["error_code"] == "auth_failed"
    assert stopped[-1]["error_detail"] == "OpenAI API key was rejected (401 Unauthorized)."
    # One rejected chunk is enough; it must not grind through the retry budget.
    assert provider.chunks == 1


@pytest.mark.anyio
async def test_reason_survives_when_audio_ends_before_provider_response():
    eof_read = asyncio.Event()

    class FiniteStdout:
        def __init__(self):
            self.read_count = 0

        async def readexactly(self, size):
            self.read_count += 1
            if self.read_count == 1:
                return bytes(size)
            eof_read.set()
            raise asyncio.IncompleteReadError(b"", size)

    class FiniteProcess:
        returncode = None
        stdout = FiniteStdout()

    class DelayedRejectingProvider(TranscriptionProvider):
        def __init__(self):
            self.chunks = 0

        async def process_chunk(self, chunk, language_code, model_variant, config, booth_state=None):
            self.chunks += 1
            await eof_read.wait()
            await asyncio.sleep(0)
            raise TranscriptionAuthError("openai", "OpenAI API key was rejected after EOF.", 401)

    async def enter_finite_stream(self):
        self.process = FiniteProcess()
        return self.process

    async def exit_finite_stream(self, exc_type, exc_value, traceback):
        return None

    provider = DelayedRejectingProvider()
    with (
        patch.object(FfmpegProcess, "__aenter__", enter_finite_stream),
        patch.object(FfmpegProcess, "__aexit__", exit_finite_stream),
    ):
        session, calls = await _run_and_collect(provider)

    stopped = [payload for event, payload in calls if event == "booth.transcription.stopped"]
    assert stopped[-1]["error_code"] == "auth_failed"
    assert stopped[-1]["error_detail"] == "OpenAI API key was rejected after EOF."
    assert provider.chunks == 1
