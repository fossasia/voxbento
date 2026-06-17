from urllib.parse import urlparse

import httpx
import jwt as pyjwt
from fastapi import HTTPException, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials

from portal.auth import decode_token
from portal.config import settings
from portal.globals import booths


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


async def _check_mediamtx() -> bool:
    """Non-blocking reachability check for MediaMTX API endpoint."""
    base = settings.mediamtx_api_base
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{base}/v3/paths/list")
        return r.status_code < 500
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        return False


# Track which channel paths have already been created this process lifetime
# to avoid redundant API calls on every page load.
_created_paths: set[str] = set()


async def _ensure_mediamtx_path(channel_id: str) -> None:
    """Create a named MediaMTX path with alwaysAvailable if it doesn't exist.

    alwaysAvailable keeps the stream alive during publisher handoffs so WHEP
    readers don't get disconnected.  This cannot be set on the wildcard
    all_others path, so we create named paths via the Control API.

    Uses ADD first; if the path already exists, PATCHes it to ensure
    alwaysAvailable is set (handles MediaMTX restarts where the runtime
    config was lost while the portal's in-memory cache was stale).
    """
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
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                f"{api_base}/v3/config/paths/add/{channel_id}",
                json=body,
            )
            if r.status_code == 200:
                _created_paths.add(channel_id)
            elif r.status_code == 400 and "already exists" in r.text.lower():
                # Path exists but may lack alwaysAvailable — patch it
                r2 = await client.patch(
                    f"{api_base}/v3/config/paths/patch/{channel_id}",
                    json=body,
                )
                if r2.status_code == 200:
                    _created_paths.add(channel_id)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        pass  # Non-fatal; path will use all_others defaults


def _require_access(
    credentials: HTTPAuthorizationCredentials | None,
    token_query: str | None = None,
) -> None:
    """Allow request if access token is unset, or if a valid JWT or legacy token is provided."""
    if not settings.booth_access_token:
        return
    if credentials is not None:
        try:
            decode_token(credentials.credentials)
            return
        except pyjwt.InvalidTokenError:
            pass
    if token_query and token_query == settings.booth_access_token:
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
