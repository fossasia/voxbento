"""Async database engine, session factory, and CRUD helpers.

Usage::

    from portal.database import get_session, create_event, get_event_by_slug

    async with get_session() as session:
        event = await create_event(session, slug='pycon2026', display_name='PyCon 2026')

For testing, call ``init_db()`` to create all tables without Alembic.
To override the database URL in tests, call ``configure(url)`` before
any database operations.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import joinedload

from portal.models import (
    Base,
    BoothMembership,
    DBBooth,
    Event,
    EventMembership,
    InviteToken,
    Room,
    RoomMembership,
    User,
    generate_token,
    utc_now,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine lifecycle — lazy init, overridable for tests
# ---------------------------------------------------------------------------

_engine = None
_async_session_factory = None


def _get_engine():
    global _engine, _async_session_factory
    if _engine is None:
        from portal.config import settings

        _engine = create_async_engine(settings.database_url, echo=settings.debug)
        _async_session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _get_session_factory():
    if _async_session_factory is None:
        _get_engine()
    return _async_session_factory


def configure(url: str, *, echo: bool = False) -> None:
    """Override the database URL. Useful for tests or manual setup.

    Must be called before any database operations.  Calling it again
    replaces the engine (the old engine is NOT disposed — call
    ``dispose()`` first if you need that).
    """
    global _engine, _async_session_factory
    _engine = create_async_engine(url, echo=echo)
    _async_session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def dispose() -> None:
    """Dispose the current engine (close connection pool)."""
    global _engine, _async_session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _async_session_factory = None


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession]:
    factory = _get_session_factory()
    async with factory() as session:
        async with session.begin():
            yield session


async def init_db() -> None:
    """Create all tables (for testing / development without Alembic)."""
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db() -> None:
    """Drop all tables (for testing teardown)."""
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------


async def create_event(session: AsyncSession, *, slug: str, display_name: str) -> Event:
    ev = Event(slug=slug, display_name=display_name)
    session.add(ev)
    await session.flush()
    return ev


async def get_event_by_slug(session: AsyncSession, slug: str) -> Event | None:
    result = await session.execute(select(Event).where(Event.slug == slug))
    return result.scalar_one_or_none()


async def get_event_by_id(session: AsyncSession, event_id: int) -> Event | None:
    result = await session.execute(select(Event).where(Event.id == event_id))
    return result.scalar_one_or_none()


async def count_events(session: AsyncSession, *, allowed_event_ids: set[int] | None = None) -> int:
    stmt = select(func.count(Event.id))
    if allowed_event_ids is not None:
        stmt = stmt.where(Event.id.in_(allowed_event_ids))
    result = await session.execute(stmt)
    return result.scalar_one()


async def list_events(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    allowed_event_ids: set[int] | None = None,
) -> list[Event]:
    stmt = select(Event).order_by(Event.created_at)
    if allowed_event_ids is not None:
        stmt = stmt.where(Event.id.in_(allowed_event_ids))
    result = await session.execute(stmt.limit(limit).offset(offset))
    return list(result.scalars().all())


async def delete_event(session: AsyncSession, event_id: int) -> bool:
    ev = await get_event_by_id(session, event_id)
    if ev is None:
        return False
    await session.delete(ev)
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# Room CRUD
# ---------------------------------------------------------------------------


async def create_room(
    session: AsyncSession,
    *,
    event_id: int,
    display_name: str,
    eventyay_room_id: str | None = None,
    jitsi_url: str | None = None,
) -> Room:
    room = Room(event_id=event_id, display_name=display_name, eventyay_room_id=eventyay_room_id, jitsi_url=jitsi_url)
    session.add(room)
    await session.flush()
    return room


async def get_room_by_id(session: AsyncSession, room_id: int) -> Room | None:
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(Room).options(selectinload(Room.translation_languages)).where(Room.id == room_id)
    )
    return result.scalar_one_or_none()


async def count_rooms_for_event(session: AsyncSession, event_id: int) -> int:
    result = await session.execute(select(func.count(Room.id)).where(Room.event_id == event_id))
    return result.scalar_one()


async def list_rooms_for_event(
    session: AsyncSession,
    event_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Room]:
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(Room)
        .options(selectinload(Room.translation_languages))
        .where(Room.event_id == event_id)
        .order_by(Room.created_at)
        .limit(limit)
        .offset(offset),
    )
    return list(result.scalars().all())


async def delete_room(session: AsyncSession, room_id: int) -> bool:
    room = await get_room_by_id(session, room_id)
    if room is None:
        return False
    await session.delete(room)
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# DBBooth CRUD
# ---------------------------------------------------------------------------


async def create_booth(
    session: AsyncSession,
    *,
    event_id: int,
    room_id: int,
    language_code: str,
    language_name: str,
) -> DBBooth:
    booth = DBBooth(
        event_id=event_id,
        room_id=room_id,
        language_code=language_code,
        language_name=language_name,
    )
    session.add(booth)
    await session.flush()
    return booth


async def get_booth_by_id(session: AsyncSession, booth_id: int) -> DBBooth | None:
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(DBBooth)
        .options(joinedload(DBBooth.event), selectinload(DBBooth.translation_languages))
        .where(DBBooth.id == booth_id),
    )
    return result.scalar_one_or_none()


async def count_booths_for_event(session: AsyncSession, event_id: int) -> int:
    result = await session.execute(select(func.count(DBBooth.id)).where(DBBooth.event_id == event_id))
    return result.scalar_one()


async def list_booths_for_event(
    session: AsyncSession,
    event_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[DBBooth]:
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(DBBooth)
        .options(
            joinedload(DBBooth.event),
            joinedload(DBBooth.room),
            selectinload(DBBooth.translation_languages),
        )
        .where(DBBooth.event_id == event_id)
        .order_by(DBBooth.language_code)
        .limit(limit)
        .offset(offset),
    )
    return list(result.scalars().all())


async def list_all_booths_for_events(
    session: AsyncSession,
    event_ids: list[int],
) -> dict[int, list[DBBooth]]:
    """Return a mapping of event_id → list[DBBooth] for the given event IDs.

    Executes a single query instead of one-per-event. Returns an empty list
    for any event_id that has no booths.
    """
    if not event_ids:
        return {}
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(DBBooth)
        .options(joinedload(DBBooth.event), selectinload(DBBooth.translation_languages))
        .where(DBBooth.event_id.in_(event_ids))
        .order_by(DBBooth.event_id, DBBooth.language_code),
    )
    booths_by_event: dict[int, list[DBBooth]] = {eid: [] for eid in event_ids}
    for booth in result.scalars().all():
        booths_by_event[booth.event_id].append(booth)
    return booths_by_event


async def list_booths_for_room(session: AsyncSession, room_id: int) -> list[DBBooth]:
    from sqlalchemy.orm import selectinload

    result = await session.execute(
        select(DBBooth)
        .options(joinedload(DBBooth.event), selectinload(DBBooth.translation_languages))
        .where(DBBooth.room_id == room_id)
        .order_by(DBBooth.language_code),
    )
    return list(result.scalars().all())


async def delete_booth(session: AsyncSession, booth_id: int) -> bool:
    booth = await get_booth_by_id(session, booth_id)
    if booth is None:
        return False
    await session.delete(booth)
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# InviteToken CRUD
# ---------------------------------------------------------------------------


async def create_invite_token(
    session: AsyncSession,
    *,
    booth_id: int,
    role: str,
    label: str = "",
    expires_at: datetime | None = None,
    created_by: str = "",
) -> InviteToken:
    token = InviteToken(
        token=generate_token(),
        booth_id=booth_id,
        role=role,
        label=label,
        expires_at=expires_at,
        created_by=created_by,
    )
    session.add(token)
    await session.flush()
    return token


async def get_invite_token(session: AsyncSession, token_str: str) -> InviteToken | None:
    result = await session.execute(
        select(InviteToken)
        .options(joinedload(InviteToken.booth).joinedload(DBBooth.event))
        .where(InviteToken.token == token_str),
    )
    return result.scalar_one_or_none()


async def redeem_invite_token(session: AsyncSession, token_str: str) -> InviteToken | None:
    """Mark an invite token as used. Returns the token or None if not found.

    Raises ``ValueError`` if the token is already used or expired.
    """
    tok = await get_invite_token(session, token_str)
    if tok is None:
        return None
    if tok.is_used:
        raise ValueError("Token has already been used.")
    if tok.is_expired:
        raise ValueError("Token has expired.")
    tok.used_at = utc_now()
    await session.flush()
    return tok


async def list_tokens_for_booth(session: AsyncSession, booth_id: int) -> list[InviteToken]:
    result = await session.execute(
        select(InviteToken).where(InviteToken.booth_id == booth_id).order_by(InviteToken.created_at),
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    display_name: str,
    password_hash: str,
) -> User:
    user = User(
        email=email.strip().lower(),
        display_name=display_name,
        password_hash=password_hash,
    )
    session.add(user)
    await session.flush()
    return user


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    result = await session.execute(
        select(User).where(User.email == email.strip().lower()),
    )
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def count_users(session: AsyncSession) -> int:
    result = await session.execute(select(func.count(User.id)))
    return result.scalar_one()


async def list_users(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[User]:
    result = await session.execute(select(User).order_by(User.created_at).limit(limit).offset(offset))
    return list(result.scalars().all())


async def update_user_active(session: AsyncSession, user_id: int, *, is_active: bool) -> User | None:
    user = await get_user_by_id(session, user_id)
    if user is None:
        return None
    user.is_active = is_active
    await session.flush()
    return user


async def delete_user(session: AsyncSession, user_id: int) -> bool:
    user = await get_user_by_id(session, user_id)
    if user is None:
        return False
    await session.delete(user)
    await session.flush()
    return True


# ---------------------------------------------------------------------------
# EventMembership CRUD
# ---------------------------------------------------------------------------


async def set_event_membership(
    session: AsyncSession,
    *,
    user_id: int,
    event_id: int,
    role: str,
) -> EventMembership:
    """Create or update a user's role for an event."""
    result = await session.execute(
        select(EventMembership).where(
            EventMembership.user_id == user_id,
            EventMembership.event_id == event_id,
        ),
    )
    membership = result.scalar_one_or_none()
    if membership:
        membership.role = role
    else:
        membership = EventMembership(user_id=user_id, event_id=event_id, role=role)
        session.add(membership)
    await session.flush()
    return membership


async def remove_event_membership(session: AsyncSession, membership_id: int) -> bool:
    result = await session.execute(
        select(EventMembership).where(EventMembership.id == membership_id),
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        return False
    await session.delete(membership)
    await session.flush()
    return True


async def list_memberships_for_event(session: AsyncSession, event_id: int) -> list[EventMembership]:
    result = await session.execute(
        select(EventMembership)
        .options(joinedload(EventMembership.user))
        .where(EventMembership.event_id == event_id)
        .order_by(EventMembership.created_at),
    )
    return list(result.scalars().all())


async def list_memberships_for_user(session: AsyncSession, user_id: int) -> list[EventMembership]:
    result = await session.execute(
        select(EventMembership)
        .options(joinedload(EventMembership.event))
        .where(EventMembership.user_id == user_id)
        .order_by(EventMembership.created_at),
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# RoomMembership CRUD
# ---------------------------------------------------------------------------


async def set_room_membership(
    session: AsyncSession,
    *,
    user_id: int,
    room_id: int,
    role: str,
) -> RoomMembership:
    """Create or update a user's role for a room."""
    result = await session.execute(
        select(RoomMembership).where(
            RoomMembership.user_id == user_id,
            RoomMembership.room_id == room_id,
        ),
    )
    membership = result.scalar_one_or_none()
    if membership:
        membership.role = role
    else:
        membership = RoomMembership(user_id=user_id, room_id=room_id, role=role)
        session.add(membership)
    await session.flush()
    return membership


async def remove_room_membership(session: AsyncSession, membership_id: int) -> bool:
    result = await session.execute(
        select(RoomMembership).where(RoomMembership.id == membership_id),
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        return False
    await session.delete(membership)
    await session.flush()
    return True


async def list_memberships_for_room(session: AsyncSession, room_id: int) -> list[RoomMembership]:
    result = await session.execute(
        select(RoomMembership)
        .options(joinedload(RoomMembership.user))
        .where(RoomMembership.room_id == room_id)
        .order_by(RoomMembership.created_at),
    )
    return list(result.scalars().all())


async def list_room_memberships_for_user(session: AsyncSession, user_id: int) -> list[RoomMembership]:
    result = await session.execute(
        select(RoomMembership)
        .options(joinedload(RoomMembership.room).joinedload(Room.event))
        .where(RoomMembership.user_id == user_id)
        .order_by(RoomMembership.created_at),
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# BoothMembership CRUD
# ---------------------------------------------------------------------------


async def set_booth_membership(
    session: AsyncSession,
    *,
    user_id: int,
    booth_id: int,
    role: str,
) -> BoothMembership:
    """Create or update a user's role for a specific booth."""
    result = await session.execute(
        select(BoothMembership).where(
            BoothMembership.user_id == user_id,
            BoothMembership.booth_id == booth_id,
        ),
    )
    membership = result.scalar_one_or_none()
    if membership:
        membership.role = role
    else:
        membership = BoothMembership(user_id=user_id, booth_id=booth_id, role=role)
        session.add(membership)
    await session.flush()
    return membership


async def remove_booth_membership(session: AsyncSession, membership_id: int) -> bool:
    result = await session.execute(
        select(BoothMembership).where(BoothMembership.id == membership_id),
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        return False
    await session.delete(membership)
    await session.flush()
    return True


async def list_memberships_for_booth(session: AsyncSession, booth_id: int) -> list[BoothMembership]:
    result = await session.execute(
        select(BoothMembership)
        .options(joinedload(BoothMembership.user))
        .where(BoothMembership.booth_id == booth_id)
        .order_by(BoothMembership.created_at),
    )
    return list(result.scalars().all())


async def list_booth_memberships_for_user(session: AsyncSession, user_id: int) -> list[BoothMembership]:
    result = await session.execute(
        select(BoothMembership)
        .options(
            joinedload(BoothMembership.booth).joinedload(DBBooth.event),
            joinedload(BoothMembership.booth).joinedload(DBBooth.room),
        )
        .where(BoothMembership.user_id == user_id)
        .order_by(BoothMembership.created_at),
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Token revocation
# ---------------------------------------------------------------------------


async def revoke_invite_token(session: AsyncSession, token_str: str) -> InviteToken | None:
    """Revoke an invite token by setting used_at (prevents future use)."""
    tok = await get_invite_token(session, token_str)
    if tok is None:
        return None
    tok.used_at = utc_now()
    await session.flush()
    return tok


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------


async def save_transcript_segment(booth_id_str: str, text: str, room_id: int | None = None) -> int | None:
    """Save a finalized transcript segment to the database asynchronously and return its ID."""
    from sqlalchemy import select

    from portal.models import DBBooth, Event, TranscriptSegment

    parts = booth_id_str.split("-")
    if len(parts) < 2:
        return None

    language_code = parts[-1]
    event_slug = "-".join(parts[:-1])

    try:
        async with get_session() as session:
            booth_id = None
            if language_code != "floor":
                stmt = (
                    select(DBBooth.id)
                    .join(Event)
                    .where(Event.slug == event_slug, DBBooth.language_code == language_code)
                )
                booth_id = await session.scalar(stmt)

            segment = TranscriptSegment(room_id=room_id, booth_id=booth_id, language_code=language_code, text=text)
            session.add(segment)
            await session.commit()
            return segment.id
    except Exception as e:
        logger.error(f"Failed to save transcript segment: {e}")
        return None
