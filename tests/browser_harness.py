"""Shared setup for the real-browser rehearsals (listener channel switching, interpreter handoff).

Import this module before anything from ``portal`` or ``fastapi_app``: it supplies the
secrets the settings need, and it points the app at an in-memory database and no MediaMTX.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

os.environ.setdefault("API_KEY_ENCRYPTION_KEY", "browser-test-encryption-key-32-chars!")
os.environ.setdefault("SECRET_KEY", "browser-test-jwt-key-for-isolated-runs")

import uvicorn

from fastapi_app import app
from portal.config import settings
from portal.database import configure, dispose, init_db

# Chromium flags shared by the rehearsals: play WebRTC audio without a click, and use
# real loopback addresses instead of mDNS names, which containers often cannot resolve.
CHROMIUM_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--disable-features=WebRtcHideLocalIpsWithMdns",
]


# Pages that load the Tailwind Play CDN get this instead, so a slow or unreachable CDN
# cannot fail a run. It keeps the utility classes that decide what is shown or clickable.
TAILWIND_SHIM = """
window.tailwind = window.tailwind || {};
document.head.insertAdjacentHTML('beforeend', '<style>'
  + '.hidden{display:none}.invisible{visibility:hidden}.opacity-0{opacity:0}'
  + '.absolute{position:absolute}.relative{position:relative}.top-full{top:100%}.left-0{left:0}.right-0{right:0}'
  + '.z-\\\\[100\\\\]{z-index:100}.bg-white{background:#fff}'
  + '</style>');
"""


async def offline_cdn(route) -> None:
    """``page.route`` handler for https://cdn.tailwindcss.com: serve ``TAILWIND_SHIM``."""
    await route.fulfill(status=200, content_type="text/javascript", body=TAILWIND_SHIM)


def runs_requested(env_var: str = "BROWSER_TEST_RUNS") -> int:
    """How many times to repeat a rehearsal in one process (for flakiness checks)."""
    return max(1, int(os.environ.get(env_var) or "1"))


@asynccontextmanager
async def live_portal() -> AsyncIterator[str]:
    """Serve the portal on an ephemeral port with an empty in-memory database; yield its origin.

    MediaMTX is switched off (no path creation) and WHEP URLs point at ``{origin}/media``,
    so a test can answer them with ``page.route``.
    """
    configure("sqlite+aiosqlite://")
    await init_db()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    saved = settings.mediamtx_whip_base, settings.mediamtx_api_base
    settings.mediamtx_whip_base = f"{origin}/media"
    settings.mediamtx_api_base = ""
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="warning", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        yield origin
    finally:
        server.should_exit = True
        await task
        sock.close()
        settings.mediamtx_whip_base, settings.mediamtx_api_base = saved
        await dispose()


async def until(check: Callable[[], bool | Awaitable[bool]], what: str, timeout: float = 10.0) -> None:
    """Poll ``check`` until it is true, failing with ``what`` after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if isinstance(result, Awaitable):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out waiting for: {what}")
        await asyncio.sleep(0.05)
