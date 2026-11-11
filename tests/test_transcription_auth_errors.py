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
    assert "rejected" in exc.value.detail.lower()


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
    assert "rejected" in exc.value.detail.lower()


@pytest.mark.anyio
@pytest.mark.parametrize("provider_name", ["openai", "elevenlabs"])
@pytest.mark.parametrize("status", [400, 404, 500, 502])
async def test_a_non_auth_http_error_is_surfaced_not_dropped_as_an_empty_chunk(provider_name, status):
    """Only 401/403 mean a rejected key. Every other non-2xx must still raise,
    as it did before the auth handling, so the retry policy sees it instead of
    the chunk silently becoming an empty transcript.
    """
    import httpx
    from tenacity import RetryError, wait_none

    if provider_name == "openai":
        from portal.transcription.providers.openai import OpenAIProvider as Provider

        model = "whisper-1"
    else:
        from portal.transcription.providers.elevenlabs import ElevenLabsProvider as Provider

        model = "scribe_v1"
    module = f"portal.transcription.providers.{provider_name}"

    response = httpx.Response(status, request=httpx.Request("POST", "https://provider.test/transcribe"))
    client = MagicMock()
    client.post = AsyncMock(return_value=response)

    with (
        patch(f"{module}.get_http_client", return_value=client),
        patch(f"{module}.wait_exponential", return_value=wait_none()),
    ):
        with pytest.raises((httpx.HTTPStatusError, RetryError)):
            await Provider().process_chunk(b"audio", "en", model, ProviderConfig(api_key="k"))


@pytest.mark.anyio
async def test_reason_travels_from_process_chunk_through_the_real_run_stream():
    """The provider path the worker actually uses, not a stubbed run_stream."""
    class RejectingProvider(TranscriptionProvider):
        def __init__(self):
            self.chunks = 0

        async def process_chunk(self, chunk, language_code, model_variant, config, booth_state=None):
            self.chunks += 1
            raise TranscriptionAuthError("openai", "OpenAI API key was rejected.", 401)

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
    assert stopped[-1]["error_detail"] == "OpenAI API key was rejected."
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


@pytest.mark.anyio
async def test_overload_drain_preserves_audio_eof():
    real_sleep = asyncio.sleep

    class FiniteStdout:
        def __init__(self):
            self.read_count = 0

        async def readexactly(self, size):
            self.read_count += 1
            if self.read_count <= 2:
                return bytes(size)
            raise asyncio.IncompleteReadError(b"", size)

    class FiniteProcess:
        returncode = None
        stdout = FiniteStdout()

    class OverloadedProvider(TranscriptionProvider):
        async def process_chunk(self, chunk, language_code, model_variant, config, booth_state=None):
            booth_state.consecutive_drops = 4
            await real_sleep(0)
            raise RuntimeError("transient provider error")

    provider = OverloadedProvider()
    with patch("portal.transcription.providers.base.asyncio.sleep", new=AsyncMock()):
        await asyncio.wait_for(
            provider.run_stream(
                FiniteProcess(),
                "en",
                "model",
                ProviderConfig(api_key="k"),
                AsyncMock(),
                "pycon2026-1-en",
            ),
            timeout=1,
        )


class _FramesExhausted(Exception):
    """Raised when the fake websocket runs out of scripted frames."""


class _FakeElevenLabsWS:
    """A websocket that yields the frames a rejected ElevenLabs key produces."""

    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def recv(self):
        if not self._frames:
            raise _FramesExhausted("no frames left")
        return self._frames.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, payload):
        self.sent.append(payload)
        # Yield, so the sender loop cannot starve the receiver in the test loop.
        await asyncio.sleep(0)


async def _run_elevenlabs_with_frames(frames):
    """Drive the real run_stream against a websocket serving `frames`."""
    import json

    from portal.transcription.providers.elevenlabs import ElevenLabsProvider

    process = MagicMock()
    process.returncode = None
    process.stdout = MagicMock()
    process.stdout.readexactly = AsyncMock(return_value=b"\x00" * 4096)

    # run_stream builds a real CaptionAggregator around this callback and awaits it,
    # so it has to be awaitable: a MagicMock would raise TypeError inside the receiver
    # and send the run around the retry loop instead of exercising the frame path.
    broadcast_callback = AsyncMock()

    ws = _FakeElevenLabsWS([json.dumps(frame) for frame in frames])

    # Bounded so a provider that never surfaces the auth error fails the test
    # quickly instead of hanging CI in the reconnect loop.
    with patch("websockets.connect", return_value=ws):
        async with asyncio.timeout(10):
            await ElevenLabsProvider().run_stream(
                process,
                "en",
                "scribe_v1",
                ProviderConfig(api_key="bad-key"),
                broadcast_callback,
                "booth-1",
            )


@pytest.mark.anyio
async def test_elevenlabs_auth_error_as_the_first_frame_is_reported():
    with pytest.raises(TranscriptionAuthError) as excinfo:
        await _run_elevenlabs_with_frames(
            [{"message_type": "auth_error", "error": "Invalid API key"}]
        )

    assert excinfo.value.error_code == "auth_failed"
    assert excinfo.value.provider == "elevenlabs"


@pytest.mark.anyio
async def test_elevenlabs_auth_error_after_the_session_starts_is_reported():
    with pytest.raises(TranscriptionAuthError) as excinfo:
        await _run_elevenlabs_with_frames(
            [
                {"message_type": "session_started"},
                {"message_type": "partial_transcript", "text": "hello"},
                {"message_type": "auth_error", "error": "API key revoked"},
            ]
        )

    assert excinfo.value.error_code == "auth_failed"
    assert excinfo.value.provider == "elevenlabs"


@pytest.mark.anyio
@pytest.mark.parametrize("message_type", ["unauthorized", "authentication_error", "auth_error"])
async def test_elevenlabs_reports_every_mid_stream_auth_frame_type(message_type):
    """"unauthorized" has no "error" substring, so the old guard skipped it."""
    with pytest.raises(TranscriptionAuthError) as excinfo:
        await _run_elevenlabs_with_frames(
            [
                {"message_type": "session_started"},
                {"message_type": "partial_transcript", "text": "hello"},
                {"message_type": message_type, "error": "API key revoked"},
            ]
        )

    assert excinfo.value.error_code == "auth_failed"
    assert excinfo.value.provider == "elevenlabs"


@pytest.mark.anyio
async def test_elevenlabs_auth_frame_guard_matches_the_classifier():
    """The guard and the classifier must not drift apart again."""
    from portal.transcription.providers.elevenlabs import ElevenLabsProvider

    for message_type in ElevenLabsProvider._AUTH_MESSAGE_TYPES:
        assert ElevenLabsProvider._is_auth_frame_type(message_type)
        assert ElevenLabsProvider._auth_error_from_frame(
            {"message_type": message_type, "error": "nope"}
        )
    assert not ElevenLabsProvider._is_auth_frame_type("partial_transcript")


def _ws_invalid_status(status_code: int):
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    return InvalidStatus(Response(status_code, "", Headers()))


@pytest.mark.anyio
async def test_deepgram_handshake_401_is_reported():
    from portal.transcription.providers.deepgram import DeepgramProvider

    process = MagicMock()
    process.returncode = None

    for status in (401, 403):
        def raise_handshake(*args, _status=status, **kwargs):
            raise _ws_invalid_status(_status)

        with patch("websockets.connect", side_effect=raise_handshake):
            with pytest.raises(TranscriptionAuthError) as excinfo:
                await DeepgramProvider().run_stream(
                    process, "en", "nova-2", ProviderConfig(api_key="bad"), AsyncMock(), "booth-1"
                )
        assert excinfo.value.error_code == "auth_failed"
        assert excinfo.value.provider == "deepgram"


@pytest.mark.anyio
async def test_openai_realtime_handshake_401_is_reported():
    """A rejected key on the realtime path used to be retried, then abandoned."""
    from portal.transcription.providers.openai import OpenAIProvider

    process = MagicMock()
    process.returncode = None

    for status in (401, 403):
        def raise_handshake(*args, _status=status, **kwargs):
            raise _ws_invalid_status(_status)

        with patch("websockets.connect", side_effect=raise_handshake):
            with pytest.raises(TranscriptionAuthError) as excinfo:
                await OpenAIProvider().run_stream(
                    process, "en", "gpt-4o-realtime-preview", ProviderConfig(api_key="bad"), AsyncMock(), "booth-1"
                )
        assert excinfo.value.error_code == "auth_failed"
        assert excinfo.value.provider == "openai"


@pytest.mark.anyio
async def test_openai_realtime_non_auth_failure_still_retries():
    """Only auth failures short-circuit; a network blip keeps the retry policy."""
    from portal.transcription.providers.openai import OpenAIProvider

    process = MagicMock()
    process.returncode = None
    attempts = 0

    def raise_conn(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("network down")

    with patch("websockets.connect", side_effect=raise_conn):
        with patch("portal.transcription.providers.openai.asyncio.sleep", new=AsyncMock()):
            await asyncio.wait_for(
                OpenAIProvider().run_stream(
                    process, "en", "gpt-4o-realtime-preview", ProviderConfig(api_key="good"), AsyncMock(), "booth-1"
                ),
                timeout=5,
            )

    assert attempts > 1


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"error": {"code": "invalid_api_key", "message": "Incorrect API key provided"}}, True),
        ({"error": {"code": "x", "message": "Unauthorized"}}, True),
        ({"error": {"code": "authentication_error", "message": "nope"}}, True),
        ({"error": {"code": "server_error", "message": "upstream exploded"}}, False),
        ({"error": {"code": "rate_limit_exceeded", "message": "slow down"}}, False),
        ({"error": "not-a-dict"}, False),
        ({}, False),
    ],
)
@pytest.mark.anyio
async def test_openai_realtime_error_events_are_classified(event, expected):
    from portal.transcription.providers.openai import OpenAIProvider

    assert bool(OpenAIProvider._auth_error_from_event(event)) is expected


@pytest.mark.anyio
async def test_deepgram_non_auth_failure_follows_retry_policy():
    from portal.transcription.providers.deepgram import DeepgramProvider

    process = MagicMock()
    process.returncode = None
    attempts = 0

    def raise_conn(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("network down")

    with patch("websockets.connect", side_effect=raise_conn):
        with patch("portal.transcription.providers.base.asyncio.sleep", new=AsyncMock()):
            await asyncio.wait_for(
                DeepgramProvider().run_stream(
                    process, "en", "nova-2", ProviderConfig(api_key="good"), AsyncMock(), "booth-1"
                ),
                timeout=2,
            )

    assert attempts == 5


@pytest.mark.anyio
async def test_drain_queue_to_eof_preserves_sentinel_behind_audio():
    from portal.transcription.providers.base import _drain_queue_to_eof

    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait(b"audio-1")
    queue.put_nowait(b"audio-2")
    queue.put_nowait(None)
    queue.put_nowait(b"audio-3")
    assert _drain_queue_to_eof(queue) is True
    assert queue.empty()

    other: asyncio.Queue = asyncio.Queue()
    other.put_nowait(b"audio")
    assert _drain_queue_to_eof(other) is False


@pytest.mark.anyio
async def test_auth_error_detail_is_bounded():
    from portal.transcription.errors import MAX_DETAIL_CHARS

    raw = "boom\n" + ("x" * 5000)
    err = TranscriptionAuthError("elevenlabs", raw)
    assert "\n" not in err.detail
    assert len(err.detail) <= MAX_DETAIL_CHARS + 1
    assert str(err) == err.detail
