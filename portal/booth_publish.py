"""Single-use WHIP publish tickets for interpreter booth paths (#783).

The active interpreter asks for a ticket over its booth WebSocket, whose
participant was assigned by the server at ``booth:join``. The browser sends it
as ``Authorization: Bearer`` on the WHIP offer; MediaMTX forwards it to
``/internal/media-auth``, where it is redeemed once and re-checked against the
live ``BoothRegistry``. Single portal process, like ``BoothRegistry``.

The ticket is only checked when the WHIP session starts, so the MediaMTX
session it opened is recorded per booth. When that interpreter leaves the booth
(revocation, removal, grace expiry) or Mission Control stops it, the portal
kicks that session itself instead of relying on the interpreter's browser (#1285).
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
from fastapi import HTTPException

from portal.booth_identity import booth_id_to_mediamtx_path
from portal.globals import booths
from portal.roles import Permission, has_permission

logger = logging.getLogger(__name__)

TICKET_TTL_SECS = 30.0
MAX_TICKETS = 4096


@dataclass(frozen=True)
class PublishTicket:
    booth_id: str
    participant_id: str
    path: str
    expires_at: float


@dataclass(frozen=True)
class Publisher:
    """The WHIP session a ticket opened in MediaMTX."""

    booth_id: str
    participant_id: str
    path: str
    session_id: str


# Keyed by SHA-256 of the ticket so raw secrets never sit in memory.
_tickets: OrderedDict[str, PublishTicket] = OrderedDict()
# The latest authorized publisher per booth; MediaMTX keeps one source per path.
_publishers: dict[str, Publisher] = {}


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

    *session_id* is the hook payload's ``id``: the UUID of the WHIP session
    MediaMTX is about to add as the path's source.
    """
    ticket = redeem_ticket(secret, path)
    if ticket is None or not still_entitled(ticket):
        return False
    try:
        session_id = str(UUID(session_id))
    except ValueError:
        return True
    _publishers[ticket.booth_id] = Publisher(ticket.booth_id, ticket.participant_id, ticket.path, session_id)
    return True


def take_departed_publisher(booth_id: str, state: dict) -> Publisher | None:
    """Claim the booth's recorded publisher if they are no longer in *state*.

    Call it right after a removal, before awaiting anything. It checks the
    recorded participant rather than whoever is active now: removing the live
    interpreter can promote a backup who has not gone live yet.
    """
    publisher = _publishers.get(booth_id)
    if publisher is None:
        return None
    if any(p.get("participant_id") == publisher.participant_id for p in state.get("participants", ())):
        return None
    return _publishers.pop(booth_id)


async def _kick_session(path: str, source_type: str, session_id: str) -> None:
    # Imported here, not at the top: program_ingest imports this module (circular import).
    from portal import program_ingest

    await program_ingest.kick(path, program_ingest.MediaSource(type=source_type, id=session_id))


async def end_publisher(publisher: Publisher | None) -> None:
    """Best effort: kick *publisher*'s WHIP session in MediaMTX.

    Kicking by session id leaves a newer publisher on the same path alone; a
    session that is already gone counts as ended.
    """
    if publisher is None:
        return
    try:
        await _kick_session(publisher.path, "webRTCSession", publisher.session_id)
    except (httpx.HTTPError, HTTPException, ValueError) as exc:
        logger.warning("Could not end the publisher of booth %s (%s)", publisher.booth_id, type(exc).__name__)


async def stop_booth_publish(booth_id: str) -> None:
    """Best effort: end whatever publishes to the booth's path (Mission Control Stop, booth deletion)."""
    from portal import program_ingest  # circular import, see _kick_session

    publisher = _publishers.pop(booth_id, None)
    await end_publisher(publisher)
    try:
        path = booth_id_to_mediamtx_path(booth_id)
        current = await program_ingest.media_path(path)
        source = current.source if current else None
        if source is not None and (publisher is None or source.id != publisher.session_id):
            await _kick_session(path, source.type, source.id)
    except (httpx.HTTPError, HTTPException, ValueError) as exc:
        logger.warning("Could not stop the publisher of booth %s (%s)", booth_id, type(exc).__name__)


def clear_tickets() -> None:
    """Forget every outstanding ticket and recorded publisher (tests)."""
    _tickets.clear()
    _publishers.clear()
