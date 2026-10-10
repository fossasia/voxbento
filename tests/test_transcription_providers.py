from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

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
