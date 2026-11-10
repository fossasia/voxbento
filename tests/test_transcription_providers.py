from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

import portal.globals as pg
from portal.transcription.providers.base import ProviderConfig, TranscriptionProvider, pcm_to_wav


def _atlas_response(status: int, body: object) -> httpx.Response:
    return httpx.Response(
        status,
        json=body,
        request=httpx.Request("GET", "https://api.atlascloud.ai/api/v1/model/prediction/p"),
    )


def _completed_payload(text: str) -> dict:
    return {
        "code": 200,
        "data": {"id": "p", "status": "completed", "outputs": [text], "stt_result": {"text": text}},
    }


def _atlas_client(submit: httpx.Response, polls: list) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(return_value=submit)
    client.get = AsyncMock(side_effect=polls)
    return client


async def _run_atlas(provider, client: MagicMock, language: str = "en-US") -> str:
    with (
        patch("portal.transcription.providers.atlascloud.get_http_client", return_value=client),
        patch("portal.transcription.providers.atlascloud.asyncio.sleep", new_callable=AsyncMock),
    ):
        return await provider.process_chunk(
            b"\x00" * 3200, language, "bytedance/seed-asr-2.0", ProviderConfig(api_key="fake")
        )


@pytest.mark.anyio
class TestTranscriptionProviders:
    async def test_atlascloud_process_chunk_submits_once_and_polls(self):
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "prediction-1", "status": "processing"}}),
            polls=[_atlas_response(200, _completed_payload("Hello from Atlas"))],
        )

        result = await _run_atlas(AtlasCloudProvider(), mock_client)

        assert result == "Hello from Atlas"
        mock_client.post.assert_awaited_once()
        mock_client.get.assert_awaited_once()
        payload = mock_client.post.await_args.kwargs["json"]
        assert payload["model"] == "bytedance/seed-asr-2.0"
        assert payload["audio_url"].startswith("data:audio/wav;base64,")
        assert payload["format"] == "wav"
        assert payload["language"] == "en-US"

    async def test_atlascloud_reads_the_stt_result_shape_the_api_returns(self):
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        # Shape of a real seed-asr-2.0 prediction: stt_result carries the text, outputs repeats it.
        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing", "outputs": None}}),
            polls=[
                _atlas_response(
                    200,
                    {
                        "code": 200,
                        "data": {
                            "id": "p",
                            "status": "completed",
                            "outputs": ["Hello. This is a test of."],
                            "stt_result": {"text": " Hello. This is a test of. ", "duration": 1.5, "words": []},
                        },
                    },
                )
            ],
        )

        assert await _run_atlas(AtlasCloudProvider(), mock_client) == "Hello. This is a test of."

    async def test_atlascloud_completed_without_speech_returns_empty(self):
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, _completed_payload("")),
            polls=[],
        )

        assert await _run_atlas(AtlasCloudProvider(), mock_client) == ""
        mock_client.get.assert_not_awaited()

    async def test_atlascloud_outputs_only_empty_speech_returns_empty(self):
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "completed", "outputs": [""]}}),
            polls=[],
        )

        assert await _run_atlas(AtlasCloudProvider(), mock_client) == ""
        mock_client.get.assert_not_awaited()

    async def test_atlascloud_exhausted_transient_poll_failures_raise_provider_error(self):
        from portal.transcription.providers.atlascloud import AtlasCloudError, AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing"}}),
            polls=[_atlas_response(503, {"code": 503, "msg": "unavailable"})] * 2,
        )

        with patch("portal.transcription.providers.atlascloud.MAX_POLL_ATTEMPTS", 2):
            with pytest.raises(AtlasCloudError, match="after 2 attempts"):
                await _run_atlas(AtlasCloudProvider(), mock_client)
        assert mock_client.get.await_count == 2

    async def test_atlascloud_omits_language_in_auto_detect(self):
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        mock_client = _atlas_client(submit=_atlas_response(200, _completed_payload("hi")), polls=[])

        await _run_atlas(AtlasCloudProvider(), mock_client, language="")

        assert "language" not in mock_client.post.await_args.kwargs["json"]

    @pytest.mark.parametrize("status", [401, 403])
    async def test_atlascloud_auth_failure_raises_with_the_provider_message(self, status):
        from portal.transcription.providers.atlascloud import AtlasCloudError, AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(status, {"code": status, "msg": "Invalid key", "data": None}),
            polls=[],
        )

        with pytest.raises(AtlasCloudError, match=rf"HTTP {status}: Invalid key"):
            await _run_atlas(AtlasCloudProvider(), mock_client)
        mock_client.get.assert_not_awaited()

    async def test_atlascloud_api_level_error_body_keeps_its_message(self):
        from portal.transcription.providers.atlascloud import AtlasCloudError, AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 401, "msg": "Invalid key", "data": None}),
            polls=[],
        )

        with pytest.raises(AtlasCloudError, match="Invalid key"):
            await _run_atlas(AtlasCloudProvider(), mock_client)

    async def test_atlascloud_failed_prediction_raises_instead_of_returning_empty(self):
        from portal.transcription.providers.atlascloud import AtlasCloudError, AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing"}}),
            polls=[_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "failed", "error": "bad audio"}})],
        )

        with pytest.raises(AtlasCloudError, match="bad audio"):
            await _run_atlas(AtlasCloudProvider(), mock_client)

    @pytest.mark.parametrize(
        "submit_body",
        [
            ["not", "an", "object"],
            {"code": 200, "data": "not-an-object"},
            {"code": 200, "data": {"status": "processing"}},
        ],
    )
    async def test_atlascloud_malformed_submit_payload_raises(self, submit_body):
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        mock_client = _atlas_client(submit=_atlas_response(200, submit_body), polls=[])

        with pytest.raises(ValueError):
            await _run_atlas(AtlasCloudProvider(), mock_client)

    async def test_atlascloud_retries_transient_poll_failures(self):
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing"}}),
            polls=[
                _atlas_response(429, {"code": 429, "msg": "slow down"}),
                _atlas_response(503, {"code": 503, "msg": "unavailable"}),
                httpx.ConnectError("boom", request=httpx.Request("GET", "https://api.atlascloud.ai/x")),
                _atlas_response(200, _completed_payload("recovered")),
            ],
        )

        assert await _run_atlas(AtlasCloudProvider(), mock_client) == "recovered"
        assert mock_client.get.await_count == 4
        mock_client.post.assert_awaited_once()

    async def test_atlascloud_does_not_retry_an_auth_failure_while_polling(self):
        from portal.transcription.providers.atlascloud import AtlasCloudError, AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing"}}),
            polls=[_atlas_response(401, {"code": 401, "msg": "Invalid key"})],
        )

        with pytest.raises(AtlasCloudError, match="HTTP 401: Invalid key"):
            await _run_atlas(AtlasCloudProvider(), mock_client)
        assert mock_client.get.await_count == 1

    async def test_atlascloud_poll_budget_is_short_enough_for_live_audio(self):
        from portal.transcription.providers import atlascloud

        assert atlascloud.MAX_POLL_ATTEMPTS * atlascloud.POLL_INTERVAL_SECONDS <= 12
        assert atlascloud.TRANSCRIBE_DEADLINE_SECONDS <= 12

    async def test_atlascloud_times_out_when_the_prediction_never_finishes(self):
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        still_running = _atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing"}})
        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing"}}),
            polls=[still_running] * 3,
        )

        with patch("portal.transcription.providers.atlascloud.MAX_POLL_ATTEMPTS", 3):
            with pytest.raises(TimeoutError):
                await _run_atlas(AtlasCloudProvider(), mock_client)
        assert mock_client.get.await_count == 3

    @pytest.mark.parametrize(
        "prediction",
        [
            {"id": "p", "status": "completed"},
            {"id": "p", "status": "completed", "stt_result": {"text": 123}, "outputs": None},
            {"id": "p", "status": "completed", "stt_result": {}, "outputs": []},
            {"id": "p", "status": "completed", "outputs": [None]},
        ],
    )
    async def test_atlascloud_malformed_completed_result_raises_on_submit_path(self, prediction):
        from portal.transcription.providers.atlascloud import AtlasCloudError, AtlasCloudProvider

        mock_client = _atlas_client(submit=_atlas_response(200, {"code": 200, "data": prediction}), polls=[])

        with pytest.raises(AtlasCloudError, match="no valid transcript"):
            await _run_atlas(AtlasCloudProvider(), mock_client)

    @pytest.mark.parametrize(
        "prediction",
        [
            {"id": "p", "status": "completed"},
            {"id": "p", "status": "completed", "stt_result": {"text": 123}, "outputs": None},
        ],
    )
    async def test_atlascloud_malformed_completed_result_raises_on_poll_path(self, prediction):
        from portal.transcription.providers.atlascloud import AtlasCloudError, AtlasCloudProvider

        mock_client = _atlas_client(
            submit=_atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing"}}),
            polls=[_atlas_response(200, {"code": 200, "data": prediction})],
        )

        with pytest.raises(AtlasCloudError, match="no valid transcript"):
            await _run_atlas(AtlasCloudProvider(), mock_client)

    async def test_atlascloud_deadline_covers_slow_http_responses(self):
        import time

        from portal.transcription.providers import atlascloud
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        processing = _atlas_response(200, {"code": 200, "data": {"id": "p", "status": "processing"}})

        async def slow_get(*args, **kwargs):
            await asyncio.sleep(0.2)
            return processing

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=processing)
        mock_client.get = AsyncMock(side_effect=slow_get)

        # Real sleeps: each poll takes ~0.2s of HTTP time plus the poll interval, 12 attempts would take
        # several seconds; the elapsed-time deadline must cut it off first.
        with (
            patch("portal.transcription.providers.atlascloud.get_http_client", return_value=mock_client),
            patch.object(atlascloud, "TRANSCRIBE_DEADLINE_SECONDS", 0.5),
            patch.object(atlascloud, "POLL_INTERVAL_SECONDS", 0.1),
        ):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await AtlasCloudProvider().process_chunk(
                    b"\x00" * 3200, "en-US", "bytedance/seed-asr-2.0", ProviderConfig(api_key="fake")
                )
            elapsed = time.monotonic() - started

        assert elapsed < 1.5
        assert mock_client.post.await_count == 1

    async def test_atlascloud_deadline_covers_a_slow_submission(self):
        import time

        from portal.transcription.providers import atlascloud
        from portal.transcription.providers.atlascloud import AtlasCloudProvider

        async def slow_post(*args, **kwargs):
            await asyncio.sleep(5)

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=slow_post)

        with (
            patch("portal.transcription.providers.atlascloud.get_http_client", return_value=mock_client),
            patch.object(atlascloud, "TRANSCRIBE_DEADLINE_SECONDS", 0.3),
        ):
            started = time.monotonic()
            with pytest.raises(TimeoutError):
                await AtlasCloudProvider().process_chunk(
                    b"\x00" * 3200, "en-US", "bytedance/seed-asr-2.0", ProviderConfig(api_key="fake")
                )

        assert time.monotonic() - started < 1.5

    async def test_atlascloud_missing_key_raises_without_a_request(self):
        from portal.transcription.providers.atlascloud import AtlasCloudError, AtlasCloudProvider

        provider = AtlasCloudProvider()
        with patch("portal.transcription.providers.atlascloud.get_http_client") as mock_get_client:
            with pytest.raises(AtlasCloudError, match="API key missing"):
                await provider.process_chunk(
                    b"\x00" * 100,
                    "en-US",
                    "bytedance/seed-asr-2.0",
                    ProviderConfig(api_key=None),
                )

        mock_get_client.assert_not_called()

    async def test_pcm_to_wav_produces_valid_wav_header(self):
        result = pcm_to_wav(b"\x00" * 3200, sample_rate=16000)
        assert result.startswith(b"RIFF")
        assert len(result) > 3200

    async def test_provider_config_get_key_returns_api_key(self):
        config = ProviderConfig(api_key="test-key-abc")
        assert config.get_key() == "test-key-abc"

    async def test_provider_config_get_key_returns_none(self):
        config = ProviderConfig(api_key=None)
        assert config.get_key() is None

    async def test_openai_process_chunk_returns_empty_on_missing_key(self):
        from portal.transcription.providers.openai import OpenAIProvider

        provider = OpenAIProvider()
        config = ProviderConfig(api_key=None)
        result = await provider.process_chunk(b"\x00" * 100, "en", "whisper-1", config)
        assert result == ""

    async def test_openai_process_chunk_calls_api_with_wav(self):
        from portal.transcription.providers.openai import OpenAIProvider

        provider = OpenAIProvider()
        config = ProviderConfig(api_key="fake")

        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Hello"}
        mock_client.post = AsyncMock(return_value=mock_response)

        pg.shared_http_client = mock_client

        try:
            result = await provider.process_chunk(b"\x00" * 3200, "en", "whisper-1", config)
            assert result == "Hello"

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            parsed_url = urlparse(call_args[0][0])
            assert parsed_url.hostname == "api.openai.com"
        finally:
            pg.shared_http_client = None

    async def test_openai_process_chunk_returns_empty_on_api_error(self):
        from portal.transcription.providers.openai import OpenAIProvider

        provider = OpenAIProvider()
        config = ProviderConfig(api_key="fake")

        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection error"))

        pg.shared_http_client = mock_client

        try:
            with pytest.raises(Exception):
                await provider.process_chunk(b"\x00" * 3200, "en", "whisper-1", config)
        finally:
            pg.shared_http_client = None

    async def test_openai_process_chunk_raises_on_auth_error_without_retry(self):
        from portal.transcription.providers.openai import OpenAIProvider

        provider = OpenAIProvider()
        config = ProviderConfig(api_key="fake")

        mock_client = MagicMock()
        mock_client.is_closed = False
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "unauthorized",
            request=httpx.Request("POST", "https://api.openai.com"),
            response=httpx.Response(401),
        )
        mock_client.post = AsyncMock(return_value=mock_response)

        pg.shared_http_client = mock_client
        try:
            # A 401 must surface (not return an empty transcript) and must not be retried.
            with pytest.raises(httpx.HTTPStatusError):
                await provider.process_chunk(b"\x00" * 3200, "en", "whisper-1", config)
            mock_client.post.assert_called_once()
        finally:
            pg.shared_http_client = None

    async def test_local_model_ref_counting(self):
        from portal.transcription.providers.local import (
            _active_booths_per_model,
            decrement_model_ref,
            increment_model_ref,
        )

        # Ensure clean state for this test
        _active_booths_per_model["tiny"] = 0

        increment_model_ref("tiny")
        increment_model_ref("tiny")
        assert _active_booths_per_model["tiny"] == 2

        decrement_model_ref("tiny")
        assert _active_booths_per_model["tiny"] == 1

        decrement_model_ref("tiny")
        assert _active_booths_per_model["tiny"] == 0

        decrement_model_ref("tiny")
        assert _active_booths_per_model["tiny"] == 0

    async def test_local_model_ref_decrement_never_goes_negative(self):
        from portal.transcription.providers.local import _active_booths_per_model, decrement_model_ref

        decrement_model_ref("nonexistent-model")
        assert _active_booths_per_model.get("nonexistent-model", 0) == 0

    async def test_transcription_eviction_loop_does_not_block_on_model_load(self):
        import threading
        import time

        from portal.transcription.providers.local import _loaded_models, eviction_loop

        model_size = "test-model-slow-load"
        if model_size in _loaded_models:
            del _loaded_models[model_size]

        lock_acquired_event = threading.Event()
        release_lock_event = threading.Event()

        def simulated_slow_load(*args, **kwargs):
            lock_acquired_event.set()
            release_lock_event.wait(timeout=5.0)
            return MagicMock()

        def background_loader():
            with patch("faster_whisper.WhisperModel", side_effect=simulated_slow_load):
                from portal.transcription.providers.local import get_model

                get_model(model_size)

        t = threading.Thread(target=background_loader)
        t.start()

        while not lock_acquired_event.is_set():
            await asyncio.sleep(0.01)

        start_time = time.time()

        with patch("asyncio.sleep", side_effect=[None, asyncio.CancelledError()]):
            try:
                await asyncio.wait_for(eviction_loop(), timeout=1.0)
            except asyncio.CancelledError:
                pass
            except TimeoutError:
                pytest.fail("Eviction loop timed out because it was blocked by the model loading lock!")

        elapsed = time.time() - start_time
        assert elapsed < 1.0, f"Eviction loop blocked for {elapsed} seconds, indicating lock contention!"

        release_lock_event.set()
        t.join()

    async def test_local_provider_applies_hallucination_filters(self):
        import numpy as np

        from portal.transcription.providers.local import LocalProvider

        provider = LocalProvider()

        with patch("portal.transcription.providers.local.get_model") as mock_get_model:
            # Create a dummy function with the expected signature so inspect.signature works
            def dummy_transcribe(
                audio,
                beam_size=5,
                vad_filter=False,
                language=None,
                word_timestamps=False,
                compression_ratio_threshold=2.4,
                no_speech_threshold=0.6,
                log_prob_threshold=-1.0,
                condition_on_previous_text=False,
                **kwargs,
            ):
                pass

            mock_model = MagicMock()
            mock_model.transcribe = MagicMock(spec=dummy_transcribe)
            mock_get_model.return_value = mock_model

            # Mock a segment with valid speech
            mock_segment = MagicMock()
            mock_segment.text = "Hello world"

            mock_word1 = MagicMock()
            mock_word1.word = "Hello"
            mock_word1.end = 1.5

            mock_word2 = MagicMock()
            mock_word2.word = "world"
            mock_word2.end = 2.0

            mock_segment.words = [mock_word1, mock_word2]

            # Transcribe returns an iterable of segments and info
            mock_model.transcribe.return_value = ([mock_segment], None)

            # Run inference with dummy audio
            audio_data = np.zeros(16000, dtype=np.float32)
            result = provider._run_inference(audio_data, "en", "tiny", None)

            # Verify that valid speech passes through
            assert result == "Hello world"

            # Verify that the anti-hallucination filters are strictly applied
            mock_model.transcribe.assert_called_once()
            _, kwargs = mock_model.transcribe.call_args

            assert kwargs.get("compression_ratio_threshold") == 2.4
            assert kwargs.get("no_speech_threshold") == 0.6
            assert kwargs.get("log_prob_threshold") == -1.0
            assert kwargs.get("condition_on_previous_text") is False
            assert kwargs.get("vad_filter") is True


class _FakeStdout:
    def __init__(self, items: list[bytes | float]):
        # Bytes items are returned directly; float items sleep before returning b"" or next chunk.
        self._items = list(items)
        self._idx = 0

    async def read(self, _n: int = 4096) -> bytes:
        while self._idx < len(self._items):
            item = self._items[self._idx]
            self._idx += 1
            if isinstance(item, float):
                await asyncio.sleep(item)
                continue
            return item
        return b""


class _FakeProcess:
    def __init__(self, stdout_items: list[bytes | float]):
        self.returncode = None
        self.stdout = _FakeStdout(stdout_items)


class _FakeSarvamWebSocket:
    def __init__(
        self,
        server_messages: list[dict] | None = None,
        *,
        close_on_end: bool = True,
        auto_close: bool = False,
        end_messages: list[dict] | None = None,
        closed: bool = False,
        receive_error: BaseException | None = None,
    ):
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue[str | BaseException | None] = asyncio.Queue()
        self._close_on_end = close_on_end
        self._end_messages = list(end_messages or [])
        self.closed = closed
        for msg in server_messages or []:
            self._incoming.put_nowait(json.dumps(msg))
        if receive_error is not None:
            self._incoming.put_nowait(receive_error)
        if auto_close:
            self._incoming.put_nowait(None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, raw: str) -> None:
        payload = json.loads(raw)
        self.sent.append(payload)
        if payload.get("event") == "end":
            for msg in self._end_messages:
                await self._incoming.put(json.dumps(msg))
            if self._close_on_end or self._end_messages:
                await self._incoming.put(None)

    def push_server_event(self, payload: dict) -> None:
        self._incoming.put_nowait(json.dumps(payload))

    def close_server_stream(self) -> None:
        self._incoming.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self._incoming.get()
        if item is None:
            raise StopAsyncIteration
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.mark.anyio
class TestSarvamProvider:
    async def test_sarvam_registered_in_worker_providers(self):
        from portal.transcription.constants import ALLOWED_MODELS, ProviderEnum
        from portal.transcription.providers.sarvam import SarvamProvider
        from portal.transcription.worker import PROVIDERS

        assert isinstance(PROVIDERS.get("sarvam"), SarvamProvider)
        assert ProviderEnum.SARVAM.value == "sarvam"
        assert ALLOWED_MODELS[ProviderEnum.SARVAM] == {"saaras:v3-realtime"}

    async def test_sarvam_get_api_key_decrypts_event_key(self):
        from portal.crypto import encrypt_val
        from portal.models import Event
        from portal.transcription.constants import ProviderEnum
        from portal.transcription.providers.base import get_api_key

        event_empty = Event(slug="evt-empty", display_name="Empty")
        assert get_api_key(event_empty, ProviderEnum.SARVAM) is None

        with patch("portal.config.settings.api_key_encryption_key", "test-key-encryption-key-for-sarvam-get-key"):
            import portal.crypto as crypto_mod

            crypto_mod._fernet = None
            try:
                event_with_key = Event(
                    slug="evt-sarvam",
                    display_name="Sarvam",
                    encrypted_sarvam_api_key=encrypt_val("sarvam-secret-123"),
                )
                assert get_api_key(event_with_key, ProviderEnum.SARVAM) == "sarvam-secret-123"
            finally:
                crypto_mod._fernet = None

    @pytest.mark.parametrize(
        ("raw_code", "expected"),
        [
            ("", "auto"),
            ("   ", "auto"),
            (None, "auto"),
            ("floor", "auto"),
            ("auto", "auto"),
            ("or", "or-IN"),
            ("or-IN", "or-IN"),
            ("or-in", "or-IN"),
            ("od-IN", "auto"),
            ("od", "auto"),
            ("hi", "hi-IN"),
            ("hi-IN", "hi-IN"),
            ("en", "en-IN"),
            ("en-IN", "en-IN"),
            ("kok-IN", "kok-IN"),
            ("fr", "auto"),
            ("en-US", "auto"),
        ],
    )
    async def test_sarvam_language_mapping(self, raw_code, expected):
        from portal.transcription.providers.sarvam import map_sarvam_language_code

        assert map_sarvam_language_code(raw_code) == expected

    async def test_sarvam_missing_api_key_does_not_connect(self):
        from portal.transcription.providers.sarvam import SarvamProvider

        provider = SarvamProvider()
        process = _FakeProcess([b"\x01\x02"])
        broadcast = AsyncMock()

        with patch("websockets.connect") as mock_connect:
            await provider.run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key=None), broadcast, "evt-1-hi"
            )

        mock_connect.assert_not_called()
        broadcast.assert_not_awaited()

    async def test_sarvam_connects_with_verified_params_and_frames_audio_and_eof(self):
        from portal.transcription.providers.sarvam import SarvamProvider

        pcm_chunk = b"\x01\x02\x03\x04" * 100
        ws = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-123"},
                {"event": "transcript.partial", "utterance_idx": 0, "text": "नमस्ते"},
            ],
            end_messages=[
                {"event": "transcript.final", "utterance_idx": 0, "text": "नमस्ते दुनिया"},
                {"event": "session.end", "request_id": "req-123", "audio_duration_s": "1.2"},
            ],
            close_on_end=True,
        )
        process = _FakeProcess([pcm_chunk])
        broadcasts: list[tuple[str, object]] = []

        async def broadcast(booth_id, payload):
            broadcasts.append((booth_id, payload))

        connect_mock = MagicMock(return_value=ws)
        with patch("websockets.connect", connect_mock):
            await SarvamProvider().run_stream(
                process, "or", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), broadcast, "evt-1-or"
            )

        connect_mock.assert_called_once()
        called_url = connect_mock.call_args.args[0]
        called_headers = connect_mock.call_args.kwargs["additional_headers"]
        parsed = urlparse(called_url)
        query = parse_qs(parsed.query)

        assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "wss://api.sarvam.ai/speech-to-text-realtime/ws"
        assert called_headers == {"Api-Subscription-Key": "sarvam-secret"}
        assert query == {
            "language_code": ["or-IN"],
            "model": ["saaras:v3-realtime"],
            "encoding": ["linear16"],
            "sample_rate": ["16000"],
            "stream_type": ["balanced"],
            "endpointing": ["vad"],
            "mode": ["transcribe"],
        }
        assert "keyterms" not in query

        assert ws.sent[0] == {
            "event": "audio_input",
            "audio": base64.b64encode(pcm_chunk).decode("utf-8"),
        }
        assert ws.sent[-1] == {"event": "end"}

        partial_msgs = [p for _, p in broadcasts if isinstance(p, dict) and p.get("status") == "partial"]
        final_msgs = [p for _, p in broadcasts if isinstance(p, dict) and p.get("status") == "final"]
        assert len(partial_msgs) == 1
        assert partial_msgs[0]["text"] == "नमस्ते"
        assert len(final_msgs) == 1
        assert final_msgs[0]["text"] == "नमस्ते दुनिया"

    async def test_sarvam_periodic_ping_sent_without_overlapping_tasks(self):
        from portal.transcription.providers import sarvam
        from portal.transcription.providers.sarvam import SarvamProvider

        ws = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-ping"},
                {"event": "pong"},
            ],
            close_on_end=True,
        )
        # Hold stdout open for ~0.06s so multiple 0.015s pings fire before EOF.
        process = _FakeProcess([b"\x00\x01", 0.06])
        broadcast = AsyncMock()

        with (
            patch.object(sarvam, "PING_INTERVAL_SECONDS", 0.015),
            patch("websockets.connect", return_value=ws),
        ):
            await SarvamProvider().run_stream(
                process, "floor", "", ProviderConfig(api_key="sarvam-secret"), broadcast, "evt-1-floor"
            )

        ping_events = [msg for msg in ws.sent if msg.get("event") == "ping"]
        assert len(ping_events) >= 2
        assert ws.sent[-1] == {"event": "end"}

    async def test_sarvam_transient_failure_reconnects_with_bounded_backoff(self):
        from portal.transcription.providers import sarvam
        from portal.transcription.providers.sarvam import SarvamProvider

        good_ws = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-ok"},
                {"event": "transcript.final", "utterance_idx": 0, "text": "Recovered text"},
                {"event": "session.end", "request_id": "req-ok"},
            ],
            close_on_end=False,
        )
        process = _FakeProcess([b"\x01\x02"])
        broadcasts: list[dict] = []

        async def broadcast(_booth_id, payload):
            broadcasts.append(payload)

        connect_mock = MagicMock(side_effect=[OSError("temporary network glitch"), good_ws])
        with (
            patch("websockets.connect", connect_mock),
            patch.object(sarvam, "BACKOFF_BASE_SECONDS", 0.001),
        ):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), broadcast, "evt-1-hi"
            )

        assert connect_mock.call_count == 2
        assert any(m.get("status") == "final" and m.get("text") == "Recovered text" for m in broadcasts)

    async def test_sarvam_gives_up_after_max_transient_reconnect_attempts(self):
        from portal.transcription.providers import sarvam
        from portal.transcription.providers.sarvam import SarvamProvider

        process = _FakeProcess([b"\x01\x02"])
        connect_mock = MagicMock(side_effect=OSError("persistent network failure"))

        with (
            patch("websockets.connect", connect_mock),
            patch.object(sarvam, "MAX_RECONNECT_ATTEMPTS", 3),
            patch.object(sarvam, "BACKOFF_BASE_SECONDS", 0.001),
        ):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), AsyncMock(), "evt-1-hi"
            )

        assert connect_mock.call_count == 3

    @pytest.mark.parametrize(
        "error_event",
        [
            {"event": "error", "code": "unauthorized", "is_fatal": True, "status_code": 401, "message": "Invalid key"},
            {"event": "error", "code": "invalid_request", "is_fatal": True, "status_code": 400, "message": "Bad param"},
        ],
    )
    async def test_sarvam_fatal_error_event_does_not_reconnect(self, error_event):
        from portal.transcription.providers.sarvam import SarvamProvider

        ws = _FakeSarvamWebSocket(server_messages=[error_event], close_on_end=False)
        # Keep stdout paused so receiver handles the fatal error event before EOF.
        process = _FakeProcess([0.05, b"\x01\x02"])
        connect_mock = MagicMock(return_value=ws)

        with patch("websockets.connect", connect_mock):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="bad-key"), AsyncMock(), "evt-1-hi"
            )

        connect_mock.assert_called_once()

    async def test_sarvam_non_fatal_error_keeps_session_alive_and_fatal_close_code_stops(self):
        from portal.transcription.providers.sarvam import SarvamProvider

        ws = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-1"},
                {"event": "error", "code": "invalid_config", "is_fatal": False, "message": "Ignored update"},
                {"event": "transcript.final", "utterance_idx": 0, "text": "Still working"},
                {"event": "session.end", "request_id": "req-1"},
            ],
            close_on_end=False,
        )
        process = _FakeProcess([b"\x01\x02"])
        broadcasts: list[dict] = []

        async def broadcast(_booth_id, payload):
            broadcasts.append(payload)

        with patch("websockets.connect", return_value=ws):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), broadcast, "evt-1-hi"
            )

        assert any(m.get("status") == "final" and m.get("text") == "Still working" for m in broadcasts)

        class _FatalCloseError(Exception):
            code = 4000

        connect_mock = MagicMock(side_effect=_FatalCloseError("Unsupported model"))
        with patch("websockets.connect", connect_mock):
            await SarvamProvider().run_stream(
                _FakeProcess([b"\x01\x02"]),
                "hi-IN",
                "saaras:v3-realtime",
                ProviderConfig(api_key="sarvam-secret"),
                AsyncMock(),
                "evt-1-hi",
            )
        connect_mock.assert_called_once()

    @pytest.mark.parametrize("model_variant", ["", "   ", None, "saaras:v3-realtime"])
    async def test_sarvam_valid_and_empty_model_variants_resolve_to_v3_realtime(self, model_variant):
        from portal.transcription.providers.sarvam import build_sarvam_ws_url, validate_sarvam_model

        assert validate_sarvam_model(model_variant) == "saaras:v3-realtime"
        query = parse_qs(urlparse(build_sarvam_ws_url("hi-IN", model_variant)).query)
        assert query["model"] == ["saaras:v3-realtime"]

    @pytest.mark.parametrize("invalid_model", ["tiny", "saaras:v3", "saaras:v4", "nova-2"])
    async def test_sarvam_invalid_model_variant_rejected_before_connect(self, invalid_model):
        from portal.transcription.providers.sarvam import SarvamProvider, validate_sarvam_model

        with pytest.raises(ValueError, match="Unsupported Sarvam model"):
            validate_sarvam_model(invalid_model)

        with patch("websockets.connect") as mock_connect:
            with pytest.raises(ValueError, match="Unsupported Sarvam model"):
                await SarvamProvider().run_stream(
                    _FakeProcess([b"\x01\x02"]),
                    "hi-IN",
                    invalid_model,
                    ProviderConfig(api_key="sarvam-secret"),
                    AsyncMock(),
                    "evt-1-hi",
                )
        mock_connect.assert_not_called()

    async def test_sarvam_post_handshake_immediate_close_stops_after_max_attempts(self):
        from portal.transcription.providers import sarvam
        from portal.transcription.providers.sarvam import SarvamProvider

        # Each WS connects and even emits session.begin, then closes immediately without transcript progress.
        sockets = [
            _FakeSarvamWebSocket(
                server_messages=[{"event": "session.begin", "request_id": f"req-{i}"}],
                auto_close=True,
            )
            for i in range(5)
        ]
        # Keep stdout open across attempts so EOF does not exit early.
        process = _FakeProcess([b"\x01\x02", 0.02] * 10)
        connect_mock = MagicMock(side_effect=sockets)

        with (
            patch("websockets.connect", connect_mock),
            patch.object(sarvam, "MAX_RECONNECT_ATTEMPTS", 3),
            patch.object(sarvam, "BACKOFF_BASE_SECONDS", 0.001),
        ):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), AsyncMock(), "evt-1-hi"
            )

        assert connect_mock.call_count == 3

    async def test_sarvam_post_handshake_transient_503_stops_after_max_attempts(self):
        from portal.transcription.providers import sarvam
        from portal.transcription.providers.sarvam import SarvamProvider

        sockets = [
            _FakeSarvamWebSocket(
                server_messages=[
                    {"event": "session.begin", "request_id": f"req-{i}"},
                    {"event": "error", "code": "service_unavailable", "is_fatal": True, "status_code": 503, "message": "Busy"},
                ],
                close_on_end=False,
            )
            for i in range(5)
        ]
        process = _FakeProcess([b"\x01\x02", 0.01] * 10)
        connect_mock = MagicMock(side_effect=sockets)

        with (
            patch("websockets.connect", connect_mock),
            patch.object(sarvam, "MAX_RECONNECT_ATTEMPTS", 3),
            patch.object(sarvam, "BACKOFF_BASE_SECONDS", 0.001),
        ):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), AsyncMock(), "evt-1-hi"
            )

        assert connect_mock.call_count == 3

    async def test_sarvam_resets_retry_counter_after_transcript_progress(self):
        from portal.transcription.providers import sarvam
        from portal.transcription.providers.sarvam import SarvamProvider

        # With MAX_RECONNECT_ATTEMPTS=3:
        # 2 pre-connect glitches (1/3, 2/3) -> healthy transcript session resets counter ->
        # drop (1/3) + 1 more glitch (2/3) -> recovers on attempt 5 (total 4 errors > budget 3).
        ws_progress_then_drop = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-1"},
                {"event": "transcript.final", "utterance_idx": 0, "text": "First utterance"},
            ],
            auto_close=True,
        )
        ws_recovered = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-2"},
                {"event": "transcript.final", "utterance_idx": 0, "text": "Second utterance"},
            ],
            end_messages=[{"event": "session.end", "request_id": "req-2"}],
            close_on_end=True,
        )
        process = _FakeProcess([b"\x01\x02", 0.02, b"\x03\x04"])
        broadcasts: list[dict] = []

        async def broadcast(_booth_id, payload):
            broadcasts.append(payload)

        connect_mock = MagicMock(
            side_effect=[
                OSError("glitch-1"),
                OSError("glitch-2"),
                ws_progress_then_drop,
                OSError("glitch-3"),
                ws_recovered,
            ]
        )

        with (
            patch("websockets.connect", connect_mock),
            patch.object(sarvam, "MAX_RECONNECT_ATTEMPTS", 3),
            patch.object(sarvam, "BACKOFF_BASE_SECONDS", 0.001),
        ):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), broadcast, "evt-1-hi"
            )

        assert connect_mock.call_count == 5
        texts = [m.get("text") for m in broadcasts if m.get("status") == "final"]
        assert texts == ["First utterance", "Second utterance"]

    async def test_sarvam_eof_with_usable_ws_and_receiver_parse_error_sends_end_without_reconnect(self):
        from portal.transcription.providers.sarvam import SarvamProvider

        # Receiver raises non-connection ValueError in the same tick sender hits EOF, while WS is still open.
        ws = _FakeSarvamWebSocket(receive_error=ValueError("malformed frame"), closed=False, close_on_end=False)
        process = _FakeProcess([])
        connect_mock = MagicMock(return_value=ws)

        with patch("websockets.connect", connect_mock):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), AsyncMock(), "evt-1-hi"
            )

        connect_mock.assert_called_once()
        assert {"event": "end"} in ws.sent

    async def test_sarvam_eof_with_broken_ws_does_not_send_end_or_reconnect(self):
        from portal.transcription.providers.sarvam import SarvamProvider

        class _BrokenWSCloseError(Exception):
            code = 1006

        ws = _FakeSarvamWebSocket(
            receive_error=_BrokenWSCloseError("socket died"),
            closed=True,
            close_on_end=False,
        )
        process = _FakeProcess([])
        connect_mock = MagicMock(return_value=ws)

        with patch("websockets.connect", connect_mock):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), AsyncMock(), "evt-1-hi"
            )

        connect_mock.assert_called_once()
        assert {"event": "end"} not in ws.sent

    @pytest.mark.parametrize(
        "fatal_event",
        [
            {"event": "error", "code": "invalid_config", "is_fatal": True, "message": "Fatal invalid config"},
            {"event": "error", "code": "insufficient_quota_error", "is_fatal": True, "status_code": 402, "message": "No credits"},
            {"event": "error", "code": "authentication_error", "is_fatal": True, "status_code": 403, "message": "Forbidden"},
            {"event": "error", "code": "unclassified_fatal", "is_fatal": True, "status_code": 499, "message": "Fatal unknown"},
        ],
    )
    async def test_sarvam_all_fatal_error_events_stop_without_retry(self, fatal_event):
        from portal.transcription.providers.sarvam import SarvamProvider

        ws = _FakeSarvamWebSocket(server_messages=[fatal_event], close_on_end=False)
        process = _FakeProcess([0.05, b"\x01\x02"])
        connect_mock = MagicMock(return_value=ws)

        with patch("websockets.connect", connect_mock):
            await SarvamProvider().run_stream(
                process, "hi-IN", "saaras:v3-realtime", ProviderConfig(api_key="sarvam-secret"), AsyncMock(), "evt-1-hi"
            )

        connect_mock.assert_called_once()

    async def test_sarvam_close_code_1003_distinguishes_rate_limit_from_invalid_key(self):
        from portal.transcription.providers import sarvam
        from portal.transcription.providers.sarvam import SarvamProvider

        class _Close1003(Exception):
            code = 1003

            def __init__(self, reason: str):
                super().__init__(reason)
                self.reason = reason

        # 1003 with rate-limit reason is transient and recovers on retry.
        good_ws = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-rl"},
                {"event": "transcript.final", "utterance_idx": 0, "text": "After rate limit"},
                {"event": "session.end", "request_id": "req-rl"},
            ],
            close_on_end=False,
        )
        broadcasts: list[dict] = []

        async def broadcast(_booth_id, payload):
            broadcasts.append(payload)

        rate_limit_connect = MagicMock(side_effect=[_Close1003("Rate limit exceeded"), good_ws])
        with (
            patch("websockets.connect", rate_limit_connect),
            patch.object(sarvam, "BACKOFF_BASE_SECONDS", 0.001),
        ):
            await SarvamProvider().run_stream(
                _FakeProcess([b"\x01\x02"]),
                "hi-IN",
                "saaras:v3-realtime",
                ProviderConfig(api_key="sarvam-secret"),
                broadcast,
                "evt-1-hi",
            )
        assert rate_limit_connect.call_count == 2
        assert any(m.get("text") == "After rate limit" for m in broadcasts)

        # 1003 with invalid subscription key reason is fatal and does not retry.
        auth_connect = MagicMock(side_effect=_Close1003("Invalid subscription key"))
        with patch("websockets.connect", auth_connect):
            await SarvamProvider().run_stream(
                _FakeProcess([b"\x01\x02"]),
                "hi-IN",
                "saaras:v3-realtime",
                ProviderConfig(api_key="bad-key"),
                AsyncMock(),
                "evt-1-hi",
            )
        auth_connect.assert_called_once()

    async def test_sarvam_transient_429_error_event_retries_and_recovers(self):
        from portal.transcription.providers import sarvam
        from portal.transcription.providers.sarvam import SarvamProvider

        ws_429 = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-1"},
                {"event": "error", "code": "rate_limit_exceeded_error", "is_fatal": True, "status_code": 429, "message": "Slow down"},
            ],
            close_on_end=False,
        )
        ws_ok = _FakeSarvamWebSocket(
            server_messages=[
                {"event": "session.begin", "request_id": "req-2"},
                {"event": "transcript.final", "utterance_idx": 0, "text": "Recovered after 429"},
                {"event": "session.end", "request_id": "req-2"},
            ],
            close_on_end=False,
        )
        broadcasts: list[dict] = []

        async def broadcast(_booth_id, payload):
            broadcasts.append(payload)

        connect_mock = MagicMock(side_effect=[ws_429, ws_ok])
        with (
            patch("websockets.connect", connect_mock),
            patch.object(sarvam, "BACKOFF_BASE_SECONDS", 0.001),
        ):
            await SarvamProvider().run_stream(
                _FakeProcess([0.02, b"\x01\x02"]),
                "hi-IN",
                "saaras:v3-realtime",
                ProviderConfig(api_key="sarvam-secret"),
                broadcast,
                "evt-1-hi",
            )

        assert connect_mock.call_count == 2
        assert any(m.get("text") == "Recovered after 429" for m in broadcasts)
