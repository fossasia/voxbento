"""Regression tests for issue #1403: the created-paths cache must be cleared when
MediaMTX is unreachable, so paths are re-created after a MediaMTX restart."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import portal.globals as pg
import portal.utils as utils_mod
from portal.config import settings

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_state():
    utils_mod._created_paths.clear()
    utils_mod._mediamtx_cache = None
    yield
    utils_mod._created_paths.clear()
    utils_mod._mediamtx_cache = None


async def test_health_check_failure_clears_created_paths():
    utils_mod._created_paths.add("chan-1")
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    pg.shared_http_client = mock_client

    with patch.object(settings, "mediamtx_api_base", "http://mediamtx:9997"):
        assert await utils_mod._check_mediamtx() is False

    assert utils_mod._created_paths == set()


async def test_health_check_success_keeps_created_paths():
    utils_mod._created_paths.add("chan-1")
    response = MagicMock()
    response.status_code = 200
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get = AsyncMock(return_value=response)
    pg.shared_http_client = mock_client

    with patch.object(settings, "mediamtx_api_base", "http://mediamtx:9997"):
        assert await utils_mod._check_mediamtx() is True

    assert "chan-1" in utils_mod._created_paths


async def test_unreachable_mediamtx_during_ensure_clears_cache_so_path_is_recreated():
    utils_mod._created_paths.update({"chan-1", "chan-2"})
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    pg.shared_http_client = mock_client

    with patch.object(settings, "mediamtx_api_base", "http://mediamtx:9997"):
        await utils_mod._ensure_mediamtx_path("chan-3")
    assert utils_mod._created_paths == set()

    ok = MagicMock()
    ok.status_code = 200
    mock_client.post = AsyncMock(return_value=ok)
    with patch.object(settings, "mediamtx_api_base", "http://mediamtx:9997"):
        await utils_mod._ensure_mediamtx_path("chan-1")
    mock_client.post.assert_called_once()
    assert "chan-1" in utils_mod._created_paths
