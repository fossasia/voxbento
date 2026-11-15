"""Single-use WHIP publish tickets for interpreter booth paths (#783).

The active interpreter asks for a ticket over its booth WebSocket, whose
participant was assigned by the server at ``booth:join``. The browser sends it
as ``Authorization: Bearer`` on the WHIP offer; MediaMTX forwards it to
``/internal/media-auth``, where it is redeemed once and re-checked against the
live ``BoothRegistry``. Single portal process, like ``BoothRegistry``.

The session MediaMTX reports for an authorised offer is remembered as the path's
publisher, so ``end_booth_publish`` can end exactly that session when access ends (#1285).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from uuid import UUID

from portal.booth_identity import parse_booth_id
from portal.globals import booths
from portal.roles import Permission, has_permission

logger = logging.getLogger(__name__)

TICKET_TTL_SECS = 30.0
MAX_TICKETS = 4096
MAX_PUBLISHERS = 4096


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


@dataclass(frozen=True)
class Publisher:
    """Who started the WHIP session now on a booth path, as MediaMTX named it."""

    participant_id: str
    session_id: str


# One entry per booth path: a later publisher replaces the earlier one, as it does in MediaMTX.
_publishers: OrderedDict[str, Publisher] = OrderedDict()


def publisher_for(path: str) -> Publisher | None:
    """The recorded publisher of *path*, if any."""
    return _publishers.get(path)


def _record_publisher(path: str, participant_id: str, session_id: str) -> None:
    _publishers[path] = Publisher(participant_id, session_id)
    _publishers.move_to_end(path)
    while len(_publishers) > MAX_PUBLISHERS:
        _publishers.popitem(last=False)


async def authorize_booth_publish(path: str, secret: str, session_id: str = "") -> bool:
    """MediaMTX hook check for a non-floor (interpreter booth) path.

    On success the session MediaMTX reports for this offer is remembered as the path's
    publisher, so the portal can end exactly that session if the holder's access ends.
    """
    ticket = redeem_ticket(secret, path)
    if ticket is None or not still_entitled(ticket):
        return False
    _record_publisher(path, ticket.participant_id, session_id)
    return True


async def end_booth_publish(booth_id: str, *, participant_id: str | None = None, was_active: bool = False) -> None:
    """End the WHIP session publishing to a booth's path, so the audio stops whatever the browser does.

    Called when access ends. A browser that is frozen, asleep or busy never runs the code that stops
    its own WHIP session, and until it does listeners keep hearing a person who may no longer speak
    for the booth (#1285).

    With *participant_id*, only that person's session is ended: the recorded publisher when it is them,
    or, when the record is empty (the portal restarted) and they were *was_active*, whatever session is
    on the path. A backup who never published, or someone else's session, is left alone. Without it the
    booth itself is going away or stopping, so whatever is publishing is ended.

    Never raises: a revocation must complete when MediaMTX is down or slow. Floor booths are never
    touched; floor ownership belongs to the Jitsi bot or program ingest, not to a booth revocation.
    """
    try:
        event_slug, room_id, language_code = parse_booth_id(booth_id)
    except ValueError:
        return
    if room_id is None or language_code == "floor":
        return
    path = f"{event_slug}/{room_id}/{language_code}"

    publisher = _publishers.get(path)
    if participant_id is None:
        target = publisher
    elif publisher is not None and publisher.participant_id == participant_id:
        target = publisher
    elif publisher is None and was_active:
        target = None
    else:
        return

    # program_ingest imports the websocket manager, which imports this module, so this cannot be
    # a module-level import.
    from portal.program_ingest import MediaSource, kick, media_path

    try:
        session = None
        if target is not None:
            try:
                session = MediaSource(type="webRTCSession", id=str(UUID(target.session_id)))
            except ValueError:
                session = None  # MediaMTX sent no usable session ID: use the path's own source
        if session is None:
            current = await media_path(path)
            if current is None or current.source is None:
                return
            if current.source.type != "webRTCSession":
                logger.warning("Not ending %s: its publisher is a %s, not a booth WHIP session", path, current.source.type)
                return
            session = current.source
        await kick(path, session)
    except Exception:
        logger.warning("Could not end the WHIP session on %s", path, exc_info=True)
        return
    if _publishers.get(path) == target:
        _publishers.pop(path, None)


def clear_tickets() -> None:
    """Forget every outstanding ticket and recorded publisher (tests)."""
    _tickets.clear()
    _publishers.clear()
