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

A two-way booth (#1397) publishes to its own path or to its return language's path,
whichever it outputs into now. The ticket is bound to that path, and the sessions the
booth opened are kept with the path they publish to, so either one can be ended.
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

from portal.booth_identity import booth_id_to_mediamtx_path, make_mediamtx_path, parse_booth_id
from portal.booth_state import Booth
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


def publish_path(booth: Booth) -> str:
    """The path the booth's active interpreter may publish to now, derived on the server only.

    Raises ``ValueError`` for a booth ID that names no event, room and language.
    """
    if booth.outputs_into_return:
        return make_mediamtx_path(booth.event_slug, booth.room_id, booth.output_language_code)
    return booth_id_to_mediamtx_path(booth.booth_id)


def publish_refusal(booth: Booth) -> str | None:
    """Why nobody in *booth* may publish to its path now, or None.

    A two-way booth interpreting into this booth's language holds that channel (#1397).
    """
    if booth.outputs_into_return:
        return None
    owner = booths.return_claim_owner(booth.event_slug, booth.room_id, booth.language_code)
    if owner is not None and owner.booth_id != booth.booth_id:
        return f"The {owner.language} booth is interpreting into {booth.language} right now."
    return None


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
    # The booth may have switched channel since the ticket was issued (#1397).
    try:
        if publish_path(booth) != ticket.path or publish_refusal(booth) is not None:
            return False
    except ValueError:
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


def take_path_publishers(booth_id: str, path: str) -> list[Publisher]:
    """Claim the booth's recorded sessions on *path*, e.g. the channel a two-way booth left."""
    sessions = _publishers.get(booth_id)
    if not sessions:
        return []
    taken = [publisher for publisher in sessions.values() if publisher.path == path]
    for publisher in taken:
        del sessions[publisher.session_id]
    if not sessions:
        del _publishers[booth_id]
    return taken


async def _kick_session(path: str, source_type: str, session_id: str) -> None:
    # Imported here, not at the top: program_ingest imports this module (circular import).
    from portal import program_ingest

    await program_ingest.kick(path, program_ingest.MediaSource(type=source_type, id=session_id))


async def end_publishers(publishers: list[Publisher], *, only_while_locked: bool = False) -> None:
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
    for booth_id, path in dict.fromkeys((publisher.booth_id, publisher.path) for publisher in publishers):
        await _end_unowned_source(booth_id, path, only_while_locked=only_while_locked)


async def _end_unowned_source(
    booth_id: str, path: str, *, only_while_locked: bool = False, known: frozenset[str] | None = None
) -> None:
    """End the source publishing to *path* unless it is a recorded session or another booth owns the path.

    The recorded sessions are *known*, or else the booth's records.

    A two-way booth's return path belongs to whichever booth interprets into it now (#1397).
    """
    from portal import program_ingest  # circular import, see _kick_session

    try:
        current = await program_ingest.media_path(path)
        source = current.source if current else None
        recorded = known if known is not None else _publishers.get(booth_id, {})
        if source is None or source.id in recorded:
            return
        if booths.output_owner_sync(path) not in (None, booth_id):
            return
        booth = booths.get_booth_sync(booth_id)
        if only_while_locked and booth is not None and booth.broadcast_unlocked:
            return
        await _kick_session(path, source.type, source.id)
    except (httpx.HTTPError, HTTPException, ValueError) as exc:
        logger.warning("Could not check the source of booth %s (%s)", booth_id, type(exc).__name__)


async def stop_booth_publish(
    booth_id: str,
    publishers: list[Publisher],
    *,
    only_while_locked: bool = False,
    return_language_code: str | None = None,
) -> None:
    """Best effort: end *publishers* and whatever else publishes to the booth's paths.

    The paths are the booth's own and, for a two-way booth, its return language's: pass
    *return_language_code* when the booth may already be gone from memory. The return
    path's source is left alone while another booth interprets into it.

    With *only_while_locked* (Mission Control Stop) a path's current source is
    left alone once the booth is unlocked again: it then belongs to a new publisher.
    """
    await end_publishers(publishers, only_while_locked=only_while_locked)
    known = frozenset(publisher.session_id for publisher in publishers)
    try:
        event_slug, room_id, language_code = parse_booth_id(booth_id)
    except ValueError as exc:
        logger.warning("Could not stop the publisher of booth %s (%s)", booth_id, type(exc).__name__)
        return
    paths = [make_mediamtx_path(event_slug, room_id, language_code)]
    booth = booths.get_booth_sync(booth_id)
    return_code = return_language_code or (booth.return_language_code if booth is not None else None)
    if return_code:
        paths.append(make_mediamtx_path(event_slug, room_id, return_code))
    for path in paths:
        await _end_unowned_source(booth_id, path, only_while_locked=only_while_locked, known=known)


def clear_tickets() -> None:
    """Forget every outstanding ticket and recorded publisher (tests)."""
    _tickets.clear()
    _publishers.clear()
