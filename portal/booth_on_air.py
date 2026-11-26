"""Whether an interpreter booth is on air for listeners (#1397).

A booth is on air while Mission Control has it unlocked and its interpreter reports a
connected stream. The interpreter's browser can report connected after the stream died
(#1357), so the MediaMTX path must also have a publisher. A path with ``alwaysAvailable``
stays ready and keeps counting inbound bytes while empty, but its ``source`` is null
without a publisher, so ``source`` is what is read here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

import httpx

from portal.config import settings
from portal.globals import get_http_client
from portal.outage_log import OutageLog

logger = logging.getLogger(__name__)

PATH_LIST_TIMEOUT = 3.0

# Paths that had a publisher at the last read, or None when that is unknown (no read yet,
# no listeners, or MediaMTX unreachable). Unknown falls back to the interpreter's own report.
live_paths: frozenset[str] | None = None
path_list_outage = OutageLog(logger, "booth_on_air path_list_unavailable")


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
    live_paths = frozenset(
        item["name"]
        for item in items
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("source")
    )
