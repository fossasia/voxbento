"""Google OAuth and YouTube Live Streaming API client for "Sign in with YouTube" (#1087).

Reads broadcast metadata and the organiser's own ingest settings. Never fetches,
stores or embeds YouTube audio/video.

The refresh token is Fernet-encrypted in ``youtube_connections`` and never returned.
Access tokens live only in process memory (one portal replica, like ``BoothRegistry``).
Tokens, stream keys, caption URLs, authorization codes and response bodies are never
logged; log lines carry ids and an action name only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from portal import program_relay
from portal.config import settings
from portal.crypto import decrypt_val
from portal.globals import get_http_client
from portal.models import Room, YouTubeConnection

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # nosec B105 - an OAuth endpoint URL, not a secret
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TIMEOUT = 10.0
# A cached access token is refreshed once it has less than this many seconds left.
TOKEN_MARGIN_SECS = 60.0
BROADCAST_PARTS = "snippet,contentDetails,status"
# liveBroadcasts.list returns at most 50 per page; a few pages covers any real channel at bounded quota.
MAX_BROADCAST_PAGES = 4
WATCH_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
REAUTH_MESSAGE = "YouTube access was revoked or expired. Sign in with YouTube again."
_REASON = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def parse_broadcast_id(text: str) -> str | None:
    """Return the 11-character video id from a pasted YouTube link or bare id, else None.

    Accepts only https links on youtube.com (watch and live pages), youtu.be and
    YouTube Studio's live-streaming page.
    """
    text = (text or "").strip()
    if VIDEO_ID.fullmatch(text):
        return text
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or port is not None or parts.username or parts.password:
        return None
    host = (parts.hostname or "").casefold()
    segments = [segment for segment in parts.path.split("/") if segment]
    candidate = ""
    if host in WATCH_HOSTS:
        if segments == ["watch"]:
            candidate = parse_qs(parts.query).get("v", [""])[0]
        elif len(segments) == 2 and segments[0] == "live":
            candidate = segments[1]
    elif host == "youtu.be" and len(segments) == 1:
        candidate = segments[0]
    elif (
        host == "studio.youtube.com"
        and len(segments) == 3
        and segments[0] == "video"
        and segments[2] == "livestreaming"
    ):
        candidate = segments[1]
    return candidate if VIDEO_ID.fullmatch(candidate) else None


def pkce_pair() -> tuple[str, str]:
    """Return a PKCE (verifier, S256 challenge) pair."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(state: str, challenge: str) -> str:
    query = urlencode(
        {
            "client_id": settings.youtube_oauth_client_id,
            "redirect_uri": settings.youtube_redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTH_URL}?{query}"


@dataclass
class Broadcast:
    id: str
    title: str
    scheduled_start: str | None
    life_cycle_status: str
    bound_stream_id: str | None
    latency_preference: str | None
    closed_captions_type: str | None

    def summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "scheduled_start": self.scheduled_start,
            "life_cycle_status": self.life_cycle_status,
            "latency_preference": self.latency_preference,
        }


@dataclass
class StreamInfo:
    id: str
    ingestion_address: str
    stream_status: str | None
    health_status: str | None
    # The YouTube stream key and caption ingestion URL: secrets, kept out of repr and logs.
    stream_name: str = field(repr=False)
    captions_url: str | None = field(repr=False, default=None)


class YouTubeAuthError(Exception):
    """The connection needs a fresh "Sign in with YouTube"."""


class YouTubeAPIError(Exception):
    """Google answered with an error. Carries the status and Google's short reason code only."""

    def __init__(self, status: int, reason: str = "") -> None:
        self.status = status
        self.reason = reason
        super().__init__(f"YouTube API error {status}" + (f": {reason}" if reason else ""))


# Google's reason codes the organiser can act on, in their words.
API_ERROR_MESSAGES = {
    "liveStreamingNotEnabled": (
        "This YouTube channel is not enabled for live streaming yet. In YouTube Studio choose "
        "Create, then Go live; the first activation can take up to 24 hours."
    ),
    "insufficientPermissions": "VoxBento was not given access to your broadcasts. Sign in with YouTube again.",
    "quotaExceeded": "The server's YouTube API quota is used up for today. Try again tomorrow.",
    "rateLimitExceeded": "YouTube is limiting requests right now. Try again in a minute.",
    "unreachable": "YouTube could not be reached. Try again.",
}


def describe(exc: YouTubeAPIError | YouTubeAuthError) -> str:
    """An organiser-facing message; built from the status and Google's reason code only."""
    if isinstance(exc, YouTubeAuthError):
        return str(exc)
    if exc.reason in API_ERROR_MESSAGES:
        return API_ERROR_MESSAGES[exc.reason]
    detail = f"{exc.status}, {exc.reason}" if exc.reason else str(exc.status)
    return f"YouTube refused the request ({detail}). Try again."


def _reason(response: httpx.Response) -> str:
    """Google's ``error.errors[0].reason`` code, never the raw body."""
    try:
        reason = response.json()["error"]["errors"][0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return reason if isinstance(reason, str) and _REASON.fullmatch(reason) else ""


def _oauth_error(response: httpx.Response) -> str:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return ""
    return error if isinstance(error, str) else ""


async def _send(method: str, url: str, **kwargs) -> httpx.Response:
    try:
        return await get_http_client().request(method, url, timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError:
        # The exception text can carry the request URL; report the failure kind only.
        raise YouTubeAPIError(502, "unreachable") from None


# event_id -> (access token, monotonic expiry, grant tag). The tag ties a cached token to the stored
# refresh token, so a refresh still in flight for a replaced connection can never serve the new one.
_tokens: dict[int, tuple[str, float, str]] = {}
# room_id -> (monotonic time checked, status dict)
_status: dict[int, tuple[float, dict]] = {}


def forget_event(event_id: int, room_ids: list[int] | set[int] = ()) -> None:
    """Drop the cached access token and room statuses, e.g. after (re)connecting or disconnecting."""
    _tokens.pop(event_id, None)
    for room_id in room_ids:
        _status.pop(room_id, None)


def forget_room(room_id: int) -> None:
    _status.pop(room_id, None)


# Connection changes are serialized: per event (linking, reconnecting and disconnecting), and across
# events for the shared-channel revocation check, so concurrent requests cannot interleave.
_event_locks: dict[int, asyncio.Lock] = {}
connections_lock = asyncio.Lock()


def event_lock(event_id: int) -> asyncio.Lock:
    return _event_locks.setdefault(event_id, asyncio.Lock())


def _grant_tag(connection: YouTubeConnection) -> str:
    return hashlib.sha256((connection.refresh_token_enc or "").encode()).hexdigest()[:16]


async def get_connection(session: AsyncSession, event_id: int) -> YouTubeConnection | None:
    result = await session.execute(select(YouTubeConnection).where(YouTubeConnection.event_id == event_id))
    return result.scalar_one_or_none()


async def channel_shared(session: AsyncSession, connection: YouTubeConnection) -> bool:
    """True when another event is connected to the same channel.

    Google revokes the whole grant, so revoking this event's token would also cut off the other event.
    """
    result = await session.execute(
        select(YouTubeConnection.id).where(
            YouTubeConnection.channel_id == connection.channel_id, YouTubeConnection.id != connection.id
        )
    )
    return result.first() is not None


async def room_ids_for_event(session: AsyncSession, event_id: int) -> list[int]:
    result = await session.execute(select(Room.id).where(Room.event_id == event_id))
    return list(result.scalars())


async def unlink_event_rooms(session: AsyncSession, event_id: int) -> list[int]:
    """Clear the linked broadcast on every room of the event; returns the event's room ids."""
    await session.execute(
        update(Room).where(Room.event_id == event_id).values(youtube_broadcast_id=None, youtube_stream_id=None)
    )
    return await room_ids_for_event(session, event_id)


async def clear_channel_links(session: AsyncSession, event_id: int) -> list[int]:
    """Forget every room's broadcast after the event switched to another channel.

    A linked room's relay destination was filled in from the old channel's stream, so it is
    cleared too; the owner picks a broadcast on the new channel. Returns the event's room ids.
    """
    linked = await session.scalars(select(Room).where(Room.event_id == event_id, Room.youtube_stream_id.is_not(None)))
    for room in linked:
        if room.program_relay_server or room.program_relay_key_enc:
            program_relay.clear_destination(room)
    return await unlink_event_rooms(session, event_id)


def _needs_sign_in(connection: YouTubeConnection) -> YouTubeAuthError:
    _tokens.pop(connection.event_id, None)
    connection.last_error = REAUTH_MESSAGE
    return YouTubeAuthError(REAUTH_MESSAGE)


async def access_token(connection: YouTubeConnection) -> str:
    """Return a valid access token for the connection, refreshing it when it is about to expire.

    On ``invalid_grant`` sets ``connection.last_error`` and raises ``YouTubeAuthError``; the
    caller commits the session so the room page can show it.
    """
    grant = _grant_tag(connection)
    cached = _tokens.get(connection.event_id)
    now = time.monotonic()
    if cached and cached[2] == grant and cached[1] - now > TOKEN_MARGIN_SECS:
        return cached[0]
    try:
        refresh_token = decrypt_val(connection.refresh_token_enc)
    except ValueError:
        # A rotated API_KEY_ENCRYPTION_KEY or a damaged row: a fresh sign-in overwrites it.
        logger.warning("youtube event_id=%s action=refresh_unreadable", connection.event_id)
        raise _needs_sign_in(connection) from None
    response = await _send(
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.youtube_oauth_client_id,
            "client_secret": settings.youtube_oauth_client_secret,
        },
    )
    if response.status_code == 400 and _oauth_error(response) == "invalid_grant":
        logger.info("youtube event_id=%s action=refresh_revoked", connection.event_id)
        raise _needs_sign_in(connection)
    if response.status_code != 200:
        raise YouTubeAPIError(response.status_code, _oauth_error(response))
    try:
        body = response.json()
        token = body["access_token"]
        expires_in = float(body.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError):
        raise YouTubeAPIError(502, "malformed") from None
    _tokens[connection.event_id] = (token, now + expires_in, grant)
    if connection.last_error:
        connection.last_error = None
    return token


async def exchange_code(code: str, verifier: str) -> dict:
    """Exchange an authorization code (with its PKCE verifier) for tokens.

    Returns ``refresh_token``, ``access_token``, ``expires_in`` and ``scope``.
    """
    response = await _send(
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "client_id": settings.youtube_oauth_client_id,
            "client_secret": settings.youtube_oauth_client_secret,
            "redirect_uri": settings.youtube_redirect_uri,
        },
    )
    if response.status_code == 400 and _oauth_error(response) == "invalid_grant":
        raise YouTubeAuthError("The YouTube sign-in expired. Try again.")
    if response.status_code != 200:
        raise YouTubeAPIError(response.status_code, _oauth_error(response))
    try:
        body = response.json()
        tokens = {
            "refresh_token": body.get("refresh_token") or "",
            "access_token": body["access_token"],
            "expires_in": float(body.get("expires_in", 3600)),
            "scope": str(body.get("scope", "")),
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        raise YouTubeAPIError(502, "malformed") from None
    if not tokens["refresh_token"]:
        raise YouTubeAuthError("Google did not grant offline access. Sign in with YouTube again and allow access.")
    if SCOPE not in tokens["scope"].split():
        raise YouTubeAuthError("Allow VoxBento to manage your YouTube account on Google's consent screen.")
    return tokens


async def _api_get(token: str, resource: str, params: dict) -> dict:
    response = await _send("GET", f"{API_BASE}/{resource}", params=params, headers={"Authorization": f"Bearer {token}"})
    if response.status_code == 401:
        raise YouTubeAuthError(REAUTH_MESSAGE)
    if response.status_code != 200:
        raise YouTubeAPIError(response.status_code, _reason(response))
    try:
        body = response.json()
    except ValueError:
        raise YouTubeAPIError(502, "malformed") from None
    return body if isinstance(body, dict) else {}


async def _connection_get(connection: YouTubeConnection, resource: str, params: dict) -> dict:
    try:
        return await _api_get(await access_token(connection), resource, params)
    except YouTubeAuthError:
        # A rejected access token: drop it so the next call refreshes instead of reusing it.
        _tokens.pop(connection.event_id, None)
        raise


async def my_channel(token: str) -> tuple[str, str] | None:
    """Return (channel id, title) of the signed-in account, or None when it has no channel."""
    body = await _api_get(token, "channels", {"part": "snippet", "mine": "true"})
    items = body.get("items") or []
    if not items:
        return None
    return str(items[0]["id"]), str(items[0].get("snippet", {}).get("title", ""))[:200]


def _broadcast(item: dict) -> Broadcast:
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    status = item.get("status") or {}
    return Broadcast(
        id=str(item["id"]),
        title=str(snippet.get("title", ""))[:200],
        scheduled_start=snippet.get("scheduledStartTime"),
        life_cycle_status=str(status.get("lifeCycleStatus", "")),
        bound_stream_id=details.get("boundStreamId") or None,
        latency_preference=details.get("latencyPreference"),
        closed_captions_type=details.get("closedCaptionsType"),
    )


async def list_broadcasts(connection: YouTubeConnection) -> list[Broadcast]:
    """The channel's active and upcoming broadcasts, live first, then by scheduled start."""
    found: dict[str, Broadcast] = {}
    # ``broadcastStatus`` and ``mine`` are incompatible filters; ``broadcastStatus`` implies the
    # authorised channel.
    for broadcast_status in ("active", "upcoming"):
        page_token = None
        for _ in range(MAX_BROADCAST_PAGES):
            params = {
                "part": BROADCAST_PARTS,
                "broadcastStatus": broadcast_status,
                "broadcastType": "all",
                "maxResults": 50,
            }
            if page_token:
                params["pageToken"] = page_token
            body = await _connection_get(connection, "liveBroadcasts", params)
            for item in body.get("items") or []:
                broadcast = _broadcast(item)
                found.setdefault(broadcast.id, broadcast)
            page_token = body.get("nextPageToken")
            if not page_token:
                break
    return sorted(found.values(), key=lambda b: (b.life_cycle_status != "live", b.scheduled_start or ""))


async def get_broadcast(connection: YouTubeConnection, broadcast_id: str) -> Broadcast | None:
    """The broadcast, or None when it is not on the authorised channel."""
    body = await _connection_get(connection, "liveBroadcasts", {"part": BROADCAST_PARTS, "id": broadcast_id})
    items = body.get("items") or []
    return _broadcast(items[0]) if items else None


async def get_stream(connection: YouTubeConnection, stream_id: str) -> StreamInfo | None:
    body = await _connection_get(connection, "liveStreams", {"part": "cdn,status,contentDetails", "id": stream_id})
    items = body.get("items") or []
    if not items:
        return None
    item = items[0]
    ingestion = (item.get("cdn") or {}).get("ingestionInfo") or {}
    status = item.get("status") or {}
    return StreamInfo(
        id=str(item["id"]),
        ingestion_address=str(ingestion.get("ingestionAddress") or ""),
        stream_status=status.get("streamStatus"),
        health_status=(status.get("healthStatus") or {}).get("status"),
        stream_name=str(ingestion.get("streamName") or ""),
        captions_url=(item.get("contentDetails") or {}).get("closedCaptionsIngestionUrl"),
    )


async def revoke(refresh_token: str | None) -> None:
    """Best-effort revocation at Google; network failures are ignored."""
    if not refresh_token:
        return
    try:
        # The token goes in the form body (RFC 7009), never the URL, so no request log can carry it.
        response = await get_http_client().post(REVOKE_URL, data={"token": refresh_token}, timeout=TIMEOUT)
    except httpx.HTTPError:
        logger.warning("youtube action=revoke_failed reason=unreachable")
        return
    if response.status_code != 200:
        logger.warning("youtube action=revoke_failed status=%s", response.status_code)


async def link_broadcast(session: AsyncSession, room: Room, connection: YouTubeConnection, text: str) -> Broadcast:
    """Link the room to a broadcast on the connected channel.

    In relay mode the bound stream's ingest address and key become the relay destination.
    All YouTube lookups happen before the room changes, so a rejected link leaves it untouched.
    Raises ValueError with an organiser-facing message on bad input.
    """
    broadcast_id = parse_broadcast_id(text)
    if broadcast_id is None:
        raise ValueError("Paste a YouTube broadcast link, or choose a broadcast from the list.")
    broadcast = await get_broadcast(connection, broadcast_id)
    if broadcast is None:
        raise ValueError("That broadcast is not on the connected channel.")
    if not broadcast.bound_stream_id:
        raise ValueError(
            "That broadcast does not have a stream bound in YouTube Studio yet. Bind a stream in Studio and try again."
        )
    stream = await get_stream(connection, broadcast.bound_stream_id)
    if stream is None:
        raise ValueError("YouTube did not return the stream bound to that broadcast. Try again in a moment.")
    if room.program_ingest_mode == program_relay.RELAY_MODE:
        if not stream.ingestion_address or not stream.stream_name:
            raise ValueError("YouTube did not return the stream key for that broadcast. Paste it manually instead.")
        program_relay.store_destination(room, stream.ingestion_address, stream.stream_name)
    room.youtube_broadcast_id = broadcast.id
    room.youtube_stream_id = broadcast.bound_stream_id
    _status.pop(room.id, None)
    logger.info(
        "youtube event_id=%s room_id=%s action=broadcast_linked destination=%s",
        connection.event_id,
        room.id,
        room.program_ingest_mode == program_relay.RELAY_MODE,
    )
    return broadcast


async def room_status(connection: YouTubeConnection, room: Room) -> dict:
    """YouTube-side status of the room's broadcast, cached per room to save API quota."""
    cached = _status.get(room.id)
    now = time.monotonic()
    if (
        cached
        and cached[1]["broadcast_id"] == room.youtube_broadcast_id
        and now - cached[0] < settings.youtube_status_cache_secs
    ):
        return cached[1]
    broadcast = await get_broadcast(connection, room.youtube_broadcast_id) if room.youtube_broadcast_id else None
    stream_id = (broadcast.bound_stream_id if broadcast else None) or room.youtube_stream_id
    stream = await get_stream(connection, stream_id) if broadcast and stream_id else None
    status = {
        "broadcast_id": room.youtube_broadcast_id,
        "title": broadcast.title if broadcast else None,
        # "missing": the broadcast was deleted or is no longer on the connected channel.
        "life_cycle_status": broadcast.life_cycle_status if broadcast else "missing",
        "stream_status": stream.stream_status if stream else None,
        "health": stream.health_status if stream else None,
        "latency_preference": broadcast.latency_preference if broadcast else None,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    _status[room.id] = (now, status)
    return status
