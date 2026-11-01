"""Single-use WHIP publish tickets for interpreter booth paths (#783).

The active interpreter asks for a ticket over its booth WebSocket, whose
participant was assigned by the server at ``booth:join``. The browser sends it
as ``Authorization: Bearer`` on the WHIP offer; MediaMTX forwards it to
``/internal/media-auth``, where it is redeemed once and re-checked against the
live ``BoothRegistry``. Single portal process, like ``BoothRegistry``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

import httpx

from portal.config import settings
from portal.globals import booths, get_http_client
from portal.roles import Permission, has_permission

logger = logging.getLogger(__name__)

TICKET_TTL_SECS = 30.0
MAX_TICKETS = 4096
MEDIA_CONTROL_TIMEOUT = 2.0


@dataclass(frozen=True)
class PublishTicket:
    booth_id: str
    participant_id: str
    path: str
    expires_at: float


# Keyed by SHA-256 of the ticket so raw secrets never sit in memory.
_tickets: OrderedDict[str, PublishTicket] = OrderedDict()


def _key(secret: str) -> str:
    """Return the store key for *secret*."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _prune(now: float) -> None:
    """Drop expired tickets, then the oldest ones while the store is full."""
    while _tickets:
        oldest = next(iter(_tickets.values()))
        if oldest.expires_at > now and len(_tickets) < MAX_TICKETS:
            break
        _tickets.popitem(last=False)


def issue_ticket(booth_id: str, participant_id: str, path: str) -> str:
    """Mint a ticket that lets *participant_id* publish to *path* once."""
    now = time.monotonic()
    _prune(now)
    secret = secrets.token_urlsafe(32)
    _tickets[_key(secret)] = PublishTicket(booth_id, participant_id, path, now + TICKET_TTL_SECS)
    return secret


def redeem_ticket(secret: str, path: str) -> PublishTicket | None:
    """Consume *secret*; return its ticket only if it is live and minted for *path*."""
    if not secret:
        return None
    # Popped before any check so a ticket is spent even when presented for the wrong path.
    ticket = _tickets.pop(_key(secret), None)
    if ticket is None or ticket.expires_at <= time.monotonic():
        return None
    if not secrets.compare_digest(ticket.path, path):
        return None
    return ticket


def still_entitled(ticket: PublishTicket) -> bool:
    """Decide whether the ticket holder may still publish at the moment MediaMTX asks.

    A ticket can sit in the browser for up to TICKET_TTL_SECS while ICE gathers,
    and the booth can change underneath it (handoff, kick, Mission Control stop).
    """
    booth = booths.get_booth_sync(ticket.booth_id)
    if booth is None or not booth.broadcast_unlocked:
        return False
    participant = booth.participants.get(ticket.participant_id)
    return bool(
        participant is not None
        and booth.active_interpreter_id == participant.participant_id
        and has_permission(participant.role, Permission.BOOTH_GO_LIVE)
    )


async def authorize_booth_publish(path: str, secret: str, session_id: str = "") -> bool:
    """MediaMTX hook check for a non-floor (interpreter booth) path.

    ``session_id`` is MediaMTX's id for the WebRTC session being authorized. On
    success it is remembered against the publisher so the stream can be kicked
    if that participant drops without leaving cleanly.
    """
    ticket = redeem_ticket(secret, path)
    if ticket is None or not still_entitled(ticket):
        return False
    if session_id:
        _publishers[ticket.booth_id] = (ticket.participant_id, session_id)
    return True


# booth_id -> (participant_id, MediaMTX WebRTC session id) of the live WHIP publisher.
# Single portal process, like ``_tickets`` and ``BoothRegistry``.
_publishers: dict[str, tuple[str, str]] = {}


async def evict_publisher(booth_id: str, participant_id: str | None = None) -> bool:
    """Kick the booth's recorded WHIP publisher so its path is free straight away.

    Without this, a publisher that vanished (tab closed, Wi-Fi drop, sleep) keeps
    the MediaMTX path occupied until MediaMTX times it out, and the interpreter's
    next publish is refused as a duplicate.

    With ``participant_id``, only that participant's own stream is kicked. A
    socket that is detected as dead after its owner already reconnected and
    republished must not take down the new stream.

    Best effort: returns True only when MediaMTX confirmed the kick. A failed
    kick keeps the record so the next call retries it.
    """
    record = _publishers.get(booth_id)
    if record is None:
        return False
    owner, session_id = record
    if participant_id is not None and owner != participant_id:
        return False
    try:
        session = str(UUID(session_id))
        response = await get_http_client().post(
            f"{settings.mediamtx_api_base}/v3/webrtcsessions/kick/{session}", timeout=MEDIA_CONTROL_TIMEOUT
        )
    except (httpx.HTTPError, ValueError):
        logger.warning("Could not kick stale WHIP publisher for booth %s", booth_id, exc_info=True)
        return False
    if response.status_code == 404:
        # Already gone, which is what we wanted.
        _publishers.pop(booth_id, None)
        return False
    if response.is_error:
        logger.warning("MediaMTX refused to kick WHIP publisher for booth %s: %s", booth_id, response.status_code)
        return False
    _publishers.pop(booth_id, None)
    return True


def clear_tickets() -> None:
    """Forget every outstanding ticket and recorded publisher (tests)."""
    _tickets.clear()
    _publishers.clear()
