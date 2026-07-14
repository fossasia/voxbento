from __future__ import annotations

import pytest

import portal.globals as g


@pytest.mark.anyio
class TestSharedHttpClient:
    async def test_lazily_creates_a_client_when_none_is_set(self):
        g.shared_http_client = None
        try:
            client = g.get_http_client()
            assert client is not None
            assert g.shared_http_client is client
        finally:
            await g.shared_http_client.aclose()
            g.shared_http_client = None

    async def test_reuses_the_same_client_across_calls(self):
        """The whole point of `get_http_client` is to avoid opening a fresh
        connection pool per request, so repeated calls must return the exact
        same client instance rather than constructing a new one each time."""
        g.shared_http_client = None
        try:
            first = g.get_http_client()
            second = g.get_http_client()
            assert first is second
        finally:
            await g.shared_http_client.aclose()
            g.shared_http_client = None

    async def test_returns_the_lifespan_managed_client_when_already_set(self):
        sentinel = object()
        g.shared_http_client = sentinel
        try:
            assert g.get_http_client() is sentinel
        finally:
            g.shared_http_client = None
