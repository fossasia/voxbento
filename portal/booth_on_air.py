"""Whether an interpreter booth is on air for listeners (#1397).

A booth is on air while Mission Control has it unlocked and its interpreter reports a
connected stream. The interpreter's browser can report connected after the stream died
(#1357), so the MediaMTX path must also have a publisher. A path with ``alwaysAvailable``
stays ready and keeps counting inbound bytes while empty, but its ``source`` is null
without a publisher, so ``source`` is what is read here.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from time import monotonic

import httpx

from portal.config import settings
from portal.globals import get_http_client
from portal.outage_log import OutageLog

logger = logging.getLogger(__name__)

PATH_LIST_TIMEOUT = 3.0
PATH_GET_TIMEOUT = 1.0
# A flapping client can't start more than one go-live check per path in this window, and a
# check reads the path at most CONFIRM_ATTEMPTS times.
CONFIRM_INTERVAL_SECONDS = 1.0
CONFIRM_ATTEMPTS = 6
CONFIRM_RETRY_SECONDS = 0.25

# Paths that had a publisher at the last read, or None when that is unknown (no read yet,
# no listeners, or MediaMTX unreachable). Unknown falls back to the interpreter's own report.
live_paths: frozenset[str] | None = None
path_list_outage = OutageLog(logger, "booth_on_air path_list_unavailable")
path_get_outage = OutageLog(logger, "booth_on_air path_get_unavailable")
# When each path was last checked on go-live; a path is in flight while its check runs.
confirmed_at: dict[str, float] = {}
confirming: set[str] = set()
# When a go-live check last found each path's publisher, so a path list read that started
# earlier (still in flight) does not drop it again.
found_live_at: dict[str, float] = {}


# "overloaded" is set when the booth's transcription fails; the interpreter is still publishing.
_PUBLISHING = frozenset({"connected", "overloaded"})


def is_on_air(state: Mapping[str, object]) -> bool:
    if state.get("broadcast_unlocked") is not True or state.get("ingest_status") not in _PUBLISHING:
        return False
    return live_paths is None or state.get("mediamtx_path") in live_paths


def forget_live_paths() -> None:
    global live_paths
    live_paths = None


async def refresh_live_paths() -> None:
    """Read every MediaMTX path once and keep the names of those with a publisher."""
    global live_paths
    started = monotonic()
    try:
        response = await get_http_client().get(
            f"{settings.mediamtx_api_base}/v3/paths/list?itemsPerPage=1000", timeout=PATH_LIST_TIMEOUT
        )
        response.raise_for_status()
        body = response.json()
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list):
            raise ValueError("Invalid MediaMTX path list")
    except (httpx.HTTPError, ValueError):
        live_paths = None
        path_list_outage.failed()
        return
    path_list_outage.recovered()
    newer = set()
    for path, at in list(found_live_at.items()):
        if at > started:
            newer.add(path)
        else:
            del found_live_at[path]
    live_paths = frozenset(
        item["name"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("source")
    ) | newer


def needs_confirm(state: Mapping[str, object]) -> bool:
    """The booth would be on air but the last path list, read before it went live, says no publisher."""
    path = state.get("mediamtx_path")
    return (
        live_paths is not None
        and isinstance(path, str)
        and path not in live_paths
        and state.get("broadcast_unlocked") is True
        and state.get("ingest_status") in _PUBLISHING
    )


async def path_has_source(path: str) -> bool:
    response = await get_http_client().get(
        f"{settings.mediamtx_api_base}/v3/paths/get/{path}", timeout=PATH_GET_TIMEOUT
    )
    if response.status_code == 404:
        return False
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Invalid MediaMTX path")
    return bool(body.get("source"))


async def confirm_live_path(path: str) -> bool:
    """Read one path now so a booth going live is not reported off air until the next tick.

    The booth reports its stream connected as soon as the WHIP answer arrives, before ICE has
    finished and MediaMTX shows the publisher, so the path is read a few times. Returns True when
    it found the publisher and marked the path live.
    """
    global live_paths
    now = monotonic()
    if path in confirming or now - confirmed_at.get(path, -CONFIRM_INTERVAL_SECONDS) < CONFIRM_INTERVAL_SECONDS:
        return False
    for stale in [p for p, at in confirmed_at.items() if now - at >= CONFIRM_INTERVAL_SECONDS]:
        del confirmed_at[stale]
    confirmed_at[path] = now
    confirming.add(path)
    try:
        for attempt in range(CONFIRM_ATTEMPTS):
            if attempt:
                await asyncio.sleep(CONFIRM_RETRY_SECONDS)
            live = await path_has_source(path)
            if live:
                found_live_at[path] = monotonic()
                break
    except (httpx.HTTPError, ValueError):
        path_get_outage.failed()
        return False
    finally:
        confirming.discard(path)
    path_get_outage.recovered()
    if live_paths is None:
        return False
    live_paths = live_paths | {path} if live else live_paths - {path}
    return live
