from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx
import pytest

import portal.globals as pg
from portal.transcription.providers.base import ProviderConfig, TranscriptionProvider, pcm_to_wav


@pytest.mark.anyio
class TestTranscriptionProviders:
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
                **kwargs
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
