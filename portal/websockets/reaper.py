from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select

from portal.booth_identity import parse_booth_id
from portal.database import get_session
from portal.models import (
    BoothMembership,
    DBBooth,
    Event,
    EventMembership,
    InviteToken,
    RoomMembership,
    User,
)
from portal.roles import ALL_ROLES
from portal.websockets.manager import (
    REASON_ACCOUNT_DEACTIVATED,
    REASON_INVITE_REVOKED,
    REASON_SESSION_EXPIRED,
    WS_CLOSE_REVOKED,
    manager,
)

logger = logging.getLogger(__name__)


async def check_booth_memberships(booth_id: str, user_ids: set[int]) -> set[int]:
    """Check which user_ids are currently authorized for booth_id using one batched DB query."""
    if not user_ids:
        return set()

    try:
        event_slug, room_id, lang_code = parse_booth_id(booth_id)
    except ValueError:
        return set()

    async with get_session() as db_session:
        stmt = (
            select(DBBooth)
            .join(Event)
            .where(
                Event.slug == event_slug,
                Event.deleted_at.is_(None),
                DBBooth.room_id == room_id,
                DBBooth.language_code == lang_code,
            )
        )
        booth = (await db_session.scalars(stmt)).first()
        if booth is None:
            return set()

        query = select(User.id).where(
            User.id.in_(user_ids),
            User.is_active.is_(True),
            or_(
                User.is_admin.is_(True),
                User.id.in_(
                    select(BoothMembership.user_id).where(
                        BoothMembership.booth_id == booth.id,
                        BoothMembership.role.in_(ALL_ROLES),
                    )
                ),
                User.id.in_(
                    select(RoomMembership.user_id).where(
                        RoomMembership.room_id == booth.room_id,
                        RoomMembership.role.in_(ALL_ROLES),
                    )
                ),
                User.id.in_(
                    select(EventMembership.user_id).where(
                        EventMembership.event_id == booth.event_id,
                        EventMembership.role.in_(ALL_ROLES),
                    )
                ),
            ),
        )
        allowed = set((await db_session.scalars(query)).all())
        return allowed


async def check_booth_tokens(booth_id: str, token_ids: set[str | int]) -> set[str | int]:
    """Check which invite tokens are currently valid for booth_id using one batched DB query."""
    if not token_ids:
        return set()

    try:
        event_slug, room_id, lang_code = parse_booth_id(booth_id)
    except ValueError:
        return set()

    token_strs = [str(t) for t in token_ids]
    async with get_session() as db_session:
        stmt = (
            select(InviteToken.public_id)
            .join(DBBooth, InviteToken.booth_id == DBBooth.id)
            .join(Event, DBBooth.event_id == Event.id)
            .where(
                InviteToken.public_id.in_(token_strs),
                InviteToken.revoked_at.is_(None),
                Event.slug == event_slug,
                Event.deleted_at.is_(None),
                DBBooth.room_id == room_id,
                DBBooth.language_code == lang_code,
            )
        )
        valid = set((await db_session.scalars(stmt)).all())
        return {t for t in token_ids if str(t) in valid}


async def reap_stale_sessions() -> int:
    """Run one pass of the session reaper checking all connected sockets against User rows.

    Returns the number of sockets closed in this pass.
    """
    sessions_snapshot = list(manager._sessions.items())
    if not sessions_snapshot:
        return 0

    user_ids: set[int] = {s.user_id for _, s in sessions_snapshot if s.user_id is not None and not s.revoked}
    token_ids: set[str | int] = {
        s.invite_token_id for _, s in sessions_snapshot if s.invite_token_id is not None and not s.revoked
    }
    if not user_ids and not token_ids:
        return 0

    user_info: dict[int, tuple[bool, datetime | None]] = {}
    token_info: dict[str, datetime | None] = {}

    async with get_session() as db_session:
        if user_ids:
            query = select(User.id, User.is_active, User.sessions_valid_after).where(User.id.in_(user_ids))
            rows = (await db_session.execute(query)).all()
            user_info = {row[0]: (row[1], row[2]) for row in rows}

        if token_ids:
            token_strs = [str(t) for t in token_ids]
            tok_query = select(InviteToken.public_id, InviteToken.revoked_at).where(
                InviteToken.public_id.in_(token_strs)
            )
            tok_rows = (await db_session.execute(tok_query)).all()
            token_info = {row[0]: row[1] for row in tok_rows}

    stale: list[tuple[object, int, str]] = []
    now = datetime.now(timezone.utc)

    for ws_id, session in sessions_snapshot:
        if session.revoked:
            continue
        # Find websocket object
        room = manager._rooms.get(session.booth_id, set())
        ws = next((w for w in room if id(w) == ws_id), None)
        if ws is None:
            continue

        if session.user_id is not None:
            info = user_info.get(session.user_id)
            if info is None:
                # User was deleted
                stale.append((ws, WS_CLOSE_REVOKED, REASON_ACCOUNT_DEACTIVATED))
            else:
                is_active, sessions_valid_after = info
                if not is_active:
                    stale.append((ws, WS_CLOSE_REVOKED, REASON_ACCOUNT_DEACTIVATED))
                elif sessions_valid_after is not None:
                    if sessions_valid_after.tzinfo is None:
                        sessions_valid_after = sessions_valid_after.replace(tzinfo=timezone.utc)
                    issued_at = getattr(session, "issued_at", None) or now
                    if issued_at <= sessions_valid_after:
                        stale.append((ws, WS_CLOSE_REVOKED, REASON_SESSION_EXPIRED))

        elif session.invite_token_id is not None:
            tok_key = str(session.invite_token_id)
            if tok_key not in token_info:
                # Token was deleted
                stale.append((ws, WS_CLOSE_REVOKED, REASON_INVITE_REVOKED))
            else:
                revoked_at = token_info[tok_key]
                if revoked_at is not None:
                    stale.append((ws, WS_CLOSE_REVOKED, REASON_INVITE_REVOKED))

    if not stale:
        return 0

    for ws, code, reason in stale:
        manager.revoke_session(ws)

    tasks = [manager.close_session(ws, code=code, reason=reason) for ws, code, reason in stale]
    await asyncio.gather(*tasks, return_exceptions=True)
    return len(stale)


async def session_reaper_loop(interval_seconds: float = 3.0) -> None:
    """Lightweight periodic reaper task that closes stale sessions across commits."""
    logger.info("Starting WebSocket session reaper loop (interval=%ss)", interval_seconds)
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            await reap_stale_sessions()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Unexpected error in session_reaper_loop; continuing")
