"""The home-page live demo on an event room's AI settings.

An Admin can point the demo at one room. The demo then uses that room's speech, translation
and voice services, the API keys its event already holds, its interpreter persona and style,
and its glossary. Nothing new is stored: providers, models and keys are set up, and changed,
in the room's own settings. This module reads them as the room's workers do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.crypto import decrypt_val
from portal.models import AIVocabularyEntry, Event, Room
from portal.transcription.constants import ProviderEnum
from portal.transcription.providers.base import get_api_key
from portal.translations.constants import TranslationProviderEnum
from portal.translations.keys import get_translation_api_key
from portal.translations.vocabulary import VocabularyEntryInput
from portal.tts.providers.base import TTSProviderEnum

#: The built-in translator, used when a room has none chosen.
DEFAULT_TRANSLATION_MODEL = "nllb-200-distilled-600M"
#: Room glossary entries the demo takes, the most important first.
MAX_ROOM_GLOSSARY = 200

_KEYLESS_TRANSLATION = {TranslationProviderEnum.LOCAL.value, TranslationProviderEnum.SELF_HOSTED.value}
_SPEECH_NAMES = {
    "local": "Whisper (built in)",
    "deepgram": "Deepgram",
    "elevenlabs": "ElevenLabs Scribe",
    "openai": "OpenAI",
    "atlascloud": "Atlas Cloud",
    "nvidia": "NVIDIA",
}
_VOICE_NAMES = {"supertonic": "Supertonic (built in)", "deepgram": "Deepgram Aura", "elevenlabs": "ElevenLabs"}


@dataclass(frozen=True)
class RoomAI:
    """What the demo needs from a room. Keys are decrypted here and never leave the server."""

    room_id: int
    event_name: str
    room_name: str
    speech_provider: str
    speech_model: str
    translation_provider: str
    translation_model: str
    #: None when the room has no AI voice.
    voice_provider: str | None
    voice: str
    persona: str | None = None
    style: str | None = None
    glossary: tuple[VocabularyEntryInput, ...] = ()
    #: What stops the demo running on this room, e.g. a missing API key.
    problems: tuple[str, ...] = ()
    speech_key: str | None = field(default=None, repr=False)
    translation_key: str | None = field(default=None, repr=False)
    voice_key: str | None = field(default=None, repr=False)

    @property
    def label(self) -> str:
        return f"{self.event_name} › {self.room_name}"

    @property
    def summary(self) -> str:
        return describe(
            self.speech_provider,
            self.speech_model,
            self.translation_provider,
            self.translation_model,
            self.voice_provider,
        )


def describe(speech: str, speech_model: str, translation: str, translation_model: str, voice: str | None) -> str:
    """ "Captions: Deepgram nova-3 · Translation: openai gpt-4o-mini · Voice: ElevenLabs"."""
    parts = [
        f"Captions: {_SPEECH_NAMES.get(speech, speech)} {speech_model}".strip(),
        f"Translation: {translation} {translation_model}".strip(),
        f"Voice: {_VOICE_NAMES.get(voice, voice)}" if voice else "No voice",
    ]
    return " · ".join(parts)


def _speech(room: Room) -> tuple[str, str]:
    provider = (room.floor_transcription_provider or "local").strip().lower()
    return provider, room.floor_transcription_model or ""


def _translation(room: Room) -> tuple[str, str]:
    provider = room.floor_translation_provider or TranslationProviderEnum.LOCAL.value
    model = room.floor_translation_model or (DEFAULT_TRANSLATION_MODEL if provider == "local" else "")
    return provider, model


def _voice(room: Room) -> str | None:
    return (room.floor_tts_provider or TTSProviderEnum.DEEPGRAM.value) if room.floor_tts_enabled else None


async def _room_glossary(session: AsyncSession, room: Room) -> tuple[VocabularyEntryInput, ...]:
    """The room's and its event's glossary, as the room's own captions use it."""
    if not room.floor_ai_vocabulary_enabled:
        return ()
    rows = await session.scalars(
        select(AIVocabularyEntry)
        .where(
            AIVocabularyEntry.event_id == room.event_id,
            or_(
                (AIVocabularyEntry.room_id == room.id) & AIVocabularyEntry.booth_id.is_(None),
                AIVocabularyEntry.room_id.is_(None) & AIVocabularyEntry.booth_id.is_(None),
            ),
        )
        .order_by(AIVocabularyEntry.priority.desc(), AIVocabularyEntry.room_id.is_(None), AIVocabularyEntry.id)
        .limit(MAX_ROOM_GLOSSARY)
    )
    return tuple(
        VocabularyEntryInput(
            source_term=row.source_term,
            target_language=row.target_language,
            target_term=row.target_term,
            case_sensitive=row.case_sensitive,
            match_type=row.match_type,
            priority=row.priority,
        )
        for row in rows
    )


async def load_room_ai(session: AsyncSession, room_id: int | None) -> RoomAI | None:
    """The room's AI settings and its event's keys, or None when the room no longer exists."""
    if room_id is None:
        return None
    room = await session.get(Room, room_id)
    event = await session.get(Event, room.event_id) if room else None
    if room is None or event is None:
        return None

    problems: list[str] = []
    speech, speech_model = _speech(room)
    speech_key = None
    if speech in {"", "none"}:
        problems.append("The room has no speech recognition service. Choose one in the room's settings.")
    elif speech != "local":
        speech_key = get_api_key(event, ProviderEnum(speech))
        if not speech_key:
            problems.append(f"The event has no {_SPEECH_NAMES.get(speech, speech)} API key.")

    translation, translation_model = _translation(room)
    translation_key = None
    if translation not in _KEYLESS_TRANSLATION:
        translation_key = get_translation_api_key(event, translation)
        if not translation_key:
            problems.append(f"The event has no {translation} API key for translation.")

    voice = _voice(room)
    voice_key = None
    if voice == TTSProviderEnum.DEEPGRAM.value:
        voice_key = decrypt_val(event.encrypted_deepgram_api_key) if event.encrypted_deepgram_api_key else None
    elif voice == TTSProviderEnum.ELEVENLABS.value:
        voice_key = decrypt_val(event.encrypted_elevenlabs_api_key) if event.encrypted_elevenlabs_api_key else None
    if voice in {TTSProviderEnum.DEEPGRAM.value, TTSProviderEnum.ELEVENLABS.value} and not voice_key:
        problems.append(f"The event has no {_VOICE_NAMES[voice]} API key for the voice.")

    return RoomAI(
        room_id=room.id,
        event_name=event.display_name,
        room_name=room.display_name,
        speech_provider=speech,
        speech_model=speech_model,
        translation_provider=translation,
        translation_model=translation_model,
        voice_provider=voice,
        voice=room.floor_tts_voice or "",
        persona=room.floor_ai_interpreter_persona,
        style=room.floor_ai_interpretation_style,
        glossary=await _room_glossary(session, room),
        problems=tuple(problems),
        speech_key=speech_key,
        translation_key=translation_key,
        voice_key=voice_key,
    )


@dataclass(frozen=True)
class RoomChoice:
    room_id: int
    label: str
    summary: str


async def room_choices(session: AsyncSession) -> list[RoomChoice]:
    """Every room an Admin can run the demo on, by event, with the services it uses."""
    rows = await session.execute(
        select(Room, Event.display_name)
        .join(Event, Event.id == Room.event_id)
        .order_by(Event.display_name, Room.display_name)
    )
    choices = []
    for room, event_name in rows:
        speech, speech_model = _speech(room)
        translation, translation_model = _translation(room)
        choices.append(
            RoomChoice(
                room_id=room.id,
                label=f"{event_name} › {room.display_name}",
                summary=describe(speech, speech_model, translation, translation_model, _voice(room)),
            )
        )
    return choices
