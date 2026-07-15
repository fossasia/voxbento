"""Regression tests for issue #245: outbound HTTP calls must reuse a shared
httpx.AsyncClient instead of opening a fresh connection pool per request.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import portal.globals as pg

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def reset_shared_client():
    pg.shared_http_client = None
    yield
    pg.shared_http_client = None


class TestGetHttpClient:
    def test_lazily_creates_a_client(self):
        assert pg.shared_http_client is None
        client = pg.get_http_client()
        assert isinstance(client, httpx.AsyncClient)
        assert pg.shared_http_client is client

    def test_returns_the_same_instance_on_repeat_calls(self):
        first = pg.get_http_client()
        second = pg.get_http_client()
        assert first is second


class TestTranslationWorkerUsesSharedClient:
    async def test_call_llm_uses_shared_client_not_a_new_one(self):
        from portal.translations.worker import TranslationWorker

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "Bonjour"}}]}

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        pg.shared_http_client = mock_client

        worker = TranslationWorker(broadcast_callback=None)
        with patch("httpx.AsyncClient", side_effect=AssertionError("must not create a new AsyncClient")):
            result = await worker._call_llm("openai", "gpt-4o-mini", "sk-test", "Hello", "French")

        assert result == "Bonjour"
        mock_client.post.assert_called_once()


class TestTTSWorkerUsesSharedClient:
    async def test_stream_llm_uses_shared_client_not_a_new_one(self):
        from portal.tts.worker import TTSWorker

        class _FakeStreamCtx:
            async def __aenter__(self):
                response = MagicMock()
                response.raise_for_status = MagicMock()

                async def _lines():
                    yield 'data: {"choices": [{"delta": {"content": "Bon"}}]}'
                    yield 'data: {"choices": [{"delta": {"content": "jour"}}]}'
                    yield "data: [DONE]"

                response.aiter_lines = _lines
                return response

            async def __aexit__(self, *exc):
                return False

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=_FakeStreamCtx())
        pg.shared_http_client = mock_client

        worker = TTSWorker(broadcast_audio_callback=None)
        with patch("httpx.AsyncClient", side_effect=AssertionError("must not create a new AsyncClient")):
            chunks = [c async for c in worker._stream_llm("openai", "gpt-4o-mini", "sk-test", "Hello", "French")]

        assert "".join(chunks) == "Bonjour"
        mock_client.stream.assert_called_once()


class TestUtilsUseSharedClient:
    async def test_check_mediamtx_uses_shared_client(self):
        from portal.config import settings
        from portal.utils import _check_mediamtx

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        pg.shared_http_client = mock_client

        with patch.object(settings, "mediamtx_api_base", "http://mediamtx:9997"):
            with patch("httpx.AsyncClient", side_effect=AssertionError("must not create a new AsyncClient")):
                reachable = await _check_mediamtx()

        assert reachable is True
        mock_client.get.assert_called_once()

    async def test_ensure_mediamtx_path_uses_shared_client(self):
        import portal.utils as utils_mod
        from portal.config import settings

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        pg.shared_http_client = mock_client
        utils_mod._created_paths.discard("chan-1")

        with patch.object(settings, "mediamtx_api_base", "http://mediamtx:9997"):
            with patch("httpx.AsyncClient", side_effect=AssertionError("must not create a new AsyncClient")):
                await utils_mod._ensure_mediamtx_path("chan-1")

        mock_client.post.assert_called_once()
        assert "chan-1" in utils_mod._created_paths
