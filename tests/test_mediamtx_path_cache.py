"""Regression tests for issue #729: MediaMTX can restart on its own and come
back without the paths the portal added at runtime, while the portal's
in-memory cache still lists them. A cached path has to be confirmed before the
portal relies on it."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import portal.globals as pg

pytestmark = pytest.mark.anyio

CHANNEL = "voxbento-demo-1-en"


def _client(*, get_response=None, get_error=None, post_response=None):
    client = MagicMock()
    client.is_closed = False
    client.get = AsyncMock(side_effect=get_error) if get_error else AsyncMock(return_value=get_response)
    client.post = AsyncMock(return_value=post_response)
    client.patch = AsyncMock(return_value=post_response)
    return client


@contextmanager
def _cached(channel: str):
    """Put *channel* in the created-path cache, then hand the real set back."""
    import portal.utils as utils_mod

    saved = set(utils_mod._created_paths)
    utils_mod._created_paths.add(channel)
    try:
        yield utils_mod
    finally:
        utils_mod._created_paths.clear()
        utils_mod._created_paths.update(saved)


async def _ensure(channel: str = CHANNEL) -> None:
    import portal.utils as utils_mod
    from portal.config import settings

    with patch.object(settings, "mediamtx_api_base", "http://mediamtx:9997"):
        await utils_mod._ensure_mediamtx_path(channel)


class TestCachedMediaMtxPaths:
    async def test_a_path_the_media_server_still_has_is_left_alone(self):
        client = _client(get_response=MagicMock(status_code=200), post_response=MagicMock(status_code=200))
        pg.shared_http_client = client

        with _cached(CHANNEL):
            await _ensure()

            assert str(client.get.call_args.args[0]).endswith(f"/v3/config/paths/get/{CHANNEL}")
            client.post.assert_not_called()

    async def test_a_path_lost_to_a_media_server_restart_is_created_again(self):
        client = _client(get_response=MagicMock(status_code=404), post_response=MagicMock(status_code=200))
        pg.shared_http_client = client

        with _cached(CHANNEL) as utils_mod:
            await _ensure()

            assert str(client.post.call_args.args[0]).endswith(f"/v3/config/paths/add/{CHANNEL}")
            assert CHANNEL in utils_mod._created_paths

    async def test_an_unreachable_media_server_keeps_the_cache(self):
        client = _client(get_error=httpx.ConnectError("media server is down"))
        pg.shared_http_client = client

        with _cached(CHANNEL) as utils_mod:
            await _ensure()

            client.post.assert_not_called()
            assert CHANNEL in utils_mod._created_paths
