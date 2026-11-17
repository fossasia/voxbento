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
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from portal.booth_identity import parse_booth_id
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
class Owner:
    """Who a booth WHIP session belongs to, by every identity that can be revoked."""

    participant_id: str
    user_id: int | None
    invite_public_id: str | None


# path -> {mediamtx_session_id: Owner}. A path can briefly hold several authorised offers:
# MediaMTX gives each its session id at creation (before the publish auth hook runs), and
# overridePublisher swaps the live one in. Every authorised offer is recorded, so a pending
# offer that passed the hook just before access ended is still ended, with no timer.
_publishers: dict[str, dict[str, Owner]] = {}
# Bounds a single path, not the whole store, so a busy path cannot evict another path's owner.
MAX_SESSIONS_PER_PATH = 16


def publisher_for(path: str) -> Owner | None:
    """The most recently recorded owner of *path*, if any (diagnostics and tests)."""
    sessions = _publishers.get(path)
    return next(reversed(sessions.values())) if sessions else None


def booth_path(booth_id: str) -> str | None:
    """The MediaMTX path for an interpreter booth, or None for a floor or malformed id."""
    try:
        event_slug, room_id, language_code = parse_booth_id(booth_id)
    except ValueError:
        return None
    if room_id is None or language_code == "floor":
        return None
    return f"{event_slug}/{room_id}/{language_code}"


def _owner_of(ticket: PublishTicket) -> Owner:
    booth = booths.get_booth_sync(ticket.booth_id)
    participant = booth.participants.get(ticket.participant_id) if booth else None
    return Owner(
        participant_id=ticket.participant_id,
        user_id=participant.user_id if participant else None,
        invite_public_id=participant.invite_public_id if participant else None,
    )


def _record_publisher(path: str, session_id: str, owner: Owner) -> None:
    sessions = _publishers.setdefault(path, {})
    sessions.pop(session_id, None)  # re-insert so the newest offer sorts last
    sessions[session_id] = owner
    while len(sessions) > MAX_SESSIONS_PER_PATH:
        del sessions[next(iter(sessions))]


def _forget(path: str, session_id: str) -> None:
    sessions = _publishers.get(path)
    if sessions is not None:
        sessions.pop(session_id, None)
        if not sessions:
            _publishers.pop(path, None)


def _owner_matches(owner: Owner, participant_id: str | None, user_id: int | None, invite_public_id: str | None) -> bool:
    return (
        (participant_id is not None and owner.participant_id == participant_id)
        or (user_id is not None and owner.user_id == user_id)
        or (invite_public_id is not None and owner.invite_public_id == invite_public_id)
    )


async def authorize_booth_publish(path: str, secret: str, session_id: str = "") -> bool:
    """MediaMTX hook check for a non-floor (interpreter booth) path.

    On success the session MediaMTX reports for this offer is remembered against its owner, so
    the portal can end exactly that session (and any other of the owner's offers) if access ends.
    """
    ticket = redeem_ticket(secret, path)
    if ticket is None or not still_entitled(ticket):
        return False
    if session_id:
        _record_publisher(path, session_id, _owner_of(ticket))
    return True


async def _kick_sessions(path: str, session_ids: list[str]) -> None:
    """Kick each session, forgetting it on success; a down or slow MediaMTX never raises here."""
    # program_ingest imports the websocket manager, which imports this module, so the import is local.
    import httpx

    from portal.program_ingest import MediaSource, kick

    for session_id in session_ids:
        try:
            source = MediaSource(type="webRTCSession", id=str(UUID(session_id)))
        except ValueError:
            _forget(path, session_id)  # not a usable session id; nothing to kick
            continue
        try:
            await kick(path, source)
        except (httpx.HTTPError, ValueError):
            logger.warning("Could not end WHIP session on %s", path, exc_info=True)
            continue
        _forget(path, session_id)


async def _live_owned_session(
    path: str,
    *,
    match: Callable[[Owner], bool] | None,
    claim_unrecorded: bool,
) -> str | None:
    """The live source MediaMTX reports for *path*, when it should be ended, else None.

    The record can miss the live session — it is pruned under the per-path cap, or an offer was
    recorded before the live one — so revocation confirms the live publisher directly rather than
    trusting the record alone. *match* decides ownership by the live source's recorded owner; when
    the live source is unrecorded (evicted, or publishing predates a restart) it is claimed only when
    *claim_unrecorded*. *match* is None for a booth that is stopping or going away, which ends whatever
    is live. A down or slow MediaMTX yields None, leaving the recorded sessions to carry the revocation.
    """
    import httpx

    from portal.program_ingest import media_path

    try:
        current = await media_path(path)
    except (httpx.HTTPError, ValueError):
        logger.warning("Could not read the live publisher of %s", path, exc_info=True)
        return None
    source = current.source if current is not None else None
    if source is None or source.type != "webRTCSession":
        return None
    # The await above suspended us, so read the record again: a replacement may have been recorded.
    owner = _publishers.get(path, {}).get(source.id)
    if match is None:
        return source.id
    if owner is not None:
        return source.id if match(owner) else None
    return source.id if claim_unrecorded else None


async def end_booth_publish(
    booth_id: str,
    *,
    participant_id: str | None = None,
    user_id: int | None = None,
    invite_public_id: str | None = None,
    was_active: bool = False,
) -> None:
    """End the WHIP session(s) publishing to a booth's path, so the audio stops whatever the browser does.

    Called when access ends. A browser that is frozen, asleep or busy never runs the code that stops
    its own WHIP session, and until it does listeners keep hearing a person who may no longer speak
    for the booth (#1285).

    With an identity (``participant_id``/``user_id``/``invite_public_id``), the owner's recorded sessions
    are ended, and the live session on the path is ended too when it is theirs — or, when it is
    unrecorded and they were the active interpreter (``was_active``), after a restart or a record
    eviction. A backup who never published, or a replacement recorded during the path lookup, is left
    alone. Without an identity the booth itself is stopping or going away, so every recorded session and
    whatever is live are ended.

    Never raises for an expected failure: a revocation must complete when MediaMTX is down or slow.
    Floor booths are never touched; floor ownership belongs to the Jitsi bot or program ingest.
    """
    path = booth_path(booth_id)
    if path is None:
        return

    sessions = _publishers.get(path, {})
    by_identity = participant_id is not None or user_id is not None or invite_public_id is not None
    if by_identity:
        targets = {sid for sid, owner in sessions.items() if _owner_matches(owner, participant_id, user_id, invite_public_id)}
        match = lambda owner: _owner_matches(owner, participant_id, user_id, invite_public_id)  # noqa: E731
    else:
        targets = set(sessions)
        match = None

    # Always confirm the live publisher: the record can miss it, and then the recorded targets alone
    # would leave the real session running (a cap eviction after a cluster of later offers, #1285 P1).
    live = await _live_owned_session(path, match=match, claim_unrecorded=was_active)
    if live is not None:
        targets.add(live)

    await _kick_sessions(path, list(targets))


async def end_publishers_for(
    *, user_id: int | None = None, invite_public_id: str | None = None, booth_id: str | None = None
) -> None:
    """End every recorded WHIP session owned by a user or invite (#1285).

    Covers a publisher whose participant record is already gone — a drop with no reconnect grace, or
    one expired after the grace — so a later revocation can still find and end their session by identity.
    With *booth_id* only that booth's path is scanned, for a revocation scoped to one booth; without it
    every booth is scanned, for an account deactivated or deleted everywhere.
    """
    if user_id is None and invite_public_id is None:
        return
    if booth_id is not None:
        path = booth_path(booth_id)
        paths = [path] if path is not None else []
    else:
        paths = list(_publishers)
    match = lambda owner: _owner_matches(owner, None, user_id, invite_public_id)  # noqa: E731
    for path in paths:
        targets = {sid for sid, owner in _publishers.get(path, {}).items() if match(owner)}
        if not targets:
            continue  # the owner published nothing here, so there is no live session of theirs to find
        # The owner published here, so an unrecorded live source is their evicted session: claim it.
        live = await _live_owned_session(path, match=match, claim_unrecorded=True)
        if live is not None:
            targets.add(live)
        await _kick_sessions(path, list(targets))


def clear_tickets() -> None:
    """Forget every outstanding ticket and recorded publisher (tests)."""
    _tickets.clear()
    _publishers.clear()
