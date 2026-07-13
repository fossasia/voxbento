import time

import httpx

from portal.booth_state import BoothRegistry

booths = BoothRegistry()
_JS_CACHE_BUST = str(int(time.time()))

# Shared connection pool for outbound API calls (LLM translation, TTS, floor-bot,
# MediaMTX). Set by the FastAPI lifespan handler on startup and closed on
# shutdown; get_http_client() lazily creates it for contexts (e.g. tests) that
# run outside the lifespan.
shared_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global shared_http_client
    if shared_http_client is None:
        shared_http_client = httpx.AsyncClient(timeout=10.0)
    return shared_http_client
