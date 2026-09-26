"""Tests for the local machine translation provider using Ray Serve."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from portal.translations.providers.local import LocalProvider


@pytest.mark.anyio
async def test_local_provider_translate_success():
    """Test that the local provider correctly constructs the payload and calls the Ray client."""
    provider = LocalProvider()

    mock_ray_client = AsyncMock()
    mock_ray_client.predict.return_value = {"translated_text": "Bonjour"}
    provider.ray_client = mock_ray_client

    result = await provider.translate(
        provider_name="local",
        text="Hello",
        target_lang_name="French",
        target_lang_code="fr",
        source_lang_name="English",
        model="nllb-200-distilled-600M",
        api_key=None,
    )
    assert result == "Bonjour"

    mock_ray_client.predict.assert_called_once()
    call_args = mock_ray_client.predict.call_args[0]
    assert call_args[0] == "translator"
    payload = call_args[1]
    assert payload["text"] == "Hello"
    assert payload["source_lang_token"] == "eng_Latn"
    assert payload["target_lang_token"] == "fra_Latn"


@pytest.mark.anyio
async def test_local_provider_translate_invalid_language():
    """Test that the local provider handles invalid languages gracefully without calling Ray."""
    provider = LocalProvider()

    mock_ray_client = AsyncMock()
    provider.ray_client = mock_ray_client

    result = await provider.translate(
        provider_name="local",
        text="Hello",
        target_lang_name="UnknownLanguage",
        target_lang_code="xx",
        source_lang_name="English",
        model="nllb-200-distilled-600M",
        api_key=None,
    )
    assert result is None
    mock_ray_client.predict.assert_not_called()
