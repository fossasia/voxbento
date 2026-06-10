"""SQLAlchemy declarative models for persistent portal entities.

Six tables:
- ``events`` — top-level event container (slug is unique key)
- ``rooms`` — rooms within an event (mapped to Eventyay rooms)
- ``booths`` — interpretation booths (one per language per room)
- ``invite_tokens`` — single-use invite tokens for booth access
- ``users`` — registered user accounts
- ``event_memberships`` — per-event role assignments for users
- ``booth_memberships`` — per-booth role assignments for users (e.g. interpreter)

Design decisions
~~~~~~~~~~~~~~~~
- **No mediamtx_path column**: derived at runtime via
  ``portal.booth_identity.make_mediamtx_path(event.slug, booth.language_code)``.
- **No hls_url column**: WHEP is the primary playback protocol.
- **Model name DBBooth**: avoids collision with the in-memory ``Booth``
  dataclass in ``portal.booth_state``.
- **InviteToken.role** stores a ``ParticipantRole`` string value validated
  against ``portal.roles.ALL_ROLES``.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, validates
import sqlalchemy as sa

from portal.booth_identity import make_mediamtx_path, validate_event_slug, validate_language_code
from portal.roles import ALL_ROLES

TOKEN_LENGTH = 64  # hex characters → 32 bytes of entropy


def utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def generate_token() -> str:
    return secrets.token_hex(TOKEN_LENGTH // 2)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


class Event(Base):
    __tablename__ = 'events'

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    transcription_api_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default='0')
    encrypted_openai_api_key: Mapped[str | None] = mapped_column("openai_api_key", Text, nullable=True, default=None)
    encrypted_deepgram_api_key: Mapped[str | None] = mapped_column("deepgram_api_key", Text, nullable=True, default=None)
    encrypted_nvidia_api_key: Mapped[str | None] = mapped_column("nvidia_api_key", Text, nullable=True, default=None)
    encrypted_elevenlabs_api_key: Mapped[str | None] = mapped_column("elevenlabs_api_key", Text, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    rooms: Mapped[list[Room]] = relationship(back_populates='event', cascade='all, delete-orphan')
    booths: Mapped[list[DBBooth]] = relationship(back_populates='event', cascade='all, delete-orphan')

    @validates('slug')
    def _validate_slug(self, _key: str, value: str) -> str:
        return validate_event_slug(value)

    def __repr__(self) -> str:
        return f'<Event slug={self.slug!r}>'


# ---------------------------------------------------------------------------
# Room
# ---------------------------------------------------------------------------


class Room(Base):
    __tablename__ = 'rooms'

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey('events.id', ondelete='CASCADE'))
    display_name: Mapped[str] = mapped_column(String(200))
    eventyay_room_id: Mapped[str | None] = mapped_column(String(200), nullable=True, default=None)
    jitsi_url: Mapped[str | None] = mapped_column(String(500), nullable=True, default=None)
    relay_booth_id: Mapped[int | None] = mapped_column(ForeignKey('booths.id', ondelete='SET NULL'), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    event: Mapped[Event] = relationship(back_populates='rooms')
    booths: Mapped[list['DBBooth']] = relationship(back_populates='room', cascade='all, delete-orphan', foreign_keys='DBBooth.room_id')
    relay_booth: Mapped['DBBooth'] = relationship('DBBooth', foreign_keys=[relay_booth_id])

    def __repr__(self) -> str:
        return f'<Room id={self.id} name={self.display_name!r}>'


# ---------------------------------------------------------------------------
# DBBooth
# ---------------------------------------------------------------------------


class DBBooth(Base):
    """Persistent booth record — one per language per room.

    ``mediamtx_path`` is a runtime-derived property, NOT a stored column.
    """

    __tablename__ = 'booths'
    __table_args__ = (
        Index('ix_booths_event_language', 'event_id', 'language_code', unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey('events.id', ondelete='CASCADE'))
    room_id: Mapped[int] = mapped_column(ForeignKey('rooms.id', ondelete='CASCADE'))
    language_code: Mapped[str] = mapped_column(String(2))
    language_name: Mapped[str] = mapped_column(String(100))
    transcription_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default='0')
    transcription_provider: Mapped[str] = mapped_column(String(20), default='local', server_default=sa.text("'local'"))
    transcription_model: Mapped[str] = mapped_column(String(20), default='tiny', server_default=sa.text("'tiny'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    event: Mapped[Event] = relationship(back_populates='booths')
    room: Mapped[Room] = relationship(back_populates='booths', foreign_keys=[room_id])
    invite_tokens: Mapped[list[InviteToken]] = relationship(
        back_populates='booth', cascade='all, delete-orphan',
    )
    memberships: Mapped[list[BoothMembership]] = relationship(
        back_populates='booth', cascade='all, delete-orphan',
    )

    @validates('language_code')
    def _validate_language_code(self, _key: str, value: str) -> str:
        return validate_language_code(value)

    @validates('transcription_provider')
    def _validate_transcription_provider(self, _key: str, value: str) -> str:
        # Avoid circular imports since models are imported everywhere
        from portal.transcription.providers.base import ProviderEnum
        try:
            ProviderEnum(value)
        except ValueError:
            raise ValueError(f"Invalid transcription provider '{value}'. Must be one of: {[p.value for p in ProviderEnum]}")
        return value

    @property
    def mediamtx_path(self) -> str:
        """Derive MediaMTX stream path from event slug + language code.

        Requires that ``self.event`` is loaded (use ``select_related`` /
        ``joinedload``).
        """
        return make_mediamtx_path(self.event.slug, self.language_code)

    def __repr__(self) -> str:
        return f'<DBBooth id={self.id} lang={self.language_code!r}>'


# ---------------------------------------------------------------------------
# InviteToken
# ---------------------------------------------------------------------------


class InviteToken(Base):
    __tablename__ = 'invite_tokens'

    token: Mapped[str] = mapped_column(String(TOKEN_LENGTH), primary_key=True, default=generate_token)
    booth_id: Mapped[int] = mapped_column(ForeignKey('booths.id', ondelete='CASCADE'))
    role: Mapped[str] = mapped_column(String(20))
    label: Mapped[str] = mapped_column(String(200), default='')
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, default=None)
    created_by: Mapped[str] = mapped_column(String(200), default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    booth: Mapped[DBBooth] = relationship(back_populates='invite_tokens')

    @validates('role')
    def _validate_role(self, _key: str, value: str) -> str:
        if value not in ALL_ROLES:
            raise ValueError(f"Invalid role '{value}'. Must be one of: {', '.join(sorted(ALL_ROLES))}")
        return value

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        now = utc_now()
        exp = self.expires_at
        # SQLite strips timezone info; normalise both to UTC-aware
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return now >= exp

    @property
    def is_used(self) -> bool:
        return self.used_at is not None

    def __repr__(self) -> str:
        return f'<InviteToken token={self.token[:8]}… role={self.role!r}>'


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class User(Base):
    """Registered user account.

    Users sign up with email + password. They have no global booth role —
    roles are assigned per-event via ``EventMembership``.  The only
    system-level flag is ``is_admin`` which grants admin panel access.
    """

    __tablename__ = 'users'

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    password_hash: Mapped[str] = mapped_column(String(200))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    memberships: Mapped[list[EventMembership]] = relationship(
        back_populates='user', cascade='all, delete-orphan',
    )
    booth_memberships: Mapped[list[BoothMembership]] = relationship(
        back_populates='user', cascade='all, delete-orphan',
    )

    @validates('email')
    def _validate_email(self, _key: str, value: str) -> str:
        value = value.strip().lower()
        if '@' not in value or '.' not in value.split('@')[-1]:
            raise ValueError('Invalid email address.')
        return value

    def __repr__(self) -> str:
        return f'<User id={self.id} email={self.email!r}>'


# ---------------------------------------------------------------------------
# EventMembership
# ---------------------------------------------------------------------------

# Roles valid for event memberships (no super_admin — that's system-level)
EVENT_ROLES = frozenset({'listener', 'interpreter', 'coordinator', 'event_admin'})
BOOTH_ROLES = frozenset({'listener', 'interpreter', 'coordinator'})


class EventMembership(Base):
    """Per-event role assignment for a user.

    A user can have different roles in different events. For example,
    a user might be an interpreter for PyCon and a coordinator for FOSDEM.
    """

    __tablename__ = 'event_memberships'
    __table_args__ = (
        Index('ix_membership_user_event', 'user_id', 'event_id', unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    event_id: Mapped[int] = mapped_column(ForeignKey('events.id', ondelete='CASCADE'))
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship(back_populates='memberships')
    event: Mapped[Event] = relationship()

    @validates('role')
    def _validate_role(self, _key: str, value: str) -> str:
        if value not in EVENT_ROLES:
            raise ValueError(f"Invalid event role '{value}'. Must be one of: {', '.join(sorted(EVENT_ROLES))}")
        return value

    def __repr__(self) -> str:
        return f'<EventMembership user={self.user_id} event={self.event_id} role={self.role!r}>'


# ---------------------------------------------------------------------------
# BoothMembership
# ---------------------------------------------------------------------------


class BoothMembership(Base):
    """Per-booth role assignment for a user.

    Used to assign specific interpreters/listeners to specific booths.
    """

    __tablename__ = 'booth_memberships'
    __table_args__ = (
        Index('ix_membership_user_booth', 'user_id', 'booth_id', unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'))
    booth_id: Mapped[int] = mapped_column(ForeignKey('booths.id', ondelete='CASCADE'))
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    user: Mapped[User] = relationship(back_populates='booth_memberships')
    booth: Mapped[DBBooth] = relationship(back_populates='memberships')

    @validates('role')
    def _validate_role(self, _key: str, value: str) -> str:
        if value not in BOOTH_ROLES:
            raise ValueError(f"Invalid booth role '{value}'. Must be one of: {', '.join(sorted(BOOTH_ROLES))}")
        return value

    def __repr__(self) -> str:
        return f'<BoothMembership user={self.user_id} booth={self.booth_id} role={self.role!r}>'
