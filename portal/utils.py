from __future__ import annotations

import logging
import time
from urllib.parse import urlparse

import httpx
import jwt as pyjwt
from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials

from portal.auth import decode_token, is_listener_token
from portal.config import settings
from portal.globals import booths, get_http_client
from portal.outage_log import OutageLog

logger = logging.getLogger(__name__)


def safe_redirect(url: str, status_code: int = status.HTTP_303_SEE_OTHER) -> RedirectResponse:
    url = url.replace("\\", "").strip()
    parsed = urlparse(url)
    if url and not parsed.netloc and not parsed.scheme and url.startswith("/"):
        return RedirectResponse(url=url, status_code=status_code)
    return RedirectResponse(url="/", status_code=status_code)


def _make_jitsi_url(base_url: str, room: str) -> str:
    """Return a full Jitsi meeting URL.

    If *room* is already an absolute URL it is returned unchanged, so
    existing deployments that stored a full URL in DEFAULT_JITSI_ROOM
    are not broken by the base-URL prefix.
    """
    if room.startswith(("http://", "https://")):
        return room
    return f"{base_url.rstrip('/')}/{room.lstrip('/')}"


# Cache for MediaMTX health check — avoids redundant HTTP calls on every page
# load. The health check is called from three endpoints (healthz, admin dashboard,
# interpreter status). A short TTL prevents stampeding the Control API while
# still keeping the status reasonably fresh.
_mediamtx_cache: tuple[float, bool] | None = None
_MEDIAMTX_CACHE_TTL = 5.0


async def _check_mediamtx() -> bool:
    """Non-blocking reachability check for MediaMTX API endpoint (cached)."""
    global _mediamtx_cache
    now = time.monotonic()
    if _mediamtx_cache is not None:
        expiry, ok = _mediamtx_cache
        if now < expiry:
            return ok
    base = settings.mediamtx_api_base
    if not base:
        _mediamtx_cache = (now + _MEDIAMTX_CACHE_TTL, False)
        return False
    try:
        client = get_http_client()
        r = await client.get(f"{base}/v3/paths/list", timeout=2.0)
        ok = r.status_code < 500
        _mediamtx_cache = (now + _MEDIAMTX_CACHE_TTL, ok)
        return ok
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError, RuntimeError):
        _mediamtx_cache = (now + _MEDIAMTX_CACHE_TTL, False)
        return False


# Track which channel paths have already been created this process lifetime
# to avoid redundant API calls on every page load.
_created_paths: set[str] = set()

# MediaMTX holds its path configuration in memory, so a restart drops every
# path this process added while the cache above still claims they exist (#729).
# The cache is therefore re-checked against the Control API on a short cycle.
_paths_verified_at: float = 0.0
_PATH_REVALIDATE_TTL = 30.0
# A failed check retries sooner: the cache may be claiming paths that a
# restarting MediaMTX has already lost, and waiting a full cycle keeps streams
# without alwaysAvailable for longer than necessary.
_PATH_REVALIDATE_RETRY_TTL = 5.0
_PATH_LIST_PAGE_SIZE = 1000
_MAX_PATH_LIST_PAGES = 20

_path_list_outage = OutageLog(logger, "mediamtx path revalidation failed")


async def _configured_mediamtx_paths(api_base: str) -> set[str] | None:
    """Names MediaMTX currently has configured, or None if it could not be asked."""
    names: set[str] = set()
    try:
        client = get_http_client()
        for page in range(_MAX_PATH_LIST_PAGES):
            r = await client.get(
                f"{api_base}/v3/config/paths/list",
                params={"page": page, "itemsPerPage": _PATH_LIST_PAGE_SIZE},
                timeout=3.0,
            )
            if r.status_code != 200:
                return None
            payload = r.json()
            if not isinstance(payload, dict):
                return None
            items = payload.get("items") or []
            if not isinstance(items, list):
                return None
            # A malformed entry makes the whole answer untrustworthy: skipping it
            # would under-report the configured set and evict a live path.
            if any(not isinstance(i, dict) or not isinstance(i.get("name"), str) for i in items):
                return None
            names.update(i["name"] for i in items)
            page_count = payload.get("pageCount", 1)
            if not isinstance(page_count, int):
                return None
            if page + 1 >= page_count:
                return names
    except (
        httpx.ConnectError, httpx.TimeoutException, httpx.RequestError,
        RuntimeError, ValueError, KeyError, TypeError, AttributeError,
    ):
        return None
    # The page cap was hit before the last page. Treating a truncated list as
    # authoritative would evict live paths, so report it as unanswerable.
    logger.warning("MediaMTX path list exceeded %d pages; skipping revalidation", _MAX_PATH_LIST_PAGES)
    return None


async def _revalidate_created_paths() -> None:
    """Forget cached paths MediaMTX no longer has, so they are added again.

    One list call reconciles every cached id, keeping this off the per-request
    path. A Control API that cannot answer leaves the cache as it is: the paths
    cannot be recreated while it is down either, and the next cycle retries.
    """
    global _paths_verified_at
    now = time.monotonic()
    if not _created_paths or now < _paths_verified_at + _PATH_REVALIDATE_TTL:
        return

    api_base = settings.mediamtx_api_base
    if not api_base:
        return

    _paths_verified_at = now
    # Snapshot before awaiting: a concurrent _ensure_mediamtx_path may add a
    # path while the list request is in flight, and that path would be absent
    # from the response through no fault of MediaMTX.
    known_paths = set(_created_paths)

    configured = await _configured_mediamtx_paths(api_base)
    if configured is None:
        _path_list_outage.failed()
        _paths_verified_at = now - _PATH_REVALIDATE_TTL + _PATH_REVALIDATE_RETRY_TTL
        return
    _path_list_outage.recovered()

    dropped = known_paths - configured
    if dropped:
        _created_paths.difference_update(dropped)
        # Expected after a MediaMTX restart; the next request re-adds each path.
        logger.info("MediaMTX dropped %d configured path(s); cache invalidated", len(dropped))


async def _ensure_mediamtx_path(channel_id: str) -> None:
    """Create a named MediaMTX path with alwaysAvailable if it doesn't exist.

    alwaysAvailable keeps the stream alive during publisher handoffs so WHEP
    readers don't get disconnected.  This cannot be set on the wildcard
    all_others path, so we create named paths via the Control API.

    Uses ADD first; if the path already exists, PATCHes it to ensure
    alwaysAvailable is set (handles MediaMTX restarts where the runtime
    config was lost while the portal's in-memory cache was stale).
    """
    await _revalidate_created_paths()
    if channel_id in _created_paths:
        return
    api_base = settings.mediamtx_api_base
    if not api_base:
        return
    body = {
        "alwaysAvailable": True,
        "alwaysAvailableTracks": [{"codec": "Opus"}],
        "overridePublisher": True,
    }
    # Floor publishers are selected explicitly, never by a last-publisher-wins race.
    if channel_id.endswith("/floor"):
        body = {"alwaysAvailable": False, "overridePublisher": False}
    try:
        client = get_http_client()
        r = await client.post(
            f"{api_base}/v3/config/paths/add/{channel_id}",
            json=body,
            timeout=3.0,
        )
        if r.status_code == 200:
            _created_paths.add(channel_id)
        elif r.status_code == 400 and "already exists" in r.text.lower():
            # Path exists but may lack alwaysAvailable — patch it
            r2 = await client.patch(
                f"{api_base}/v3/config/paths/patch/{channel_id}",
                json=body,
                timeout=3.0,
            )
            if r2.status_code == 200:
                _created_paths.add(channel_id)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError, RuntimeError):
        pass  # Non-fatal; path will use all_others defaults


def _require_access(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    token_query: str | None = None,
    session_payload: dict | None = None,
) -> None:
    """Allow request with a valid credential, or anonymously in debug mode."""
    if not settings.booth_access_token:
        if not settings.debug:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="BOOTH_ACCESS_TOKEN is not configured on this server.",
            )
        return
    if credentials is not None:
        if credentials.credentials == settings.booth_access_token:
            return
        try:
            payload = decode_token(credentials.credentials)
            if is_listener_token(payload):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="Listener tokens cannot be used for this API."
                )
            return
        except pyjwt.InvalidTokenError:
            pass
    if token_query and token_query == settings.booth_access_token:
        return

    # Cookie Fallback
    from portal.auth import get_booth_session

    payload = session_payload if session_payload is not None else get_booth_session(request)
    if payload:
        if is_listener_token(payload):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Listener tokens cannot be used for this API."
            )
        return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing auth token.")


async def _resolve_whip_url(booth_id: str, participant_id: str, language: str, channel_id: str) -> dict:
    """Check publish permission and return the WHIP URL payload.

    Used by the event-scoped ``/api/events/{slug}/booths/{lang}/whip-url`` endpoint.
    Raises :class:`HTTPException` on permission/lookup failures.
    """
    try:
        await booths.check_publish_permission(booth_id, participant_id, language, channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    await _ensure_mediamtx_path(channel_id)
    whip_url = f"{settings.mediamtx_whip_base}/{channel_id}/whip"
    return {"whip_url": whip_url, "channel_id": channel_id, "booth_id": booth_id}


def verify_same_origin(request: Request) -> bool:
    """Validate that the request Origin or Referer matches the request Host header.

    Protects state-changing POST endpoints against CSRF.
    """
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    host = request.headers.get("host")

    target_url = origin or referer
    if not target_url:
        return False

    parsed = urlparse(target_url)
    if not parsed.netloc:
        return False

    return parsed.netloc.lower() == (host or "").lower()


def get_client_ip(request: Request) -> str:
    """Extract client IP address from headers or connection."""
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
