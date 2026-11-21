from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from portal.auth import coordinates_room, load_membership_scope, require_oauth_scope
from portal.booth_identity import make_booth_id, make_mediamtx_path, validate_language_code
from portal.config import settings
from portal.database import delete_room as delete_db_room
from portal.database import get_db_session
from portal.globals import booths
from portal.models import (
    DBBooth,
    Event,
    EventMembership,
    OAuthAuditLog,
    OAuthClient,
    OAuthToken,
    Room,
    RoomTranslationLanguage,
    TranscriptSegment,
    TranscriptTranslation,
    User,
)
from portal.rate_limit import auth_rate_limiter
from portal.roles import EVENT_OWNER
from portal.room_setup import engine_catalog, language_name, server_unavailable_reason, voice_languages_for
from portal.transcription.constants import ALLOWED_MODELS, ProviderEnum
from portal.transcription.providers.base import get_api_key
from portal.transcription.worker import active_workers, start_transcription_worker, stop_transcription_worker
from portal.translations.constants import TRANSLATION_MODELS, TranslationProviderEnum
from portal.translations.keys import get_translation_api_key
from portal.translations.model_languages import language_support_error
from portal.translations.prompts import MAX_PERSONA_CHARS, MAX_STYLE_CHARS
from portal.tts.providers.base import TTSProviderEnum
from portal.tts.worker import invalidate_config as invalidate_tts_config
from portal.websockets.revocation import revoke_booths_for_deleted_entity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")

_MAX_DATABASE_ID = 2**31 - 1

# Maps each EventAPIKeysUpdate field name to its corresponding encrypted
# attribute on the Event model.  Adding a new provider only requires one
# new entry here.
_API_KEY_FIELDS: list[tuple[str, str]] = [
    ("openai_api_key", "encrypted_openai_api_key"),
    ("deepgram_api_key", "encrypted_deepgram_api_key"),
    ("nvidia_api_key", "encrypted_nvidia_api_key"),
    ("elevenlabs_api_key", "encrypted_elevenlabs_api_key"),
    ("translation_openai_api_key", "encrypted_translation_openai_api_key"),
    ("openrouter_api_key", "encrypted_openrouter_api_key"),
    ("gemini_api_key", "encrypted_gemini_api_key"),
    ("anthropic_api_key", "encrypted_anthropic_api_key"),
    ("groq_api_key", "encrypted_groq_api_key"),
    ("atlascloud_api_key", "encrypted_atlascloud_api_key"),
]


async def _lock_event_for_request(db: AsyncSession, event: Event, *, write_lock: bool) -> Event | None:
    """Reload an event and serialize a mutation against concurrent deletion.

    PostgreSQL uses a row lock for mutating requests. SQLite ignores
    ``FOR UPDATE``, so it performs a guarded no-op update instead; that starts
    a write transaction and keeps the database write lock until the request
    transaction commits or rolls back.
    """
    is_sqlite = db.get_bind().dialect.name == "sqlite"

    if write_lock and is_sqlite:
        await db.execute(
            update(Event)
            .where(Event.id == event.id, Event.deleted_at.is_(None))
            .values(deleted_at=Event.deleted_at)
            .execution_options(synchronize_session=False)
        )
        # A concurrent delete may have committed while the guarded update
        # waited for SQLite's writer lock. Refresh below so the caller returns
        # the normal deleted-event 404 rather than succeeding.

    event_query = select(Event).where(Event.id == event.id).execution_options(populate_existing=True)
    if write_lock and not is_sqlite:
        event_query = event_query.with_for_update()

    result = await db.execute(event_query)
    return result.scalars().first()


async def _get_event_for_request(db: AsyncSession, event_slug: str, *, write_lock: bool) -> Event | None:
    """Load an event, acquiring the mutation lock before the first read."""
    is_sqlite = db.get_bind().dialect.name == "sqlite"

    event_query = select(Event).where(Event.slug == event_slug)
    if write_lock and is_sqlite:
        await db.execute(
            update(Event)
            .where(Event.slug == event_slug, Event.deleted_at.is_(None))
            .values(deleted_at=Event.deleted_at)
            .execution_options(synchronize_session=False)
        )
    elif write_lock:
        event_query = event_query.with_for_update()

    result = await db.execute(event_query.execution_options(populate_existing=True))
    return result.scalars().first()


async def _verify_token_rbac(
    db: AsyncSession,
    token: OAuthToken,
    event: Event,
    room_id: int | str | None = None,
    *,
    manage: bool = False,
    lock_event: bool = False,
    write_lock: bool = False,
    room_id_kind: Literal["auto", "internal", "eventyay"] = "auto",
    reject_ambiguous_room_id: bool = False,
) -> Room | None:
    """Validate OAuth RBAC and optionally lock the event for a mutating request.

    A token whose ``event_id`` does not match ``event`` is answered with the
    same ``404 Event not found`` an unknown slug gets, for every client type,
    so that a token scoped to one event cannot be used to probe which other
    events exist (``docs/threat_model.md`` §3).

    manage=True is for creating, changing or deleting rooms and booths, which only Admins and
    Event owners may do (#757); Room coordinators pass only for live-session and read actions.
    write_lock=True serializes the request against concurrent event deletion.
    """
    if lock_event:
        event = await _lock_event_for_request(db, event, write_lock=write_lock)

    if event is None or event.deleted_at is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    if token.event_id != event.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    resolved_room: Room | None = None
    if room_id is not None:
        if room_id_kind in {"auto", "eventyay"}:
            # ``auto`` keeps the existing Eventyay-ID-first behavior for legacy
            # routes. Settings updates use an explicit identifier kind instead.
            stmt = select(Room).where(Room.event_id == event.id, Room.eventyay_room_id == str(room_id))
            res = await db.execute(stmt)
            resolved_room = res.scalars().first()

        if (not resolved_room or reject_ambiguous_room_id) and room_id_kind in {"auto", "internal"}:
            parsed_id: int | None = None
            if isinstance(room_id, int):
                parsed_id = room_id
            elif room_id_kind == "auto" and isinstance(room_id, str) and room_id.isascii() and room_id.isdecimal():
                try:
                    parsed_id = int(room_id)
                except ValueError:
                    parsed_id = None

            if parsed_id is not None and not 1 <= parsed_id <= _MAX_DATABASE_ID:
                parsed_id = None
            if parsed_id is not None:
                internal_room = await db.get(Room, parsed_id)
                if internal_room and internal_room.event_id != event.id:
                    internal_room = None
                if resolved_room and internal_room and resolved_room.id != internal_room.id:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="Room identifier is ambiguous; use an explicit internal or Eventyay settings route",
                    )
                resolved_room = resolved_room or internal_room

        if not resolved_room:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

    from portal.models import User

    user = await db.get(User, token.user_id)

    # Account state is security-critical, so read it from the User row rather
    # than trusting the token: admin_toggle_user_active deactivates a user
    # without revoking their OAuth tokens, so an unexpired token outlives the
    # account unless we re-check here, ahead of every bypass below.
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User account is inactive")

    # Confidential clients manage their own RBAC
    client = await db.get(OAuthClient, token.client_id)
    if client and client.is_confidential and client.status == "active":
        return resolved_room

    # Check if user is Admin
    if getattr(user, "is_admin", False):
        return resolved_room

    scope = await load_membership_scope(db, token.user_id)
    if event.id in scope["owned_event_ids"]:
        return resolved_room

    # Room coordinators may be given per event, room or booth (#757); the room was checked above.
    if not manage and resolved_room is not None and coordinates_room(scope, resolved_room.id, event.id):
        return resolved_room

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User lost RBAC access to this resource")


class EventResponse(BaseModel):
    id: int = Field(description="Internal database ID.")
    slug: str = Field(description="URL-safe unique event identifier.")
    display_name: str = Field(description="Human-readable event name.")
    owner_id: int | None = Field(
        default=None,
        description=(
            "User ID of the event owner resolved from EventMembership. "
            "None if no owner membership exists. "
            "If multiple owner rows exist, the lowest user_id is returned."
        ),
    )
    created_at: str = Field(description="ISO-8601 UTC creation timestamp.")


def _extract_room_id(room: Room | Any, room_id: int | str) -> int:
    """Return the internal room primary key for either supported room identifier."""

    if room is not None and hasattr(room, "id") and isinstance(room.id, int):
        return room.id
    if isinstance(room_id, int):
        return room_id
    if isinstance(room_id, str) and room_id.isascii() and room_id.isdecimal():
        try:
            return int(room_id)
        except ValueError:
            pass
    return getattr(room, "id", room_id)


def _validate_lang(language_code: str) -> str:
    try:
        return validate_language_code(language_code)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/events/{event_slug}", response_model=EventResponse)
async def get_event(
    event_slug: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:read")),
):
    """Retrieve event details by slug.

    Resource-visibility policy (``docs/threat_model.md`` §3) — every case where
    the caller may not see the event returns an identical
    ``404 {"detail": "Event not found"}``:

    * the slug does not exist,
    * the event is soft-deleted (``deleted_at`` set),
    * the event exists but the token is scoped to a different event.

    A caller therefore cannot tell "no such event" from "not yours", so a token
    for one event cannot enumerate other events. 403 is reserved for callers
    whose token *is* scoped to this event but whose user has since lost access
    (deactivated account, or no owner/coordinator membership) — they already
    know the event exists, so there is nothing left to leak.

    ``owner_id`` is resolved from ``EventMembership`` (role ``event_owner``);
    ``Event`` itself has no ``owner_id`` column.
    """
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, lock_event=True)

    # Lowest user_id wins so the response is stable when an event has several
    # event_owner rows. One query — do not loop over memberships here.
    owner_result = await db.execute(
        select(EventMembership.user_id)
        .where(
            EventMembership.event_id == event.id,
            EventMembership.role == EVENT_OWNER,
        )
        .order_by(EventMembership.user_id)
        .limit(1)
    )
    owner_id = owner_result.scalar_one_or_none()

    return {
        "id": event.id,
        "slug": event.slug,
        "display_name": event.display_name,
        "owner_id": owner_id,
        "created_at": event.created_at.isoformat(),
    }


@router.get("/events/{event_slug}/rooms")
async def get_rooms(
    event_slug: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, lock_event=True)

    room_result = await db.execute(select(Room).where(Room.event_id == event.id))
    rooms = room_result.scalars().all()

    return [
        {
            "id": r.id,
            "display_name": r.display_name,
            "created_at": r.created_at.isoformat(),
        }
        for r in rooms
    ]


class EventCreate(BaseModel):
    slug: str
    name: str
    organizer_emails: list[str] = Field(default_factory=list)

    @field_validator("organizer_emails")
    @classmethod
    def validate_organizer_emails(cls, v: list[str]) -> list[str]:
        cleaned = []
        for e in v:
            val = e.strip().lower()
            if not val:
                continue
            if "@" not in val or "." not in val.split("@")[-1] or val.startswith("@") or val.endswith("."):
                raise ValueError(f"Invalid organizer email address: {e}")
            cleaned.append(val)
        return cleaned


async def _ensure_event_memberships(
    db: AsyncSession, event_id: int, user_id: int | None, organizer_emails: list[str]
) -> None:
    from sqlalchemy.exc import IntegrityError

    from portal.models import EventMembership, User

    async def _upsert_member(uid: int, role: str = EVENT_OWNER) -> None:
        try:
            async with db.begin_nested():
                mem_res = await db.execute(
                    select(EventMembership).where(
                        EventMembership.user_id == uid,
                        EventMembership.event_id == event_id,
                    )
                )
                existing = mem_res.scalars().first()
                if existing:
                    if existing.role != role:
                        existing.role = role
                else:
                    db.add(EventMembership(user_id=uid, event_id=event_id, role=role))
                await db.flush()
        except IntegrityError:
            try:
                async with db.begin_nested():
                    mem_res = await db.execute(
                        select(EventMembership).where(
                            EventMembership.user_id == uid,
                            EventMembership.event_id == event_id,
                        )
                    )
                    existing = mem_res.scalars().first()
                    if existing and existing.role != role:
                        existing.role = role
                        await db.flush()
            except IntegrityError:
                pass

    if user_id:
        await _upsert_member(user_id, EVENT_OWNER)

    for email_raw in organizer_emails:
        email = email_raw.strip().lower()
        if not email or "@" not in email:
            continue
        user_res = await db.execute(select(User).where(User.email == email))
        user_obj = user_res.scalars().first()
        if not user_obj:
            try:
                async with db.begin_nested():
                    user_obj = User(
                        email=email,
                        display_name=email.split("@")[0],
                        is_active=True,
                        email_verified=False,
                    )
                    db.add(user_obj)
                    await db.flush()
            except IntegrityError:
                user_res = await db.execute(select(User).where(User.email == email))
                user_obj = user_res.scalars().first()

        if user_obj:
            await _upsert_member(user_obj.id, EVENT_OWNER)


@router.post("/events/", status_code=status.HTTP_201_CREATED)
async def create_event(
    request: Request,
    payload: EventCreate,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:write")),
):
    """Create an event or update an existing active event's memberships."""
    client_ip = request.client.host if request.client else "unknown"
    if await auth_rate_limiter.is_rate_limited(f"create_event_{client_ip}"):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Too many requests")

    from portal.booth_identity import validate_event_slug

    try:
        slug = validate_event_slug(payload.slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing_event = await _get_event_for_request(db, slug, write_lock=True)

    if existing_event is not None:
        if existing_event.deleted_at is not None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

        if token.event_id == existing_event.id:
            await _verify_token_rbac(db, token, existing_event, manage=True)
        else:
            # Outside the token's scope only a current owner passes, e.g. retrying a creation.
            user = await db.get(User, token.user_id)
            owner_membership = await db.scalar(
                select(EventMembership.id).where(
                    EventMembership.user_id == token.user_id,
                    EventMembership.event_id == existing_event.id,
                    EventMembership.role == EVENT_OWNER,
                )
            )
            if user is None or not user.is_active or owner_membership is None:
                raise HTTPException(status_code=403, detail="Not authorized to manage memberships for this event.")

        # Update the display name if the event already exists (e.g. from OAuth auto-provisioning)
        if existing_event.display_name != payload.name:
            existing_event.display_name = payload.name
            db.add(existing_event)
            await db.flush()
        await _ensure_event_memberships(db, existing_event.id, token.user_id, payload.organizer_emails)
        return {"id": existing_event.id, "slug": existing_event.slug, "name": existing_event.display_name}
    else:
        # A retained token must not bootstrap new ownership after its grant loses access.
        authorizing_event = await db.get(Event, token.event_id)
        if authorizing_event is None:
            raise HTTPException(status_code=404, detail="Event not found")
        await _verify_token_rbac(db, token, authorizing_event, manage=True, lock_event=True, write_lock=True)

        event = Event(slug=slug, display_name=payload.name)
        db.add(event)
        await db.flush()

        # The organizer becomes owner; the app's developer gets no automatic
        # access (#757). The organizer can add them as a member if they want help.
        await _ensure_event_memberships(db, event.id, token.user_id, payload.organizer_emails)

        action = "event.created"
        status_code_ret = status.HTTP_201_CREATED

    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        action=action,
        request_path="/api/v1/events/",
        status_code=status_code_ret,
    )
    db.add(audit)
    await db.flush()

    return {"status": "success", "event_slug": event.slug}


@router.delete("/events/{event_slug}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(
    event_slug: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("events:write")),
):
    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    await _verify_token_rbac(db, token, event, lock_event=True, write_lock=True)

    active_booths = await booths.list_booths_for_event(event_slug)
    has_active_session = any(b.get("ingest_status") == "connected" for b in active_booths)
    if has_active_session:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Cannot delete event while active booths are running."
        )

    for b in active_booths:
        await booths.remove_booth(event_slug, b["room_id"], b["language_code"])

    await revoke_booths_for_deleted_entity(db, level="event", target_id=event.id)
    event.deleted_at = datetime.now(timezone.utc)

    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        action="event.deleted",
        request_path=f"/api/v1/events/{event_slug}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    db.add(audit)
    await db.flush()
    return None


class RoomUpsert(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = Field(None, max_length=1000)
    enabled: bool | None = None
    target_languages: list[str] | None = None
    enable_transcription: bool | None = None
    transcription_provider: ProviderEnum | None = Field(None)
    transcription_model: str | None = Field(None, max_length=40)
    source_language: str | None = Field(None, max_length=10)
    enable_translation: bool | None = None
    translation_provider: TranslationProviderEnum | None = Field(None)
    translation_model: str | None = Field(None, max_length=100)

    @field_validator(
        "transcription_provider",
        "transcription_model",
        "source_language",
        "translation_provider",
        "translation_model",
        mode="before",
    )
    @classmethod
    def empty_str_to_none(cls, v):
        if v == "":
            return None
        return v

    @field_validator("target_languages")
    @classmethod
    def check_language_codes(cls, codes: list[str] | None) -> list[str] | None:
        if codes is None:
            return None
        validated = []
        for code in codes:
            normalised = validate_language_code(code)
            if normalised == "floor":
                raise ValueError("'floor' is not a valid booth language for a room.")
            validated.append(normalised)
        return validated


class RoomSettingsUpdate(BaseModel):
    """Settings that can be changed through the versioned room API.

    The API deliberately exposes an allow-list instead of accepting arbitrary
    database attributes.  A few aliases are kept for compatibility with the
    room sync payload used by Eventyay (for example, ``enable_transcription``
    maps to ``floor_transcription_enabled``).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    display_name: str | None = Field(
        None,
        min_length=1,
        max_length=200,
        validation_alias=AliasChoices("display_name", "name"),
    )
    jitsi_url: Any = Field(
        None,
        description="Meeting URLs must be changed through the dedicated meeting-source workflow.",
    )
    audio_delay_ms: int | None = Field(None, ge=0, le=10_000)
    relay_booth_id: int | None = Field(None, ge=1, le=_MAX_DATABASE_ID)

    floor_transcription_enabled: bool | None = Field(
        None,
        validation_alias=AliasChoices("floor_transcription_enabled", "enable_transcription"),
    )
    floor_transcription_provider: ProviderEnum | None = Field(
        None,
        validation_alias=AliasChoices("floor_transcription_provider", "transcription_provider"),
    )
    floor_transcription_model: str | None = Field(
        None,
        max_length=40,
        validation_alias=AliasChoices("floor_transcription_model", "transcription_model"),
    )
    floor_language_code: str | None = Field(
        None,
        max_length=10,
        validation_alias=AliasChoices("floor_language_code", "source_language"),
    )

    floor_translation_enabled: bool | None = Field(
        None,
        validation_alias=AliasChoices("floor_translation_enabled", "enable_translation"),
    )
    floor_translation_provider: TranslationProviderEnum | None = Field(
        None,
        validation_alias=AliasChoices("floor_translation_provider", "translation_provider"),
    )
    floor_translation_model: str | None = Field(
        None,
        max_length=100,
        validation_alias=AliasChoices("floor_translation_model", "translation_model"),
    )
    floor_ai_interpreter_persona: str | None = Field(None, max_length=MAX_PERSONA_CHARS)
    floor_ai_interpretation_style: str | None = Field(None, max_length=MAX_STYLE_CHARS)
    floor_ai_vocabulary_enabled: bool | None = None

    floor_tts_enabled: bool | None = None
    floor_tts_provider: TTSProviderEnum | None = None
    floor_tts_voice: Literal["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"] | None = None

    @field_validator("floor_language_code")
    @classmethod
    def validate_language_code(cls, value):
        """Normalize and validate ISO language codes before persistence."""

        if value is None:
            return value
        normalised = validate_language_code(value)
        if normalised == "floor":
            raise ValueError("'floor' is not a valid source language for a room")
        return normalised

    @field_validator(
        "floor_transcription_model",
        "floor_language_code",
        "floor_translation_provider",
        "floor_translation_model",
        "floor_ai_interpreter_persona",
        "floor_ai_interpretation_style",
        mode="before",
    )
    @classmethod
    def empty_string_to_none(cls, value):
        """Treat blank optional string settings as an explicit value to clear."""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value):
        """Trim room names while preserving validation for blank names."""

        return value.strip() if isinstance(value, str) else value


_NON_NULL_ROOM_SETTINGS = frozenset(
    {
        "display_name",
        "audio_delay_ms",
        "floor_transcription_enabled",
        "floor_transcription_provider",
        "floor_transcription_model",
        "floor_translation_enabled",
        "floor_ai_vocabulary_enabled",
        "floor_tts_enabled",
        "floor_tts_provider",
        "floor_tts_voice",
    }
)

_TRANSCRIPTION_ROOM_SETTINGS = frozenset(
    {
        "floor_transcription_enabled",
        "floor_transcription_provider",
        "floor_transcription_model",
        "floor_language_code",
    }
)
_TRANSLATION_ROOM_SETTINGS = frozenset(
    {
        "floor_translation_enabled",
        "floor_translation_provider",
        "floor_translation_model",
        "floor_language_code",
    }
)
_TTS_ROOM_SETTINGS = frozenset(
    {"floor_tts_enabled", "floor_tts_provider", "floor_tts_voice", "floor_translation_enabled"}
)


def _effective_room_setting(room: Room, updates: dict, field: str):
    value = updates[field] if field in updates else getattr(room, field)
    return getattr(value, "value", value)


def _validate_transcription_settings(event: Event, room: Room, updates: dict) -> None:
    if not _TRANSCRIPTION_ROOM_SETTINGS.intersection(updates):
        return

    enabled = bool(_effective_room_setting(room, updates, "floor_transcription_enabled"))
    provider_value = _effective_room_setting(room, updates, "floor_transcription_provider")
    model = _effective_room_setting(room, updates, "floor_transcription_model")
    language = _effective_room_setting(room, updates, "floor_language_code")

    if not enabled and not {"floor_transcription_provider", "floor_transcription_model"}.intersection(updates):
        return
    try:
        provider = ProviderEnum(provider_value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A valid transcription provider is required for transcription settings",
        ) from None
    if model not in ALLOWED_MODELS[provider]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Invalid transcription model '{model}' for provider '{provider.value}'",
        )
    if not enabled:
        return
    if provider == ProviderEnum.NONE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Transcription cannot be enabled with provider 'none'",
        )
    if not language:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Select a source language before enabling transcription",
        )
    unavailable = server_unavailable_reason(provider.value)
    if unavailable:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=unavailable)
    if provider != ProviderEnum.LOCAL:
        if not event.transcription_api_enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Enable the event transcription API before selecting an external provider",
            )
        try:
            provider_key = get_api_key(event, provider)
        except ValueError:
            provider_key = None
        if not provider_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Configure the {provider.value} API key before enabling this provider",
            )


def _translation_sync_payload(updates: dict) -> dict:
    field_map = {
        "floor_translation_enabled": "enable_translation",
        "floor_translation_provider": "translation_provider",
        "floor_translation_model": "translation_model",
        "floor_language_code": "source_language",
    }
    return {field_map[field]: getattr(value, "value", value) for field, value in updates.items() if field in field_map}


async def _validate_translation_settings(db: AsyncSession, event: Event, room: Room, updates: dict) -> None:
    translation_changed = bool(_TRANSLATION_ROOM_SETTINGS.intersection(updates))
    tts_needs_translation = bool(
        {"floor_tts_enabled", "floor_tts_provider", "floor_tts_voice"}.intersection(updates)
        and _effective_room_setting(room, updates, "floor_tts_enabled")
    )
    if not translation_changed and not tts_needs_translation:
        return

    if "floor_translation_provider" in updates and "floor_translation_model" not in updates:
        provider = _effective_room_setting(room, updates, "floor_translation_provider")
        if provider != room.floor_translation_provider:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="translation_model is required when changing translation_provider",
            )

    sync_payload = _translation_sync_payload(updates)
    if tts_needs_translation and not translation_changed:
        sync_payload = {
            "enable_translation": _effective_room_setting(room, updates, "floor_translation_enabled"),
            "translation_provider": _effective_room_setting(room, updates, "floor_translation_provider"),
            "translation_model": _effective_room_setting(room, updates, "floor_translation_model"),
            "source_language": _effective_room_setting(room, updates, "floor_language_code"),
        }
    _check_server_translation(sync_payload, room)
    if "translation_model" in sync_payload and "floor_translation_model" not in updates:
        updates["floor_translation_model"] = sync_payload["translation_model"]

    enabled = bool(_effective_room_setting(room, updates, "floor_translation_enabled"))
    provider_value = _effective_room_setting(room, updates, "floor_translation_provider")
    model = _effective_room_setting(room, updates, "floor_translation_model")
    try:
        provider = TranslationProviderEnum(provider_value)
    except (TypeError, ValueError):
        provider = None
    choice_changed = bool({"floor_translation_provider", "floor_translation_model"}.intersection(updates) or enabled)
    if choice_changed:
        if provider is None or not model:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="A translation provider and model are required before enabling translation",
            )
        if provider not in {TranslationProviderEnum.LOCAL, TranslationProviderEnum.SELF_HOSTED}:
            if model not in TRANSLATION_MODELS.get(provider.value, ()):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Invalid translation model '{model}' for provider '{provider.value}'",
                )
    if enabled:
        language = _effective_room_setting(room, updates, "floor_language_code")
        if not language:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Select a source language before enabling translation",
            )
        if provider not in {TranslationProviderEnum.LOCAL, TranslationProviderEnum.SELF_HOSTED}:
            try:
                provider_key = get_translation_api_key(event, provider.value)
            except ValueError:
                provider_key = None
            if not provider_key:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=f"Configure the {provider.value} translation API key before enabling this provider",
                )
        has_target = await db.scalar(
            select(RoomTranslationLanguage.id)
            .where(
                RoomTranslationLanguage.room_id == room.id,
                RoomTranslationLanguage.enabled.is_(True),
            )
            .limit(1)
        )
        if has_target is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Add at least one enabled target language before enabling translation",
            )

    await _check_translation_languages(db, sync_payload, room, force=tts_needs_translation)
    if enabled and not _effective_room_setting(room, updates, "floor_transcription_enabled"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Enable floor transcription before enabling translation",
        )


async def _validate_tts_settings(db: AsyncSession, event: Event, room: Room, updates: dict) -> None:
    if not _TTS_ROOM_SETTINGS.intersection(updates):
        return
    if not _effective_room_setting(room, updates, "floor_tts_enabled"):
        return
    if not _effective_room_setting(room, updates, "floor_translation_enabled"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Enable floor translation before enabling TTS",
        )

    provider_value = _effective_room_setting(room, updates, "floor_tts_provider")
    try:
        provider = TTSProviderEnum(provider_value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="A valid TTS provider is required before enabling TTS",
        ) from None
    if provider in {TTSProviderEnum.DEEPGRAM, TTSProviderEnum.ELEVENLABS}:
        key_provider = ProviderEnum(provider.value)
        try:
            provider_key = get_api_key(event, key_provider)
        except ValueError:
            provider_key = None
        if not provider_key:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{provider.value} TTS is not available for this event",
            )
    if engine_catalog(event).find("voice", provider.value) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{provider.value} TTS is not available for this event",
        )

    spoken_codes = list(
        await db.scalars(
            select(RoomTranslationLanguage.language_code).where(
                RoomTranslationLanguage.room_id == room.id,
                RoomTranslationLanguage.enabled.is_(True),
                RoomTranslationLanguage.tts_enabled.is_(True),
            )
        )
    )
    if not spoken_codes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Add at least one AI voice target language before enabling TTS",
        )
    supported = set(voice_languages_for(provider.value))
    unsupported = [language_name(code) for code in spoken_codes if code not in supported]
    if unsupported:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"{provider.value} TTS can't speak {', '.join(unsupported)}. "
                "Choose another provider or use AI captions for that language"
            ),
        )


def _sync_floor_transcription_settings(room: Room, payload_dict: dict) -> None:
    """Apply floor transcription configuration and source language to the room instance."""
    if "enable_transcription" in payload_dict and payload_dict["enable_transcription"] is not None:
        room.floor_transcription_enabled = payload_dict["enable_transcription"]
    if "transcription_provider" in payload_dict:
        val = payload_dict["transcription_provider"]
        room.floor_transcription_provider = getattr(val, "value", val) or "local"
    if "transcription_model" in payload_dict:
        room.floor_transcription_model = payload_dict["transcription_model"] or "tiny"
    if "source_language" in payload_dict:
        room.floor_language_code = payload_dict["source_language"]


def _sync_floor_translation_settings(room: Room, payload_dict: dict) -> None:
    """Apply floor translation configuration to the room instance."""
    if "enable_translation" in payload_dict and payload_dict["enable_translation"] is not None:
        room.floor_translation_enabled = payload_dict["enable_translation"]
    if "translation_provider" in payload_dict:
        val = payload_dict["translation_provider"]
        room.floor_translation_provider = getattr(val, "value", val)
    if "translation_model" in payload_dict:
        room.floor_translation_model = payload_dict["translation_model"]


def _check_server_translation(payload_dict: dict, room: Room | None) -> None:
    """Reject self-hosted or built-in translation this server can't run, before anything is written.

    An explicit model must be on the server's allow list; when the model is left out, the room keeps
    an allowed model it already has or gets the default one. An update that turns translation off is
    never rejected, so it still works after the server or a model is removed.
    """
    if not {"translation_provider", "translation_model", "enable_translation"} & payload_dict.keys():
        return
    if payload_dict.get("enable_translation") is False:
        return
    current_provider = room.floor_translation_provider if room else None
    provider = payload_dict.get("translation_provider", current_provider)
    if provider == TranslationProviderEnum.SELF_HOSTED.value:
        if not settings.self_hosted_llm_enabled:
            raise HTTPException(status_code=422, detail="Self-hosted translation is not configured on this server")
        allows, model_or_default, name = (
            settings.self_hosted_llm_allows,
            settings.self_hosted_llm_model_or_default,
            "self-hosted",
        )
    elif provider == TranslationProviderEnum.LOCAL.value:
        if not settings.local_translation_model_list:
            raise HTTPException(status_code=422, detail="Built-in translation is not offered on this server")
        allows, model_or_default, name = (
            settings.local_translation_allows,
            settings.local_translation_model_or_default,
            "built-in",
        )
    else:
        return
    model = payload_dict.get("translation_model")
    if model is not None:
        if not allows(model):
            raise HTTPException(status_code=422, detail=f"translation_model is not allowed for {name} translation")
        return
    payload_dict["translation_model"] = model_or_default(room.floor_translation_model if room else None)


_TRANSLATION_SETUP_FIELDS = {
    "translation_provider",
    "translation_model",
    "enable_translation",
    "source_language",
    "target_languages",
}


async def _check_translation_languages(
    db: AsyncSession, payload_dict: dict, room: Room | None, *, force: bool = False
) -> None:
    """Reject a translation model that cannot handle the room's languages, before anything is written.

    Looks at the room as it will be once the update is applied. Only an update that changes
    the translation setup is checked, and never one that leaves translation off, so a sync
    that re-sends a room saved before this rule keeps working until its setup changes.
    """
    if not _TRANSLATION_SETUP_FIELDS & payload_dict.keys():
        return

    saved_targets: list[str] = []
    if room is not None:
        rows = await db.scalars(
            select(RoomTranslationLanguage.language_code).where(
                RoomTranslationLanguage.room_id == room.id, RoomTranslationLanguage.enabled.is_(True)
            )
        )
        saved_targets = list(rows)
    before = (
        bool(room and room.floor_translation_enabled),
        room.floor_translation_provider if room else None,
        room.floor_translation_model if room else None,
        room.floor_language_code if room else None,
        sorted(saved_targets),
    )

    def after(field: str, current):
        """The proposed value from payload_dict, or the current value when omitted or null."""
        value = payload_dict.get(field)
        return current if value is None else value

    targets = payload_dict.get("target_languages")
    proposed = (
        after("enable_translation", before[0]),
        payload_dict["translation_provider"] if "translation_provider" in payload_dict else before[1],
        payload_dict["translation_model"] if "translation_model" in payload_dict else before[2],
        payload_dict["source_language"] if "source_language" in payload_dict else before[3],
        before[4] if targets is None else sorted(set(targets)),
    )
    if not proposed[0] or (room is not None and proposed == before and not force):
        return
    error = language_support_error(proposed[1], proposed[2], (proposed[3], *proposed[4]))
    if error:
        raise HTTPException(status_code=422, detail=error)


def _apply_room_settings(room: Room, payload_dict: dict) -> None:
    """Apply display name, floor transcription, and floor translation settings to the room."""
    if "name" in payload_dict and payload_dict["name"] is not None:
        room.display_name = payload_dict["name"]
    _sync_floor_transcription_settings(room, payload_dict)
    _sync_floor_translation_settings(room, payload_dict)


async def _create_or_update_base_room(
    db: AsyncSession,
    event: Event,
    eventyay_room_id: str,
    payload_dict: dict,
) -> tuple[Room, str, int]:
    """Create or update the base room while preserving upsert and concurrency transaction semantics."""
    room_res = await db.execute(
        select(Room).where(Room.event_id == event.id, Room.eventyay_room_id == eventyay_room_id)
    )
    room = room_res.scalars().first()

    _check_server_translation(payload_dict, room)
    await _check_translation_languages(db, payload_dict, room)

    if room:
        _apply_room_settings(room, payload_dict)
        return room, "room.updated", status.HTTP_200_OK

    # Require name for creation
    if "name" not in payload_dict or not payload_dict["name"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required to create a new room")

    from sqlalchemy.exc import IntegrityError

    try:
        async with db.begin_nested():
            room = Room(
                event_id=event.id,
                eventyay_room_id=eventyay_room_id,
                display_name=payload_dict["name"],
            )
            _apply_room_settings(room, payload_dict)
            db.add(room)
            await db.flush()
    except IntegrityError:
        room_res = await db.execute(
            select(Room).where(Room.event_id == event.id, Room.eventyay_room_id == eventyay_room_id)
        )
        room = room_res.scalars().first()
        if not room:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to upsert room")
        _apply_room_settings(room, payload_dict)
        return room, "room.updated", status.HTTP_200_OK

    return room, "room.created", status.HTTP_201_CREATED


async def _delete_removed_languages_and_booths(
    db: AsyncSession,
    event: Event,
    room: Room,
    eventyay_room_id: str,
    requested_langs: set[str],
    existing_booths: dict[str, DBBooth],
    existing_langs: dict[str, RoomTranslationLanguage],
    token: OAuthToken,
) -> None:
    """Delete unrequested booths and languages, enforcing active-session safety checks."""
    from portal.booth_identity import make_booth_id
    from portal.globals import booths

    # Safe Delete Removed Booths & Languages
    for code, b in existing_booths.items():
        if code not in requested_langs:
            # Active Session Guard — use BoothRegistry.get_booth_sync() (not .items())
            booth_id = make_booth_id(event.slug, room.id, code)
            active_booth = booths.get_booth_sync(booth_id)
            if active_booth is not None:
                has_connected = active_booth.ingest_status == "connected"
                if has_connected:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=f"Cannot remove language '{code}' while it has an active session running.",
                    )
            await booths.remove_booth(event.slug, room.id, code)
            await db.delete(b)
            db.add(
                OAuthAuditLog(
                    token_id=token.id,
                    client_id=token.client_id,
                    action="booth.deleted",
                    request_path=f"/api/v1/events/{event.slug}/rooms/{eventyay_room_id}/booths/{code}",
                    status_code=status.HTTP_200_OK,
                )
            )

    for code, rl in existing_langs.items():
        if code not in requested_langs:
            await db.delete(rl)


async def _create_missing_languages_and_booths(
    db: AsyncSession,
    event: Event,
    room: Room,
    eventyay_room_id: str,
    requested_langs: set[str],
    existing_booths: dict[str, DBBooth],
    existing_langs: dict[str, RoomTranslationLanguage],
    token: OAuthToken,
) -> None:
    """Create missing translation languages and booths inside savepoints."""
    from sqlalchemy.exc import IntegrityError

    # Create Missing Booths & Languages
    for code in requested_langs:
        if code not in existing_langs:
            try:
                async with db.begin_nested():
                    db.add(RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=code))
                    await db.flush()
            except IntegrityError:
                pass

        if code not in existing_booths:
            try:
                async with db.begin_nested():
                    new_booth = DBBooth(room_id=room.id, language_code=code, event_id=event.id, language_name=code)
                    db.add(new_booth)
                    db.add(
                        OAuthAuditLog(
                            token_id=token.id,
                            client_id=token.client_id,
                            action="booth.created",
                            request_path=f"/api/v1/events/{event.slug}/rooms/{eventyay_room_id}/booths/{code}",
                            status_code=status.HTTP_201_CREATED,
                        )
                    )
                    await db.flush()
            except IntegrityError:
                pass


async def _sync_target_languages_and_booths(
    db: AsyncSession,
    event: Event,
    room: Room,
    eventyay_room_id: str,
    requested_langs: set[str],
    token: OAuthToken,
) -> None:
    """Reconcile requested target languages with the room's persisted booths and translation languages."""
    lang_res = await db.execute(select(RoomTranslationLanguage).where(RoomTranslationLanguage.room_id == room.id))
    existing_langs = {rl.language_code: rl for rl in lang_res.scalars().all()}

    booth_res = await db.execute(select(DBBooth).where(DBBooth.room_id == room.id))
    existing_booths = {b.language_code: b for b in booth_res.scalars().all()}

    await _delete_removed_languages_and_booths(
        db, event, room, eventyay_room_id, requested_langs, existing_booths, existing_langs, token
    )
    await _create_missing_languages_and_booths(
        db, event, room, eventyay_room_id, requested_langs, existing_booths, existing_langs, token
    )


async def _build_booths_response(db: AsyncSession, event_slug: str, room_id: int) -> list[dict[str, str]]:
    """Build the canonical booth response list with WHEP and WHIP URLs."""
    from portal.booth_identity import make_mediamtx_path
    from portal.config import settings

    final_booth_res = await db.execute(select(DBBooth).where(DBBooth.room_id == room_id))
    final_booths = final_booth_res.scalars().all()

    returned_booths = []
    try:
        for b in final_booths:
            whip_path = make_mediamtx_path(event_slug, room_id, b.language_code)
            whep_url = f"{settings.mediamtx_whip_base}/{whip_path}/whep"
            returned_booths.append({"language": b.language_code, "whip_path": whip_path, "whep_url": whep_url})
    except Exception:
        logger.exception("Error generating canonical WHEP URLs")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error")

    return returned_booths


@router.put("/events/{event_slug}/rooms/{eventyay_room_id}")
async def upsert_room(
    event_slug: str,
    eventyay_room_id: str,
    payload: RoomUpsert,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
):
    """Create or update a room after validating its resulting translation configuration."""
    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

    await _verify_token_rbac(db, token, event, manage=True, lock_event=True, write_lock=True)

    payload_dict = payload.model_dump(exclude_unset=True)
    room, action, status_code_ret = await _create_or_update_base_room(db, event, eventyay_room_id, payload_dict)

    if "target_languages" in payload_dict and payload_dict["target_languages"] is not None:
        requested_langs = set(payload_dict["target_languages"])
        await _sync_target_languages_and_booths(db, event, room, eventyay_room_id, requested_langs, token)

    # Audit Logging
    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        action=action,
        request_path=f"/api/v1/events/{event_slug}/rooms/{eventyay_room_id}",
        status_code=status_code_ret,
    )
    db.add(audit)

    await db.flush()

    returned_booths = await _build_booths_response(db, event.slug, room.id)

    return {"status": "success", "room_id": room.id, "booths": returned_booths}


@router.delete("/events/{event_slug}/rooms/{eventyay_room_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_room(
    event_slug: str,
    eventyay_room_id: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
):
    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, manage=True, lock_event=True, write_lock=True)

    room_res = await db.execute(
        select(Room).where(Room.event_id == event.id, Room.eventyay_room_id == eventyay_room_id)
    )
    room = room_res.scalars().first()
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")

    room_booths = [b for b in await booths.list_booths_for_event(event_slug) if b["room_id"] == room.id]

    has_active_session = any(b.get("ingest_status") == "connected" for b in room_booths)
    if has_active_session:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Cannot delete room while active booths are running."
        )

    for b in room_booths:
        await booths.remove_booth(event_slug, b["room_id"], b["language_code"])

    await revoke_booths_for_deleted_entity(db, level="room", target_id=room.id)
    await delete_db_room(db, room.id)

    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        action="room.deleted",
        request_path=f"/api/v1/events/{event_slug}/rooms/{eventyay_room_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    db.add(audit)
    await db.flush()
    return None


async def _patch_room_settings(
    event_slug: str,
    room_id: int | str,
    payload: RoomSettingsUpdate,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
    *,
    room_id_kind: Literal["auto", "internal", "eventyay"],
    request_room_id: int | str,
):
    """Apply a validated partial update to an explicitly identified room."""

    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(
        db,
        token,
        event,
        room_id,
        manage=True,
        lock_event=True,
        write_lock=True,
        room_id_kind=room_id_kind,
        reject_ambiguous_room_id=room_id_kind == "auto",
    )
    if room is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found")

    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one room setting is required")

    null_forbidden = sorted(field for field in updates if field in _NON_NULL_ROOM_SETTINGS and updates[field] is None)
    if null_forbidden:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"These room settings cannot be null: {', '.join(null_forbidden)}",
        )

    if "jitsi_url" in updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Meeting URLs must be changed through the dedicated meeting-source workflow",
        )

    if "relay_booth_id" in updates and updates["relay_booth_id"] is not None:
        relay_booth = await db.scalar(
            select(DBBooth)
            .join(Room, Room.id == DBBooth.room_id)
            .where(
                DBBooth.id == updates["relay_booth_id"],
                DBBooth.event_id == event.id,
                Room.event_id == event.id,
            )
        )
        if relay_booth is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Relay booth not found")

    _validate_transcription_settings(event, room, updates)
    await _validate_translation_settings(db, event, room, updates)
    await _validate_tts_settings(db, event, room, updates)

    for field, value in updates.items():
        setattr(room, field, getattr(value, "value", value))

    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=token.client_id,
        event_id=event.id,
        action="room.settings_updated",
        request_path=f"/api/v1/events/{event_slug}/rooms/{request_room_id}/settings",
        status_code=status.HTTP_200_OK,
    )
    db.add(audit)
    await db.flush()
    await db.commit()
    if _TTS_ROOM_SETTINGS.intersection(updates):
        invalidate_tts_config(room.id)

    return {
        "status": "success",
        "room_id": room.id,
        "settings": {field: getattr(room, field) for field in updates},
    }


@router.patch("/events/{event_slug}/rooms/{room_id}/settings")
async def patch_room_settings(
    event_slug: str,
    room_id: Annotated[str, Path(min_length=1, max_length=200)],
    payload: RoomSettingsUpdate,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
):
    """Update an unambiguous legacy room identifier, preserving Eventyay-first lookup."""

    return await _patch_room_settings(
        event_slug,
        room_id,
        payload,
        db,
        token,
        room_id_kind="auto",
        request_room_id=room_id,
    )


@router.patch("/events/{event_slug}/rooms/internal/{room_id}/settings")
async def patch_internal_room_settings(
    event_slug: str,
    room_id: Annotated[int, Path(ge=1, le=_MAX_DATABASE_ID)],
    payload: RoomSettingsUpdate,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
):
    """Update a room using its explicit internal primary-key identifier."""

    return await _patch_room_settings(
        event_slug,
        room_id,
        payload,
        db,
        token,
        room_id_kind="internal",
        request_room_id=f"internal/{room_id}",
    )


@router.patch("/events/{event_slug}/rooms/eventyay/{eventyay_room_id}/settings")
async def patch_eventyay_room_settings(
    event_slug: str,
    eventyay_room_id: Annotated[str, Path(min_length=1, max_length=200)],
    payload: RoomSettingsUpdate,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("rooms:write")),
):
    """Update a room using its explicit Eventyay room identifier."""

    return await _patch_room_settings(
        event_slug,
        eventyay_room_id,
        payload,
        db,
        token,
        room_id_kind="eventyay",
        request_room_id=f"eventyay/{eventyay_room_id}",
    )


@router.get("/events/{event_slug}/rooms/{room_id}/booths")
async def list_booths(
    event_slug: str,
    room_id: int | str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("booths:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(db, token, event, room_id, lock_event=True)
    target_room_id = _extract_room_id(room, room_id)

    result = await db.execute(select(DBBooth).where(DBBooth.room_id == target_room_id, DBBooth.event_id == event.id))
    booths_list = result.scalars().all()

    return [
        {
            "id": b.id,
            "language_code": b.language_code,
            "whip_path": make_mediamtx_path(event.slug, target_room_id, b.language_code),
            "created_at": b.created_at.isoformat(),
        }
        for b in booths_list
    ]


@router.get("/events/{event_slug}/rooms/{room_id}/booths/{language_code}")
async def get_booth(
    event_slug: str,
    room_id: int | str,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("booths:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(db, token, event, room_id, lock_event=True)
    target_room_id = _extract_room_id(room, room_id)
    language_code = _validate_lang(language_code)

    result = await db.execute(
        select(DBBooth).where(
            DBBooth.room_id == target_room_id, DBBooth.language_code == language_code, DBBooth.event_id == event.id
        )
    )
    booth = result.scalars().first()
    if not booth:
        raise HTTPException(status_code=404, detail="Booth not found")

    return {
        "id": booth.id,
        "language_code": booth.language_code,
        "whip_path": make_mediamtx_path(event.slug, target_room_id, booth.language_code),
        "created_at": booth.created_at.isoformat(),
    }


@router.post("/events/{event_slug}/rooms/{room_id}/booths/{language_code}")
async def create_booth(
    event_slug: str,
    room_id: int | str,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("booths:write")),
):
    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(db, token, event, room_id, manage=True, lock_event=True, write_lock=True)
    target_room_id = _extract_room_id(room, room_id)
    language_code = _validate_lang(language_code)
    if language_code == "floor":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'floor' is not a valid booth language for a room.",
        )

    result = await db.execute(
        select(DBBooth).where(DBBooth.room_id == target_room_id, DBBooth.language_code == language_code)
    )
    if result.scalars().first():
        raise HTTPException(status_code=409, detail="Booth already exists")

    from portal.booth_identity import make_mediamtx_path

    booth = DBBooth(room_id=target_room_id, language_code=language_code, event_id=event.id, language_name=language_code)
    db.add(booth)
    await db.flush()

    whip_path = make_mediamtx_path(event.slug, target_room_id, language_code)
    return {"status": "success", "booth_id": booth.id, "whip_path": whip_path}


@router.delete("/events/{event_slug}/rooms/{room_id}/booths/{language_code}")
async def delete_booth_endpoint(
    event_slug: str,
    room_id: int | str,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("booths:write")),
):
    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(db, token, event, room_id, manage=True, lock_event=True, write_lock=True)
    target_room_id = _extract_room_id(room, room_id)
    language_code = _validate_lang(language_code)

    result = await db.execute(
        select(DBBooth).where(
            DBBooth.room_id == target_room_id, DBBooth.language_code == language_code, DBBooth.event_id == event.id
        )
    )
    booth = result.scalars().first()
    if not booth:
        raise HTTPException(status_code=404, detail="Booth not found")

    from portal.database import delete_booth

    await revoke_booths_for_deleted_entity(db, level="booth", target_id=booth.id)
    await delete_booth(db, booth.id)
    return {"status": "deleted"}


@router.post("/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcription/start")
async def start_transcription(
    event_slug: str,
    room_id: int | str,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("sessions:manage")),
):
    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(db, token, event, room_id, lock_event=True, write_lock=True)
    target_room_id = _extract_room_id(room, room_id)
    language_code = _validate_lang(language_code)

    booth_id = make_booth_id(event_slug, target_room_id, language_code)

    # Actually start transcription using the registry
    if booth_id not in booths:
        raise HTTPException(status_code=400, detail="Booth not active in memory")

    # We call the real worker
    await start_transcription_worker(booth_id, event.id)

    return {"status": "started", "booth_id": booth_id}


@router.post("/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcription/stop")
async def stop_transcription(
    event_slug: str,
    room_id: int | str,
    language_code: str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("sessions:manage")),
):
    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(db, token, event, room_id, lock_event=True, write_lock=True)
    target_room_id = _extract_room_id(room, room_id)
    language_code = _validate_lang(language_code)

    booth_id = make_booth_id(event_slug, target_room_id, language_code)
    await stop_transcription_worker(booth_id)
    return {"status": "stopped", "booth_id": booth_id}


@router.get("/events/{event_slug}/rooms/{room_id}/status")
async def get_transcription_status(
    event_slug: str,
    room_id: int | str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("sessions:read")),
):
    result = await db.execute(select(Event).where(Event.slug == event_slug))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(db, token, event, room_id, lock_event=True)
    target_room_id = _extract_room_id(room, room_id)

    # Collect statuses for all booths in the room
    result = await db.execute(select(DBBooth).where(DBBooth.room_id == target_room_id, DBBooth.event_id == event.id))
    booths_list = result.scalars().all()

    statuses = {}
    for b in booths_list:
        bid = make_booth_id(event_slug, target_room_id, b.language_code)
        statuses[b.language_code] = {
            "is_active": booths.get_booth_sync(bid) is not None,
            # Running transcription is tracked by the worker registry, not on the
            # in-memory Booth, which has no transcription_task attribute.
            "transcription_running": bid in active_workers,
        }

    return {"room_id": target_room_id, "statuses": statuses}


def _format_timestamp(dt: datetime) -> str:
    """Normalize naive datetimes to UTC before formatting as ISO 8601 string."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


@router.get("/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcripts/export")
async def export_transcript(
    event_slug: str,
    room_id: int | str,
    language_code: str,
    target_lang: Annotated[
        str | None,
        Query(
            min_length=1,
            description="Return translations in this language instead of source segments",
        ),
    ] = None,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("transcripts:read")),
):
    """Export source transcript segments or translations for a room booth in chronological order."""
    target_lang = target_lang if isinstance(target_lang, str) else None

    result = await db.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
    event = result.scalars().first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    room = await _verify_token_rbac(db, token, event, room_id, lock_event=True)
    target_room_id = _extract_room_id(room, room_id)
    language_code = _validate_lang(language_code)

    # Validate that the booth exists within this event/room
    result = await db.execute(
        select(DBBooth).where(
            DBBooth.room_id == target_room_id,
            DBBooth.language_code == language_code,
            DBBooth.event_id == event.id,
        )
    )
    booth = result.scalars().first()
    if not booth:
        raise HTTPException(status_code=404, detail="Booth not found")

    if target_lang:
        stmt = (
            select(TranscriptSegment, TranscriptTranslation)
            .join(TranscriptSegment, TranscriptTranslation.segment_id == TranscriptSegment.id)
            .where(
                TranscriptSegment.room_id == target_room_id,
                TranscriptSegment.language_code == language_code,
                TranscriptTranslation.language_code == target_lang,
            )
            .order_by(TranscriptSegment.created_at, TranscriptSegment.id)
        )
        result = await db.execute(stmt)
        segments = [
            {"id": seg.id, "text": tr.text, "created_at": _format_timestamp(seg.created_at)} for seg, tr in result.all()
        ]
    else:
        stmt = (
            select(TranscriptSegment)
            .where(
                TranscriptSegment.room_id == target_room_id,
                TranscriptSegment.language_code == language_code,
            )
            .order_by(TranscriptSegment.created_at, TranscriptSegment.id)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        segments = [{"id": r.id, "text": r.text, "created_at": _format_timestamp(r.created_at)} for r in rows]

    return {
        "event_slug": event_slug,
        "room_id": target_room_id,
        "language_code": language_code,
        "target_lang": target_lang,
        "segments": segments,
    }


@router.post("/events/{event_slug}/rooms/{room_id}/listener-token")
async def provision_listener_token(
    event_slug: str,
    room_id: int | str,
    db: AsyncSession = Depends(get_db_session),
    token: OAuthToken = Depends(require_oauth_scope("listeners:provision")),
):
    event = await _get_event_for_request(db, event_slug, write_lock=True)
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, room_id, lock_event=True, write_lock=True)

    from portal.auth import create_listener_token

    t = create_listener_token(event_slug=event.slug)

    return {"listener_token": t}


class EventAPIKeysUpdate(BaseModel):
    openai_api_key: str | None = None
    deepgram_api_key: str | None = None
    nvidia_api_key: str | None = None
    elevenlabs_api_key: str | None = None
    translation_openai_api_key: str | None = None
    openrouter_api_key: str | None = None
    gemini_api_key: str | None = None
    anthropic_api_key: str | None = None
    groq_api_key: str | None = None
    atlascloud_api_key: str | None = None


@router.patch("/events/{event_slug}/api_keys", response_model=dict)
async def update_event_api_keys(
    event_slug: str,
    payload: EventAPIKeysUpdate,
    request: Request,
    token: OAuthToken = Depends(require_oauth_scope("events:write")),
    db: AsyncSession = Depends(get_db_session),
):
    from portal.crypto import encrypt_val

    event = await _get_event_for_request(db, event_slug, write_lock=True)

    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    await _verify_token_rbac(db, token, event, lock_event=True, write_lock=True)

    payload_data = payload.model_dump()
    for payload_field, event_attr in _API_KEY_FIELDS:
        value = payload_data[payload_field]
        if value is not None:
            setattr(event, event_attr, encrypt_val(value) if value else None)

    await db.commit()
    return {"status": "ok"}
