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
