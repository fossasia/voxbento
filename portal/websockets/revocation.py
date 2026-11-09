from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from portal.booth_identity import make_booth_id
from portal.models import DBBooth
from portal.websockets.manager import REASON_MEMBERSHIP_REMOVED, manager

logger = logging.getLogger(__name__)


async def revoke_membership_push(
    session: AsyncSession,
    *,
    user_id: int,
    level: str,
    target_id: int,
) -> None:
    """Push-revoke active WebSocket sessions for *user_id* in all booths affected by *level* and *target_id*."""
    affected_booth_ids: list[str] = []

    if level == "booth":
        stmt = select(DBBooth).options(joinedload(DBBooth.event)).where(DBBooth.id == target_id)
        booth = (await session.scalars(stmt)).first()
        if booth is not None and booth.event is not None:
            bid = make_booth_id(booth.event.slug, booth.room_id, booth.language_code)
            affected_booth_ids.append(bid)
    elif level == "room":
        stmt = select(DBBooth).options(joinedload(DBBooth.event)).where(DBBooth.room_id == target_id)
        booths = (await session.scalars(stmt)).all()
        for b in booths:
            if b.event is not None:
                bid = make_booth_id(b.event.slug, b.room_id, b.language_code)
                affected_booth_ids.append(bid)
    elif level == "event":
        stmt = select(DBBooth).options(joinedload(DBBooth.event)).where(DBBooth.event_id == target_id)
        booths = (await session.scalars(stmt)).all()
        for b in booths:
            if b.event is not None:
                bid = make_booth_id(b.event.slug, b.room_id, b.language_code)
                affected_booth_ids.append(bid)

    for booth_id in affected_booth_ids:
        try:
            await manager.close_user_sessions(user_id, booth_id=booth_id, reason=REASON_MEMBERSHIP_REMOVED)
        except Exception:
            logger.exception("Failed to close user %d sessions for booth %s", user_id, booth_id)


async def revoke_booths_for_deleted_entity(
    session: AsyncSession,
    *,
    level: str,
    target_id: int,
) -> None:
    """Close all sessions in booths affected by the deletion of an event, room, or booth."""
    affected_booth_ids: list[str] = []

    if level == "booth":
        stmt = select(DBBooth).options(joinedload(DBBooth.event)).where(DBBooth.id == target_id)
        booth = (await session.scalars(stmt)).first()
        if booth is not None and booth.event is not None:
            bid = make_booth_id(booth.event.slug, booth.room_id, booth.language_code)
            affected_booth_ids.append(bid)
    elif level == "room":
        stmt = select(DBBooth).options(joinedload(DBBooth.event)).where(DBBooth.room_id == target_id)
        booths = (await session.scalars(stmt)).all()
        for b in booths:
            if b.event is not None:
                bid = make_booth_id(b.event.slug, b.room_id, b.language_code)
                affected_booth_ids.append(bid)
    elif level == "event":
        stmt = select(DBBooth).options(joinedload(DBBooth.event)).where(DBBooth.event_id == target_id)
        booths = (await session.scalars(stmt)).all()
        for b in booths:
            if b.event is not None:
                bid = make_booth_id(b.event.slug, b.room_id, b.language_code)
                affected_booth_ids.append(bid)

    for booth_id in affected_booth_ids:
        try:
            await manager.close_booth_sessions(booth_id)
        except Exception:
            logger.exception("Failed to close booth %s sessions on %s deletion", booth_id, level)
