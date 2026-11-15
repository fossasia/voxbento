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
# Authorized sessions per booth, newest last, keyed by session id. More than one is kept
# because a retry can be authorized and then fail while the earlier session stays live.
_publishers: dict[str, OrderedDict[str, Publisher]] = {}
MAX_SESSIONS_PER_BOOTH = 8


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

    *session_id* is the hook payload's ``id``: the UUID of the WHIP session being
    published. It is required, so every session the portal allows can be ended later.
    """
    try:
        session_id = str(UUID(session_id))
    except ValueError:
        return False
    ticket = redeem_ticket(secret, path)
    if ticket is None or not still_entitled(ticket):
        return False
    _remember(Publisher(ticket.booth_id, ticket.participant_id, ticket.path, session_id))
    return True


def _remember(publisher: Publisher) -> None:
    sessions = _publishers.setdefault(publisher.booth_id, OrderedDict())
    sessions[publisher.session_id] = publisher
    sessions.move_to_end(publisher.session_id)
    while len(sessions) > MAX_SESSIONS_PER_BOOTH:
        sessions.popitem(last=False)


def take_departed_publishers(booth_id: str, state: dict) -> list[Publisher]:
    """Claim the booth's recorded sessions whose participant is no longer in *state*.

    Call it right after a removal, before awaiting anything. It checks the
    participant who opened each session rather than whoever is active now:
    removing the live interpreter can promote a backup who has not gone live yet.
    """
    sessions = _publishers.get(booth_id)
    if not sessions:
        return []
    present = {p.get("participant_id") for p in state.get("participants", ())}
    departed = [publisher for publisher in sessions.values() if publisher.participant_id not in present]
    for publisher in departed:
        del sessions[publisher.session_id]
    if not sessions:
        del _publishers[booth_id]
    return departed


def take_booth_publishers(booth_id: str) -> list[Publisher]:
    """Claim every recorded session of the booth (Mission Control Stop, booth deletion)."""
    return list(_publishers.pop(booth_id, {}).values())


async def _kick_session(path: str, source_type: str, session_id: str) -> None:
    # Imported here, not at the top: program_ingest imports this module (circular import).
    from portal import program_ingest

    await program_ingest.kick(path, program_ingest.MediaSource(type=source_type, id=session_id))


async def end_publishers(publishers: list[Publisher]) -> None:
    """Best effort: kick each recorded WHIP session in MediaMTX.

    Kicking by session id leaves a newer publisher on the same path alone, and it
    works before MediaMTX lists the session as the path's source; a session that
    is already gone counts as ended. A failed kick is recorded again, so the next
    removal in the booth, Stop or deletion retries it.

    The path's current source is ended too when no remaining record owns it: the
    per-booth cap can evict a live session after repeated failed retries.
    """
    for publisher in publishers:
        try:
            await _kick_session(publisher.path, "webRTCSession", publisher.session_id)
        except (httpx.HTTPError, HTTPException, ValueError) as exc:
            logger.warning("Could not end the publisher of booth %s (%s)", publisher.booth_id, type(exc).__name__)
            _remember(publisher)
    if publishers:
        await _end_unowned_source(publishers[0].booth_id, publishers[0].path)


async def _end_unowned_source(booth_id: str, path: str) -> None:
    from portal import program_ingest  # circular import, see _kick_session

    try:
        current = await program_ingest.media_path(path)
        source = current.source if current else None
        if source is not None and source.id not in _publishers.get(booth_id, {}):
            await _kick_session(path, source.type, source.id)
    except (httpx.HTTPError, HTTPException, ValueError) as exc:
        logger.warning("Could not check the source of booth %s (%s)", booth_id, type(exc).__name__)


async def stop_booth_publish(booth_id: str, publishers: list[Publisher], *, only_while_locked: bool = False) -> None:
    """Best effort: end *publishers* and whatever else publishes to the booth's path.

    With *only_while_locked* (Mission Control Stop) the path's current source is
    left alone once the booth is unlocked again: it then belongs to a new publisher.
    """
    from portal import program_ingest  # circular import, see _kick_session

    await end_publishers(publishers)
    known = {publisher.session_id for publisher in publishers}
    try:
        path = booth_id_to_mediamtx_path(booth_id)
        current = await program_ingest.media_path(path)
        source = current.source if current else None
        if source is not None and source.id not in known:
            booth = booths.get_booth_sync(booth_id)
            if only_while_locked and booth is not None and booth.broadcast_unlocked:
                return
            await _kick_session(path, source.type, source.id)
    except (httpx.HTTPError, HTTPException, ValueError) as exc:
        logger.warning("Could not stop the publisher of booth %s (%s)", booth_id, type(exc).__name__)


def clear_tickets() -> None:
    """Forget every outstanding ticket and recorded publisher (tests)."""
    _tickets.clear()
    _publishers.clear()
