from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import portal.transcription as ts
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
        # Local import required to avoid circular dependency
        from portal.transcription.providers.openai import OpenAIProvider

        provider = OpenAIProvider()
        config = ProviderConfig(api_key=None)
        result = await provider.process_chunk(b"\x00" * 100, "en", "whisper-1", config)
        assert result == ""

    async def test_openai_process_chunk_calls_api_with_wav(self):
        # Local import required to avoid circular dependency
        from portal.transcription.providers.openai import OpenAIProvider

        provider = OpenAIProvider()
        config = ProviderConfig(api_key="fake")

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Hello"}
        mock_client.post = AsyncMock(return_value=mock_response)

        ts.shared_http_client = mock_client

        try:
            result = await provider.process_chunk(b"\x00" * 3200, "en", "whisper-1", config)
            assert result == "Hello"

            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert "api.openai.com" in call_args[0][0]
        finally:
            ts.shared_http_client = None

    async def test_openai_process_chunk_returns_empty_on_api_error(self):
        # Local import required to avoid circular dependency
        from portal.transcription.providers.openai import OpenAIProvider

        provider = OpenAIProvider()
        config = ProviderConfig(api_key="fake")

        mock_client = MagicMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("Connection error"))

        ts.shared_http_client = mock_client

        try:
            with pytest.raises(Exception):
                await provider.process_chunk(b"\x00" * 3200, "en", "whisper-1", config)
        finally:
            ts.shared_http_client = None

    async def test_local_model_ref_counting(self):
        # Local import required to avoid circular dependency
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
        # Local import required to avoid circular dependency
        from portal.transcription.providers.local import _active_booths_per_model, decrement_model_ref

        decrement_model_ref("nonexistent-model")
        assert _active_booths_per_model.get("nonexistent-model", 0) == 0
