"""Guided room setup: the spoken language, listener languages and how each is delivered.

One form describes a room in plain terms (who speaks, which languages listeners pick,
whether each is a human interpreter, AI or both, captions and an AI voice) and this
module turns it into the booths and floor caption/translation/voice settings the rest
of VoxBento runs on. The same plan creates a room and edits an existing one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from importlib.util import find_spec

import pycountry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import FormData

from portal.config import settings
from portal.database import create_booth, delete_booth, list_booths_for_room
from portal.models import DBBooth, Event, Room, RoomTranslationLanguage
from portal.roles import INTERPRETER
from portal.translations.constants import NLLB_LANGUAGE_MAP
from portal.tts.constants import DEEPGRAM_VOICE_MAPPING, SUPERTONIC_SUPPORTED_LANGS
from portal.utils import _make_jitsi_url

AI = "ai"
BOTH = "both"
DELIVERIES = (INTERPRETER, AI, BOTH)
DELIVERY_LABELS = {
    INTERPRETER: "Interpreter",
    AI: "AI",
    BOTH: "Interpreter + AI",
}

# Room-level answer to "How will this room be interpreted?". "both" lets each
# language choose; the other two apply one delivery to every language.
MODE_INTERPRETERS = "interpreters"
MODE_AI = "ai"
MODE_BOTH = "both"
MODES = (MODE_INTERPRETERS, MODE_AI, MODE_BOTH)

MAX_ROOM_NAME = 200
MAX_LANGUAGES = 40

COMMON_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("zh", "Chinese"),
    ("ar", "Arabic"),
    ("pt", "Portuguese"),
    ("ru", "Russian"),
    ("ja", "Japanese"),
    ("hi", "Hindi"),
    ("it", "Italian"),
    ("ko", "Korean"),
    ("nl", "Dutch"),
    ("tr", "Turkish"),
    ("pl", "Polish"),
    ("uk", "Ukrainian"),
    ("vi", "Vietnamese"),
    ("id", "Indonesian"),
    ("th", "Thai"),
    ("sv", "Swedish"),
)


def _build_language_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for lang in pycountry.languages:
        code = getattr(lang, "alpha_2", None)
        if code:
            # Prefer the everyday name ("Greek" over "Modern Greek (1453-)").
            names[code] = getattr(lang, "common_name", None) or re.sub(r"\s*\(.*\)$", "", lang.name)
    names.update(dict(COMMON_LANGUAGES))
    return names


LANGUAGE_NAMES: dict[str, str] = _build_language_names()
ALL_LANGUAGES: tuple[tuple[str, str], ...] = tuple(sorted(LANGUAGE_NAMES.items(), key=lambda item: item[1]))


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code, code.upper())


# ── AI engines ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Engine:
    provider: str
    model: str
    label: str
    note: str


@dataclass(frozen=True)
class EngineCatalog:
    """The engines an event can use right now, recommended first, and the providers still locked."""

    speech: tuple[Engine, ...]
    translation: tuple[Engine, ...]
    voice: tuple[Engine, ...]
    locked: tuple[str, ...]
    unavailable: tuple[str, ...] = ()

    def find(self, kind: str, provider: str) -> Engine | None:
        return next((e for e in getattr(self, kind) if e.provider == provider), None)


_KEY_ADDED = "API key added"
_SELF_HOSTED = "Runs on this server, no API key; downloads a model the first time"

# (provider, default model, label, event key attribute). Cloud first: they are faster
# and more accurate than the self-hosted fallback when the event has a key for them.
_SPEECH_ENGINES = (
    ("deepgram", "nova-2", "Deepgram Nova", "encrypted_deepgram_api_key"),
    ("elevenlabs", "scribe_v2_realtime", "ElevenLabs Scribe", "encrypted_elevenlabs_api_key"),
    ("openai", "whisper-1", "OpenAI Whisper", "encrypted_openai_api_key"),
    ("nvidia", "parakeet-rnnt", "NVIDIA Parakeet", "encrypted_nvidia_api_key"),
)
_TRANSLATION_ENGINES = (
    ("openai", "gpt-4o-mini", "OpenAI", "encrypted_translation_openai_api_key"),
    ("anthropic", "claude-3-5-haiku-20241022", "Anthropic Claude", "encrypted_anthropic_api_key"),
    ("gemini", "gemini-2.5-flash", "Google Gemini", "encrypted_gemini_api_key"),
    ("groq", "openai/gpt-oss-120b", "Groq", "encrypted_groq_api_key"),
    ("openrouter", "meta-llama/llama-3.3-70b-instruct", "OpenRouter", "encrypted_openrouter_api_key"),
)


_BRANDS = {
    "deepgram": "Deepgram",
    "elevenlabs": "ElevenLabs",
    "openai": "OpenAI",
    "nvidia": "NVIDIA",
    "anthropic": "Anthropic",
    "gemini": "Google Gemini",
    "groq": "Groq",
    "openrouter": "OpenRouter",
}


def engine_catalog(event: Event) -> EngineCatalog:
    """List the engines this event can use, so the setup only offers choices that will work."""
    speech: list[Engine] = []
    translation: list[Engine] = []
    voice: list[Engine] = []
    locked: list[str] = []
    unavailable: list[str] = []

    for provider, model, label, key_attr in _SPEECH_ENGINES:
        if provider == "nvidia" and (find_spec("riva") is None or not settings.nvidia_function_id):
            unavailable.append("NVIDIA needs the server's nvidia extra and NVIDIA_FUNCTION_ID")
            continue
        # Cloud speech recognition also needs the event's switch (see program_ingest.pipeline_problem).
        if getattr(event, key_attr) and event.transcription_api_enabled:
            speech.append(Engine(provider, model, label, _KEY_ADDED))
        else:
            locked.append(_BRANDS[provider])
    speech.append(Engine("local", "tiny", "Whisper, self-hosted", _SELF_HOSTED))

    for provider, model, label, key_attr in _TRANSLATION_ENGINES:
        if getattr(event, key_attr):
            translation.append(Engine(provider, model, label, _KEY_ADDED))
        else:
            locked.append(_BRANDS[provider])
    translation.append(Engine("local", "nllb-200-distilled-600M", "NLLB, self-hosted", _SELF_HOSTED))

    if event.encrypted_deepgram_api_key:
        voice.append(Engine("deepgram", "aura", "Deepgram Aura", _KEY_ADDED))
    else:
        locked.append(_BRANDS["deepgram"])
    if find_spec("supertonic") is not None:
        voice.append(Engine("supertonic", "supertonic", "Supertonic, self-hosted", _SELF_HOSTED))
    else:
        unavailable.append("Supertonic needs the server's supertonic extra")

    return EngineCatalog(
        tuple(speech), tuple(translation), tuple(voice), tuple(dict.fromkeys(locked)), tuple(unavailable)
    )


# ── The plan ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoomPlan:
    display_name: str
    spoken_language: str
    mode: str
    languages: tuple[tuple[str, str], ...]
    captions: bool
    ai_voice: bool
    speech_engine: str
    translation_engine: str
    voice_engine: str

    @property
    def interpreter_languages(self) -> tuple[str, ...]:
        return tuple(code for code, delivery in self.languages if delivery in (INTERPRETER, BOTH))

    @property
    def ai_languages(self) -> tuple[str, ...]:
        return tuple(code for code, delivery in self.languages if delivery in (AI, BOTH))

    @property
    def uses_speech_recognition(self) -> bool:
        return self.captions or bool(self.ai_languages)

    @property
    def deliveries(self) -> dict[str, str]:
        return dict(self.languages)


@dataclass(frozen=True)
class RoomSnapshot:
    """What a room has now: its plan and which human booths are live."""

    plan: RoomPlan
    booth_codes: tuple[str, ...]
    live_booth_codes: frozenset[str] = field(default_factory=frozenset)


def room_mode(languages: Sequence[tuple[str, str]]) -> str:
    deliveries = {delivery for _, delivery in languages}
    if deliveries == {INTERPRETER}:
        return MODE_INTERPRETERS
    if deliveries == {AI}:
        return MODE_AI
    return MODE_BOTH if deliveries else ""


def _first(engines: Sequence[Engine]) -> str:
    return engines[0].provider if engines else ""


def blank_plan(catalog: EngineCatalog) -> RoomPlan:
    return RoomPlan(
        display_name="",
        spoken_language="en",
        mode="",
        languages=(),
        captions=True,
        ai_voice=bool(catalog.voice),
        speech_engine=_first(catalog.speech),
        translation_engine=_first(catalog.translation),
        voice_engine=_first(catalog.voice),
    )


def _available_or_first(engines: Sequence[Engine], current: str | None) -> str:
    if current and any(e.provider == current for e in engines):
        return current
    return _first(engines)


async def load_snapshot(
    session: AsyncSession, room: Room, catalog: EngineCatalog, live_codes: Iterable[str] = ()
) -> RoomSnapshot:
    """Describe an existing room as a plan, so the setup form opens with what it has today."""
    booths = await list_booths_for_room(session, room.id)
    booth_codes = tuple(b.language_code for b in booths)
    ai_codes: tuple[str, ...] = ()
    if room.floor_translation_enabled:
        ai_codes = tuple(
            await session.scalars(
                select(RoomTranslationLanguage.language_code)
                .where(RoomTranslationLanguage.room_id == room.id, RoomTranslationLanguage.enabled.is_(True))
                .order_by(RoomTranslationLanguage.id)
            )
        )
    languages: list[tuple[str, str]] = []
    for code in dict.fromkeys((*booth_codes, *ai_codes)):
        human, machine = code in booth_codes, code in ai_codes
        languages.append((code, BOTH if human and machine else (INTERPRETER if human else AI)))
    plan = RoomPlan(
        display_name=room.display_name,
        spoken_language=room.floor_language_code or "en",
        mode=room_mode(languages) or (MODE_INTERPRETERS if room.floor_transcription_enabled else ""),
        languages=tuple(languages),
        captions=room.floor_transcription_enabled,
        ai_voice=bool(catalog.voice) and (room.floor_tts_enabled or not room.floor_translation_enabled),
        speech_engine=_available_or_first(catalog.speech, room.floor_transcription_provider),
        translation_engine=_available_or_first(catalog.translation, room.floor_translation_provider),
        voice_engine=_available_or_first(catalog.voice, room.floor_tts_provider),
    )
    return RoomSnapshot(plan, booth_codes, frozenset(live_codes))


def parse_plan(
    form: FormData, catalog: EngineCatalog, *, existing_booth_codes: Iterable[str] = ()
) -> tuple[RoomPlan, tuple[str, ...]]:
    """Read the setup form. Always returns a plan (to re-show the form) plus any errors."""
    errors: list[str] = []
    existing_booths = set(existing_booth_codes)

    display_name = str(form.get("display_name", "")).strip()[:MAX_ROOM_NAME]
    if not display_name:
        errors.append("Give the room a name.")

    spoken = str(form.get("spoken_language", "")).strip().lower()
    if spoken not in LANGUAGE_NAMES:
        errors.append("Choose the language spoken in the room.")

    mode = str(form.get("mode", "")).strip()
    if mode not in MODES:
        errors.append("Choose how the room will be interpreted: human interpreters, AI, or both.")

    languages: list[tuple[str, str]] = []
    for raw in form.getlist("languages"):
        code = str(raw).strip().lower()
        if not code or code in dict(languages):
            continue
        if code not in LANGUAGE_NAMES:
            errors.append("One of the chosen languages isn't recognised.")
            continue
        if code == spoken and code not in existing_booths:
            # Listeners already hear the room's own language; it needs no channel.
            continue
        if mode == MODE_BOTH:
            delivery = str(form.get(f"delivery_{code}", BOTH)).strip()
            delivery = delivery if delivery in DELIVERIES else BOTH
        else:
            delivery = AI if mode == MODE_AI else INTERPRETER
        languages.append((code, delivery))
    if len(languages) > MAX_LANGUAGES:
        errors.append(f"A room can offer up to {MAX_LANGUAGES} languages.")
        languages = languages[:MAX_LANGUAGES]

    ai_languages = any(delivery in (AI, BOTH) for _, delivery in languages)
    # AI interpretation is made from the live captions, so they are on whenever it is.
    captions = form.get("captions") == "on" or ai_languages
    ai_voice = form.get("ai_output", "voice") != "captions"
    if not languages and not captions:
        errors.append("Choose at least one listener language, or turn on captions.")

    plan = RoomPlan(
        display_name=display_name,
        spoken_language=spoken if spoken in LANGUAGE_NAMES else "en",
        mode=mode if mode in MODES else "",
        languages=tuple(languages),
        captions=captions,
        ai_voice=ai_voice,
        speech_engine=str(form.get("speech_engine", "")).strip(),
        translation_engine=str(form.get("translation_engine", "")).strip(),
        voice_engine=str(form.get("voice_engine", "")).strip(),
    )

    if plan.uses_speech_recognition and catalog.find("speech", plan.speech_engine) is None:
        errors.append("Choose a speech recognition service for captions.")
    if ai_languages and catalog.find("translation", plan.translation_engine) is None:
        errors.append("Choose a translation service for the AI languages.")
    if ai_languages and plan.ai_voice and catalog.find("voice", plan.voice_engine) is None:
        errors.append("Choose an available voice for the AI languages, or use captions only.")
    if ai_languages and plan.translation_engine == "local":
        unsupported = [
            language_name(code)
            for code in dict.fromkeys((plan.spoken_language, *plan.ai_languages))
            if not pycountry.languages.get(alpha_2=code)
            or pycountry.languages.get(alpha_2=code).name not in NLLB_LANGUAGE_MAP
        ]
        if unsupported:
            errors.append(
                f"Local translation doesn't support {', '.join(unsupported)}. "
                "Choose another translation service or change the AI languages."
            )
    if ai_languages and plan.ai_voice and catalog.find("voice", plan.voice_engine):
        supported = DEEPGRAM_VOICE_MAPPING if plan.voice_engine == "deepgram" else SUPERTONIC_SUPPORTED_LANGS
        unsupported = [language_name(code) for code in plan.ai_languages if code not in supported]
        if unsupported:
            errors.append(
                f"The selected AI voice doesn't support {', '.join(unsupported)}. "
                "Choose another voice service or use captions only."
            )
    return plan, tuple(errors)


# ── Applying a plan ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApplyResult:
    added_booths: tuple[str, ...]
    removed_booths: tuple[str, ...]
    kept_live_booths: tuple[str, ...]


def default_jitsi_url(event_slug: str, display_name: str) -> str:
    clean_name = re.sub("[^a-zA-Z0-9]+", "", display_name)
    return _make_jitsi_url(settings.effective_jitsi_base_url, f"Voxbento-{event_slug}-{clean_name}")


async def apply_plan(
    session: AsyncSession,
    event: Event,
    room: Room,
    plan: RoomPlan,
    catalog: EngineCatalog,
    live_booth_codes: Iterable[str] = (),
) -> ApplyResult:
    """Make the room match the plan. Booths that are live right now are never removed."""
    room.display_name = plan.display_name
    room.floor_language_code = plan.spoken_language

    live = set(live_booth_codes)
    wanted = set(plan.interpreter_languages)
    existing: list[DBBooth] = await list_booths_for_room(session, room.id)
    existing_codes = {b.language_code for b in existing}
    added = [code for code in plan.interpreter_languages if code not in existing_codes]
    removed: list[str] = []
    kept_live: list[str] = []
    for booth in existing:
        if booth.language_code in wanted:
            continue
        if booth.language_code in live:
            kept_live.append(booth.language_code)
        else:
            await delete_booth(session, booth.id)
            removed.append(booth.language_code)
    for code in added:
        await create_booth(
            session, event_id=event.id, room_id=room.id, language_code=code, language_name=language_name(code)
        )

    room.floor_transcription_enabled = plan.uses_speech_recognition
    if plan.uses_speech_recognition:
        speech = catalog.find("speech", plan.speech_engine)
        if speech and (speech.provider != room.floor_transcription_provider or not room.floor_transcription_model):
            room.floor_transcription_provider = speech.provider
            room.floor_transcription_model = speech.model

    ai_codes = plan.ai_languages
    room.floor_translation_enabled = bool(ai_codes)
    if ai_codes:
        translation = catalog.find("translation", plan.translation_engine)
        # Keep a model chosen on the Captions tab while the provider stays the same.
        if translation and (
            translation.provider != room.floor_translation_provider or not room.floor_translation_model
        ):
            room.floor_translation_provider = translation.provider
            room.floor_translation_model = translation.model
    await _sync_translation_languages(session, room.id, ai_codes)

    room.floor_tts_enabled = bool(ai_codes) and plan.ai_voice
    if room.floor_tts_enabled:
        voice = catalog.find("voice", plan.voice_engine)
        if voice:
            room.floor_tts_provider = voice.provider

    await session.flush()
    return ApplyResult(tuple(added), tuple(removed), tuple(kept_live))


def _nllb_language_name(code: str) -> str:
    lang = pycountry.languages.get(alpha_2=code)
    return lang.name if lang else language_name(code)


async def _sync_translation_languages(session: AsyncSession, room_id: int, codes: Sequence[str]) -> None:
    rows = await session.scalars(select(RoomTranslationLanguage).where(RoomTranslationLanguage.room_id == room_id))
    existing = {row.language_code: row for row in rows}
    wanted = set(codes)
    for code, row in existing.items():
        row.enabled = code in wanted
    for code in codes:
        if code not in existing:
            session.add(
                RoomTranslationLanguage(
                    room_id=room_id,
                    language_code=code,
                    language_name=_nllb_language_name(code),
                    enabled=True,
                )
            )


# ── Summaries for the room pages ─────────────────────────────────────────


@dataclass(frozen=True)
class LanguageLine:
    code: str
    name: str
    delivery: str
    label: str


@dataclass(frozen=True)
class RoomSummary:
    spoken_language: str | None
    captions: bool
    ai_voice: bool
    languages: tuple[LanguageLine, ...]

    @property
    def mode_label(self) -> str:
        if not self.languages and self.captions:
            return "Captions only"
        mode = room_mode([(line.code, line.delivery) for line in self.languages])
        return {
            MODE_INTERPRETERS: "Human interpreters",
            MODE_AI: "AI interpretation",
            MODE_BOTH: "Interpreters and AI",
        }.get(mode, "Not set up yet")


def summarize(room: Room, snapshot: RoomSnapshot) -> RoomSummary:
    plan = snapshot.plan
    lines = tuple(
        LanguageLine(code, language_name(code), delivery, DELIVERY_LABELS[delivery])
        for code, delivery in plan.languages
    )
    return RoomSummary(
        spoken_language=language_name(room.floor_language_code) if room.floor_language_code else None,
        captions=plan.captions,
        ai_voice=plan.ai_voice and bool(plan.ai_languages),
        languages=lines,
    )
