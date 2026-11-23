"""Validate a room's fallback against every translation stream it serves."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from portal.models import BoothTranslationLanguage, DBBooth, Event, Room, RoomTranslationLanguage
from portal.translations.model_languages import language_support_error


async def lock_event_translation_settings(session: AsyncSession, event_id: int) -> None:
    """Share the API's event write lock until the settings transaction ends.

    SQLite ignores FOR UPDATE, so a guarded no-op update takes its writer lock.
    All configuration must be read after this call to avoid validating stale choices.
    """
    if session.get_bind().dialect.name == "sqlite":
        await session.execute(
            update(Event)
            .where(Event.id == event_id, Event.deleted_at.is_(None))
            .values(deleted_at=Event.deleted_at)
            .execution_options(synchronize_session=False)
        )
    else:
        await session.execute(select(Event.id).where(Event.id == event_id).with_for_update())


async def fallback_configuration_error(session: AsyncSession, room: Room) -> str | None:
    provider, model = room.translation_fallback_provider, room.translation_fallback_model
    if not provider or not model:
        return None
    fallback = (provider, model)
    if room.floor_translation_enabled:
        if fallback == (room.floor_translation_provider, room.floor_translation_model):
            return "The primary translator and fallback must be different."
        targets = await session.scalars(
            select(RoomTranslationLanguage.language_code).where(
                RoomTranslationLanguage.room_id == room.id, RoomTranslationLanguage.enabled.is_(True)
            )
        )
        error = language_support_error(provider, model, (room.floor_language_code, *targets))
        if error:
            return error
    booths = await session.scalars(
        select(DBBooth).where(DBBooth.room_id == room.id, DBBooth.translation_enabled.is_(True))
    )
    for booth in booths:
        if fallback == (booth.translation_provider, booth.translation_model):
            return f"The {booth.language_code} booth translator and room fallback must be different."
        targets = await session.scalars(
            select(BoothTranslationLanguage.language_code).where(
                BoothTranslationLanguage.booth_id == booth.id, BoothTranslationLanguage.enabled.is_(True)
            )
        )
        error = language_support_error(provider, model, (booth.language_code, *targets))
        if error:
            return f"Room fallback for the {booth.language_code} booth: {error}"
    return None
