from __future__ import annotations

import time

import httpx

from portal.booth_state import BoothRegistry

booths = BoothRegistry()
_JS_CACHE_BUST = str(int(time.time()))

# Process-wide httpx client, started/closed by the FastAPI lifespan in
# `fastapi_app.py`. Reused by hot paths (translation, TTS, MediaMTX/admin
# calls) instead of opening a fresh connection pool and TLS context per
# request. Callers should pass an explicit `timeout=` to individual requests
# rather than relying on the client's own default, since a single shared
# client is used for calls that previously had different per-call timeouts.
shared_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared httpx client, lazily creating one if the lifespan
    hasn't started it yet (e.g. when called from tests or scripts)."""
    global shared_http_client
    if shared_http_client is None:
        shared_http_client = httpx.AsyncClient()
    return shared_http_client
