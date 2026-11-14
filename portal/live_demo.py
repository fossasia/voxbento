"""Home-page live demo: speak into the browser, get captions, translations and a voice back.

Everything runs on the server's own models, so the demo needs no provider keys:

    browser mic (16 kHz PCM over /ws/demo/live)
      -> energy VAD cuts the speech into utterances
      -> faster-whisper  (partial captions while speaking, a final caption per utterance)
      -> NLLB             (one translation per chosen language, sentence by sentence)
      -> Supertonic       (spoken audio for the language the visitor is listening to)

A glossary steers both models: its terms are given to Whisper as hints so it hears them,
and NLLB keeps them (or uses their set translation) through the glossary placeholders the
booth pipeline already uses. The glossary combines the Admin's site-wide list with the
names and translations a visitor types in.

Admins switch the demo on or off and set its limits at /admin/live-demo/ (one
``live_demo_settings`` row). The ``LIVE_DEMO_*`` environment variables are the defaults
before anything is saved, and ``LIVE_DEMO_ENABLED=false`` switches it off for good on a
server that cannot afford it. The endpoint is public, so the work it can cause is bounded:
a few sessions at once, a time limit per session, a per-IP session budget and a cap on
target languages.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pycountry
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from portal.config import settings
from portal.database import get_session
from portal.models import LiveDemoSettings
from portal.transcription.providers.local import get_model as get_whisper_model
from portal.translations.constants import TranslationProviderEnum
from portal.translations.providers.local import LocalProvider as LocalTranslator
from portal.translations.providers.local import get_model_and_tokenizer
from portal.translations.registry import NLLB_200_DISTILLED_600M
from portal.translations.vocabulary import (
    VocabularyEntryInput,
    VocabularyOverlapIndex,
    VocabularyRowError,
    clean_speech_term,
    find_term_spans,
    validate_vocabulary_entry,
)
from portal.tts.constants import SUPERTONIC_SUPPORTED_LANGS
from portal.tts.providers.supertonic import SupertonicTTSProvider

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
TTS_SAMPLE_RATE = 24000

#: Largest binary frame accepted from the browser (one second of audio).
MAX_FRAME_BYTES = BYTES_PER_SECOND
MAX_NAMES = 12
MAX_NAMES_CHARS = 240
#: Entries a visitor may add, and the longest term or translation they may type.
MAX_VISITOR_GLOSSARY = 10
MAX_VISITOR_TERM_CHARS = 80
#: Entries the Admin's site-wide glossary may hold.
MAX_SITE_GLOSSARY = 100
#: Whisper's prompt holds about 224 tokens; the glossary hints stay well inside it.
MAX_HINT_CHARS = 400

#: Speech models an Admin may pick. Larger ones are too slow on CPU for live captions.
WHISPER_MODELS: dict[str, str] = {
    "tiny": "Tiny: fastest, least accurate",
    "base": "Base: balanced (recommended)",
    "small": "Small: most accurate, needs a fast CPU",
}

#: Allowed range for each numeric setting.
SETTING_LIMITS: dict[str, tuple[int, int]] = {
    "max_sessions": (1, 20),
    "max_session_seconds": (15, 600),
    "sessions_per_ip_per_hour": (1, 500),
    "max_targets": (1, 5),
}

#: Audio kept from before speech starts, so the first syllable is not clipped.
PREROLL_SECONDS = 0.3
#: Silence that ends an utterance.
END_SILENCE_SECONDS = 0.7
#: After this much audio a shorter pause is enough to end the utterance.
SOFT_CUT_SECONDS = 6.0
SOFT_CUT_SILENCE_SECONDS = 0.25
#: An utterance is cut here even mid-word, so captions keep flowing for someone who never pauses.
HARD_CUT_SECONDS = 10.0
#: Voiced audio needed before a partial caption is worth computing.
MIN_PARTIAL_SPEECH_SECONDS = 0.5
PARTIAL_INTERVAL_SECONDS = 0.9
#: A session closes after this long without any message from the browser.
IDLE_TIMEOUT_SECONDS = 20.0

#: RMS (int16) a frame must reach to count as speech, before noise adaptation.
MIN_SPEECH_RMS = 260.0

#: Languages offered in the demo picker, in display order. Each is kept only if the
#: local translation model supports it; ``voice`` follows Supertonic's language list.
DEMO_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("es", "Spanish"),
    ("fr", "French"),
    ("de", "German"),
    ("it", "Italian"),
    ("pt", "Portuguese"),
    ("nl", "Dutch"),
    ("ru", "Russian"),
    ("pl", "Polish"),
    ("tr", "Turkish"),
    ("ar", "Arabic"),
    ("he", "Hebrew"),
    ("hi", "Hindi"),
    ("zh", "Chinese"),
    ("ja", "Japanese"),
    ("ko", "Korean"),
    ("vi", "Vietnamese"),
    ("id", "Indonesian"),
    ("th", "Thai"),
    ("uk", "Ukrainian"),
    ("sv", "Swedish"),
    ("el", "Greek"),
)

#: Hallucinations Whisper produces from breath or room noise; never shown as captions.
_NOISE_TRANSCRIPTS = frozenset(
    {
        "",
        "you",
        "thank you",
        "thank you.",
        "thanks for watching!",
        "thanks for watching.",
        "bye.",
        "...",
    }
)

#: Where one sentence ends and the next begins, in Latin and CJK punctuation.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?。！？])\s+")
#: Where a long sentence can be cut into clauses when its glossary terms got lost.
_CLAUSE_BREAK = re.compile(r"(?<=[,;:、，；])\s*")
_LANGUAGE_PREFIX = re.compile(r"^([A-Za-z]{2})\s*:\s*(.*)$")


class DemoRequestError(ValueError):
    """The browser's start message asked for something the demo does not offer."""


class DemoSettingsError(ValueError):
    """An Admin's settings form had values outside what the demo allows."""


@dataclass(frozen=True)
class DemoLanguage:
    code: str
    name: str
    voice: bool


# ── Settings ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DemoSettings:
    """The demo's effective settings: the saved row, or the environment defaults."""

    enabled: bool
    voice_enabled: bool
    whisper_model: str
    max_sessions: int
    max_session_seconds: int
    sessions_per_ip_per_hour: int
    max_targets: int
    glossary_text: str = ""
    glossary: tuple[VocabularyEntryInput, ...] = ()
    #: False when ``LIVE_DEMO_ENABLED`` switches the demo off on this server.
    server_allowed: bool = True

    @property
    def live(self) -> bool:
        return self.enabled and self.server_allowed

    @property
    def voice(self) -> bool:
        """Whether translations are spoken: switched on and the Supertonic package installed."""
        return self.voice_enabled and voice_installed()


def voice_installed() -> bool:
    return importlib.util.find_spec("supertonic") is not None


def env_defaults() -> DemoSettings:
    return DemoSettings(
        enabled=settings.live_demo_enabled,
        voice_enabled=settings.live_demo_voice_enabled,
        whisper_model=settings.live_demo_whisper_model,
        max_sessions=settings.live_demo_max_sessions,
        max_session_seconds=settings.live_demo_max_session_seconds,
        sessions_per_ip_per_hour=settings.live_demo_sessions_per_ip_per_hour,
        max_targets=3,
        server_allowed=settings.live_demo_enabled,
    )


def settings_from_row(row: LiveDemoSettings | None) -> DemoSettings:
    if row is None:
        return env_defaults()
    glossary, _ = parse_glossary(row.glossary or "")
    return DemoSettings(
        enabled=row.enabled,
        voice_enabled=row.voice_enabled,
        whisper_model=row.whisper_model if row.whisper_model in WHISPER_MODELS else "base",
        max_sessions=row.max_sessions,
        max_session_seconds=row.max_session_seconds,
        sessions_per_ip_per_hour=row.sessions_per_ip_per_hour,
        max_targets=row.max_targets,
        glossary_text=row.glossary or "",
        glossary=tuple(glossary),
        server_allowed=settings.live_demo_enabled,
    )


#: Settings are read for every home-page view, so they are cached briefly; saving replaces the cache.
_SETTINGS_TTL_SECONDS = 10.0
_cached_settings: tuple[float, DemoSettings] | None = None


async def current_settings() -> DemoSettings:
    """The settings in force, cached for a few seconds; the last known ones if the database fails."""
    global _cached_settings
    now = time.monotonic()
    if _cached_settings and now - _cached_settings[0] < _SETTINGS_TTL_SECONDS:
        return _cached_settings[1]
    try:
        async with get_session() as session:
            row = await session.get(LiveDemoSettings, 1)
            value = settings_from_row(row)
    except SQLAlchemyError:
        logger.exception("[LiveDemo] Could not read the demo settings; using the last known ones")
        return _cached_settings[1] if _cached_settings else env_defaults()
    _cached_settings = (now, value)
    return value


def forget_cached_settings() -> None:
    global _cached_settings
    _cached_settings = None


async def load_settings(session: AsyncSession) -> DemoSettings:
    """The saved settings, read fresh (for the admin page)."""
    return settings_from_row(await session.get(LiveDemoSettings, 1))


def _bounded_int(name: str, raw: Any, label: str) -> int:
    low, high = SETTING_LIMITS[name]
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise DemoSettingsError(f"{label} must be a whole number.") from None
    if not low <= value <= high:
        raise DemoSettingsError(f"{label} must be between {low} and {high}.")
    return value


async def save_settings(
    session: AsyncSession,
    *,
    enabled: bool,
    voice_enabled: bool,
    whisper_model: str,
    max_sessions: Any,
    max_session_seconds: Any,
    sessions_per_ip_per_hour: Any,
    max_targets: Any,
    glossary: str,
    editor_id: int,
) -> DemoSettings:
    """Validate and save the Admin's settings. Nothing is changed when any value is refused.

    Raises:
        DemoSettingsError: a value is out of range or a glossary line is invalid.
    """
    if whisper_model not in WHISPER_MODELS:
        raise DemoSettingsError("Choose one of the listed speech models.")
    values = {
        "max_sessions": _bounded_int("max_sessions", max_sessions, "Visitors at once"),
        "max_session_seconds": _bounded_int("max_session_seconds", max_session_seconds, "Time limit"),
        "sessions_per_ip_per_hour": _bounded_int(
            "sessions_per_ip_per_hour", sessions_per_ip_per_hour, "Tries per visitor per hour"
        ),
        "max_targets": _bounded_int("max_targets", max_targets, "Languages per try"),
    }
    glossary = glossary.replace("\r\n", "\n").strip()
    _, errors = parse_glossary(glossary)
    if errors:
        raise DemoSettingsError("Glossary: " + " ".join(errors))

    row = await session.get(LiveDemoSettings, 1)
    if row is None:
        row = LiveDemoSettings(id=1)
        session.add(row)
    row.enabled = enabled
    row.voice_enabled = voice_enabled
    row.whisper_model = whisper_model
    for name, value in values.items():
        setattr(row, name, value)
    row.glossary = glossary
    row.updated_by_id = editor_id
    await session.flush()

    global _cached_settings
    value = settings_from_row(row)
    _cached_settings = (time.monotonic(), value)
    return value


# ── Glossary ──────────────────────────────────────────────────────────────────


def parse_glossary(text: str, *, limit: int = MAX_SITE_GLOSSARY) -> tuple[list[VocabularyEntryInput], list[str]]:
    """Read the Admin's glossary, one entry per line.

    ``VoxBento`` keeps a term as it is in every language; ``keynote = plenary`` translates it
    the same way in every language; ``keynote = fr: plénière`` only into French. Blank lines
    and lines starting with ``#`` are skipped. Returns the entries and a message per bad line.
    """
    entries: list[VocabularyEntryInput] = []
    errors: list[str] = []
    seen = VocabularyOverlapIndex()
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        term, has_translation, rest = (part.strip() for part in line.partition("="))
        language, translation = "all", term
        if has_translation:
            prefixed = _LANGUAGE_PREFIX.match(rest)
            # "fr: …" names a language; any other text before a colon is part of the translation.
            if prefixed and pycountry.languages.get(alpha_2=prefixed.group(1).lower()):
                language, translation = prefixed.group(1).lower(), prefixed.group(2).strip()
            else:
                translation = rest
            if not translation:
                errors.append(
                    f"Line {number}: add a translation after “=”, or remove the “=” to keep the term as it is."
                )
                continue
        try:
            entry = validate_vocabulary_entry(
                {"source_term": term, "target_language": language, "target_term": translation}, number
            )
        except VocabularyRowError as exc:
            errors.append(str(exc).replace("Row", "Line", 1))
            continue
        if seen.conflicts(entry):
            errors.append(f"Line {number}: “{entry.source_term}” is already in the glossary for that language.")
            continue
        if len(entries) >= limit:
            errors.append(f"The glossary can hold {limit} entries.")
            break
        seen.add(entry)
        entries.append(entry)
    return entries, errors


def parse_visitor_glossary(raw: Any, target_codes: set[str]) -> tuple[VocabularyEntryInput, ...]:
    """The term → translation pairs a visitor added in the console.

    Each item is ``{"term", "lang", "translation"}``; ``lang`` is ``all`` or a chosen target,
    and an empty translation keeps the term as it is.

    Raises:
        DemoRequestError: too many entries, or one that is malformed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise DemoRequestError("The glossary must be a list.")
    if len(raw) > MAX_VISITOR_GLOSSARY:
        raise DemoRequestError(f"Add up to {MAX_VISITOR_GLOSSARY} glossary entries.")
    entries: list[VocabularyEntryInput] = []
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise DemoRequestError("Each glossary entry needs a term.")
        term = clean_speech_term(str(item.get("term") or ""))
        translation = clean_speech_term(str(item.get("translation") or "")) or term
        language = str(item.get("lang") or "all").lower()
        if not term:
            raise DemoRequestError("Each glossary entry needs a term.")
        if len(term) > MAX_VISITOR_TERM_CHARS or len(translation) > MAX_VISITOR_TERM_CHARS:
            raise DemoRequestError(f"Keep glossary terms under {MAX_VISITOR_TERM_CHARS} characters.")
        if language != "all" and language not in target_codes:
            raise DemoRequestError("A glossary entry is for a language you haven't chosen.")
        try:
            entries.append(
                validate_vocabulary_entry(
                    {"source_term": term, "target_language": language, "target_term": translation}, number
                )
            )
        except VocabularyRowError as exc:
            raise DemoRequestError(f"Glossary entry {number} can't be used.") from exc
    return tuple(entries)


def merge_glossaries(*sources: Sequence[VocabularyEntryInput]) -> tuple[VocabularyEntryInput, ...]:
    """Combine glossaries, earlier sources winning when they give the same term the same language."""
    seen = VocabularyOverlapIndex()
    merged: list[VocabularyEntryInput] = []
    for source in sources:
        for entry in source:
            if not seen.conflicts(entry):
                seen.add(entry)
                merged.append(entry)
    return tuple(merged)


def entries_for(glossary: Sequence[VocabularyEntryInput], target: str) -> list[VocabularyEntryInput]:
    """The entries that apply to one target language, one per term: its own entry before an "all" one."""
    ordered = [e for e in glossary if e.target_language == target] + [e for e in glossary if e.target_language == "all"]
    seen = VocabularyOverlapIndex(per_language=False)
    chosen: list[VocabularyEntryInput] = []
    for entry in ordered:
        if not seen.conflicts(entry):
            seen.add(entry)
            chosen.append(entry)
    return chosen


def speech_hints(glossary: Sequence[VocabularyEntryInput]) -> tuple[str, ...]:
    """Distinct source terms for Whisper to listen for, first ones first, within its prompt budget."""
    hints: list[str] = []
    used = 0
    folded: set[str] = set()
    for entry in glossary:
        term = clean_speech_term(entry.source_term)
        if not term or term.casefold() in folded:
            continue
        if used + len(term) + 2 > MAX_HINT_CHARS:
            break
        folded.add(term.casefold())
        hints.append(term)
        used += len(term) + 2
    return tuple(hints)


def split_sentences(text: str) -> list[str]:
    return [part for part in (piece.strip() for piece in _SENTENCE_BREAK.split(text)) if part]


def split_clauses(sentence: str) -> list[str]:
    return [part for part in (piece.strip() for piece in _CLAUSE_BREAK.split(sentence)) if part]


def missing_terms(source: str, translated: str, glossary: Sequence[VocabularyEntryInput]) -> int:
    """How many glossary terms said in ``source`` are not in ``translated`` as the glossary wants them."""
    folded = translated.casefold()
    return sum(1 for entry in glossary if find_term_spans(entry, source) and entry.target_term.casefold() not in folded)


# ── Requests ──────────────────────────────────────────────────────────────────


def demo_languages(voice: bool) -> list[DemoLanguage]:
    """The languages the demo can caption and translate, with whether each can be spoken."""
    return [
        DemoLanguage(code, name, voice and code in SUPERTONIC_SUPPORTED_LANGS)
        for code, name in DEMO_LANGUAGES
        if NLLB_200_DISTILLED_600M.engine_code(code)
    ]


def public_config(demo: DemoSettings) -> dict[str, Any]:
    """What the home page needs to draw the demo, without exposing any server detail."""
    if not demo.live:
        return {"enabled": False}
    return {
        "enabled": True,
        "languages": [
            {"code": lang.code, "name": lang.name, "voice": lang.voice} for lang in demo_languages(demo.voice)
        ],
        "max_targets": demo.max_targets,
        "max_seconds": demo.max_session_seconds,
        "sample_rate": SAMPLE_RATE,
        "max_glossary": MAX_VISITOR_GLOSSARY,
        "glossary": [
            {"term": e.source_term, "lang": e.target_language, "translation": e.target_term} for e in demo.glossary
        ],
    }


@dataclass(frozen=True)
class DemoRequest:
    source: DemoLanguage
    targets: tuple[DemoLanguage, ...]
    listen: str | None
    #: Visitor entries first, then the visitor's names, then the site glossary.
    glossary: tuple[VocabularyEntryInput, ...] = ()
    whisper_model: str = "base"

    @property
    def hints(self) -> tuple[str, ...]:
        return speech_hints(self.glossary)


def parse_start(message: dict[str, Any], demo: DemoSettings) -> DemoRequest:
    """Validate a ``start`` message against the offered languages and the demo settings.

    Raises:
        DemoRequestError: an unknown language, too many targets or a malformed glossary.
    """
    by_code = {lang.code: lang for lang in demo_languages(demo.voice)}
    source = by_code.get(str(message.get("source") or ""))
    if source is None:
        raise DemoRequestError("Choose a language to speak.")

    raw_targets = message.get("targets")
    if not isinstance(raw_targets, list):
        raise DemoRequestError("Choose at least one language to translate into.")
    targets: list[DemoLanguage] = []
    for code in raw_targets:
        lang = by_code.get(str(code))
        if lang is None:
            raise DemoRequestError("One of the chosen languages isn't available in the demo.")
        if lang.code != source.code and lang not in targets:
            targets.append(lang)
    if not targets:
        raise DemoRequestError("Choose at least one language other than the one you speak.")
    if len(targets) > demo.max_targets:
        raise DemoRequestError(f"Choose up to {demo.max_targets} languages.")

    names = tuple(
        VocabularyEntryInput(source_term=name, target_language="all", target_term=name)
        for name in parse_names(message.get("names"))
    )
    visitor = parse_visitor_glossary(message.get("glossary"), {t.code for t in targets})
    return DemoRequest(
        source=source,
        targets=tuple(targets),
        listen=_listen_code(message.get("listen"), targets),
        glossary=merge_glossaries(visitor, names, demo.glossary),
        whisper_model=demo.whisper_model,
    )


def parse_names(raw: Any) -> tuple[str, ...]:
    """Split the "names you'll say" box into distinct names, bounded in count and length."""
    if raw is None:
        return ()
    if not isinstance(raw, str):
        raise DemoRequestError("Names must be text.")
    if len(raw) > MAX_NAMES_CHARS:
        raise DemoRequestError(f"Keep names under {MAX_NAMES_CHARS} characters.")
    names: list[str] = []
    for part in raw.replace("\n", ",").split(","):
        name = clean_speech_term(part)
        if name and name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    return tuple(names[:MAX_NAMES])


def _listen_code(raw: Any, targets: Sequence[DemoLanguage]) -> str | None:
    """The target to speak aloud, or None when it has no voice or was not chosen."""
    for lang in targets:
        if lang.code == raw and lang.voice:
            return lang.code
    return None


def frame_rms(frame: bytes) -> float:
    """Root-mean-square level of an int16 PCM frame."""
    if len(frame) < 2:
        return 0.0
    samples = np.frombuffer(frame[: len(frame) // 2 * 2], dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples * samples)))


def clean_transcript(text: str) -> str:
    """Collapse whitespace and drop Whisper's usual noise-only hallucinations."""
    text = " ".join(text.split())
    return "" if text.casefold() in _NOISE_TRANSCRIPTS else text


class Segmenter:
    """Energy-based voice activity detection that cuts incoming PCM into utterances.

    Feed frames with :meth:`push`; it returns a finished utterance when one ends. The noise
    floor adapts to the room so a fan or laptop hum does not count as speech.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.speech_seconds = 0.0
        self.trailing_silence = 0.0
        self.noise_floor = 120.0

    @property
    def seconds(self) -> float:
        return len(self.buffer) / BYTES_PER_SECOND

    @property
    def has_speech(self) -> bool:
        return self.speech_seconds > 0

    def push(self, frame: bytes) -> bytes | None:
        duration = len(frame) / BYTES_PER_SECOND
        rms = frame_rms(frame)
        threshold = max(MIN_SPEECH_RMS, self.noise_floor * 3.0)
        is_speech = rms >= threshold
        if not is_speech:
            self.noise_floor = 0.95 * self.noise_floor + 0.05 * rms

        self.buffer.extend(frame)
        if is_speech:
            self.speech_seconds += duration
            self.trailing_silence = 0.0
        elif self.has_speech:
            self.trailing_silence += duration
        else:
            # Nothing said yet: keep only a short pre-roll.
            excess = len(self.buffer) - int(PREROLL_SECONDS * BYTES_PER_SECOND)
            if excess > 0:
                del self.buffer[: excess - excess % 2]
            return None

        if (
            self.trailing_silence >= END_SILENCE_SECONDS
            or (self.seconds >= SOFT_CUT_SECONDS and self.trailing_silence >= SOFT_CUT_SILENCE_SECONDS)
            or self.seconds >= HARD_CUT_SECONDS
        ):
            return self.flush()
        return None

    def flush(self) -> bytes | None:
        """End the current utterance now; None when it held no speech."""
        audio = bytes(self.buffer) if self.has_speech else None
        self.buffer.clear()
        self.speech_seconds = 0.0
        self.trailing_silence = 0.0
        return audio


# ── Engines ───────────────────────────────────────────────────────────────────

Transcribe = Callable[[bytes, DemoRequest, bool], Awaitable[str]]
Translate = Callable[[str, DemoRequest, DemoLanguage], Awaitable[str | None]]
Synthesize = Callable[[str, str], Awaitable[bytes]]


@dataclass(frozen=True)
class Engines:
    transcribe: Transcribe
    translate: Translate
    synthesize: Synthesize


async def _local_transcribe(audio: bytes, request: DemoRequest, partial: bool) -> str:
    hints = request.hints

    def run() -> str:
        model = get_whisper_model(request.whisper_model)
        samples = np.frombuffer(audio, np.int16).astype(np.float32) / 32768.0
        # A glossary prompt keeps the punctuation and spelling; hotwords make Whisper favour
        # the terms themselves. Together they caught names neither caught alone.
        segments, _ = model.transcribe(
            samples,
            language=request.source.code,
            beam_size=1 if partial else 5,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=f"Glossary: {', '.join(hints)}." if hints else None,
            hotwords=" ".join(hints) if hints else None,
            no_speech_threshold=0.6,
        )
        return " ".join(segment.text.strip() for segment in segments)

    return clean_transcript(await asyncio.to_thread(run))


_translator_instance: LocalTranslator | None = None


def _translator() -> LocalTranslator:
    # Built on first use: its constructor starts the model eviction loop, which needs a running loop.
    global _translator_instance
    if _translator_instance is None:
        _translator_instance = LocalTranslator()
    return _translator_instance


async def _local_translate(text: str, request: DemoRequest, target: DemoLanguage) -> str | None:
    """Translate one sentence at a time, and a sentence clause by clause if it lost a glossary term.

    Given several sentences at once, NLLB-600M often leaves one out, and a missing part takes
    its glossary placeholder with it, so the whole caption would fall back to a translation
    without the glossary. Shorter pieces keep every part and every term.
    """
    glossary = entries_for(request.glossary, target.code)

    async def translate(piece: str) -> str:
        translated = await _translator().translate(
            provider_name=TranslationProviderEnum.LOCAL.value,
            text=piece,
            target_lang_name=target.name,
            target_lang_code=target.code,
            source_lang_name=request.source.name,
            source_lang_code=request.source.code,
            model=settings.live_demo_translation_model,
            api_key=None,
            vocabulary_entries=glossary,
        )
        return translated or ""

    parts: list[str] = []
    for sentence in split_sentences(text):
        translated = await translate(sentence)
        lost = missing_terms(sentence, translated, glossary) if glossary else 0
        clauses = split_clauses(sentence) if lost else []
        if len(clauses) > 1:
            retry = " ".join(part for part in [await translate(clause) for clause in clauses] if part)
            if missing_terms(sentence, retry, glossary) < lost:
                translated = retry
        if translated:
            parts.append(translated)
    return " ".join(parts)


async def _local_synthesize(text: str, language: str) -> bytes:
    chunks: list[bytes] = []

    async def collect(audio: bytes) -> None:
        chunks.append(audio)

    async def one_text() -> AsyncIterator[str]:
        yield text

    await SupertonicTTSProvider().synthesize_stream(
        text_chunks=one_text(), language_code=language, voice="", on_audio=collect
    )
    return b"".join(chunks)


LOCAL_ENGINES = Engines(transcribe=_local_transcribe, translate=_local_translate, synthesize=_local_synthesize)


_warm_lock = asyncio.Lock()
#: (speech model, translation model, voice) combinations already loaded in this process.
_warmed: set[tuple[str, str, bool]] = set()


def _warm_key(demo: DemoSettings) -> tuple[str, str, bool]:
    return (demo.whisper_model, settings.live_demo_translation_model, demo.voice)


def models_ready(demo: DemoSettings) -> bool:
    return _warm_key(demo) in _warmed


def models_loading() -> bool:
    return _warm_lock.locked()


async def warm_up(demo: DemoSettings) -> None:
    """Load (and on first use download) every model once, before the visitor starts talking.

    Otherwise the first sentence would wait for a download mid-run.
    """
    key = _warm_key(demo)
    if key in _warmed:
        return
    async with _warm_lock:
        if key in _warmed:
            return
        await asyncio.to_thread(get_whisper_model, demo.whisper_model)
        await asyncio.to_thread(get_model_and_tokenizer, settings.live_demo_translation_model)
        if demo.voice:
            # A short synthesis loads the engine and the English voice style.
            await _local_synthesize("Hello.", "en")
        _warmed.add(key)


# ── Session ──────────────────────────────────────────────────────────────────

SendJson = Callable[[dict[str, Any]], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]


@dataclass
class _Utterance:
    id: int
    audio: bytes


@dataclass
class LiveDemoSession:
    """One visitor's demo run: audio in, caption/translation/voice events out.

    Transcription and translation run in separate loops so the next sentence is captioned
    while the previous one is still being translated and spoken.
    """

    request: DemoRequest
    send_json: SendJson
    send_bytes: SendBytes
    engines: Engines = LOCAL_ENGINES
    segmenter: Segmenter = field(default_factory=Segmenter)

    def __post_init__(self) -> None:
        self.listen = self.request.listen
        self._next_id = 1
        self._send_lock = asyncio.Lock()
        self._finals: asyncio.Queue[_Utterance | None] = asyncio.Queue()
        self._captions: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        self._partial: _Utterance | None = None
        self._wake = asyncio.Event()
        self._last_partial = 0.0
        self._closing = False
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._transcription_loop()),
            asyncio.create_task(self._translation_loop()),
        ]

    async def _send(self, message: dict[str, Any], audio: bytes | None = None) -> None:
        # The audio header and its bytes must arrive together, never split by another message.
        async with self._send_lock:
            await self.send_json(message)
            if audio is not None:
                await self.send_bytes(audio)

    def set_listen(self, code: Any) -> None:
        self.listen = _listen_code(code, self.request.targets)

    def push_audio(self, frame: bytes) -> None:
        if self._closing:
            return
        utterance = self.segmenter.push(frame)
        if utterance is not None:
            self._queue_final(utterance)
            return
        now = time.monotonic()
        if (
            self.segmenter.speech_seconds >= MIN_PARTIAL_SPEECH_SECONDS
            and now - self._last_partial >= PARTIAL_INTERVAL_SECONDS
        ):
            self._last_partial = now
            self._partial = _Utterance(self._next_id, bytes(self.segmenter.buffer))
            self._wake.set()

    def _queue_final(self, audio: bytes) -> None:
        self._finals.put_nowait(_Utterance(self._next_id, audio))
        self._next_id += 1
        self._partial = None
        self._wake.set()

    async def finish(self) -> None:
        """Caption whatever is still being said, then wait for every result to be sent."""
        if self._closing:
            return
        self._closing = True
        remaining = self.segmenter.flush()
        if remaining is not None:
            self._queue_final(remaining)
        self._finals.put_nowait(None)
        self._wake.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def cancel(self) -> None:
        for task in self._tasks:
            task.cancel()

    async def _transcription_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while not self._finals.empty():
                utterance = self._finals.get_nowait()
                if utterance is None:
                    await self._captions.put(None)
                    return
                text = await self._run_transcribe(utterance.audio, partial=False)
                await self._send({"type": "final", "id": utterance.id, "text": text})
                if text:
                    await self._captions.put((utterance.id, text))
            partial = self._partial
            if partial is not None and partial.id == self._next_id:
                self._partial = None
                text = await self._run_transcribe(partial.audio, partial=True)
                # The utterance may have ended while this ran; its final caption replaces it.
                if text and partial.id == self._next_id and not self._closing:
                    await self._send({"type": "partial", "id": partial.id, "text": text})

    async def _run_transcribe(self, audio: bytes, *, partial: bool) -> str:
        try:
            return await self.engines.transcribe(audio, self.request, partial)
        except Exception:  # one failed chunk must not end the visitor's demo
            logger.exception("[LiveDemo] Transcription failed")
            return ""

    async def _translation_loop(self) -> None:
        while True:
            item = await self._captions.get()
            if item is None:
                return
            caption_id, text = item
            listen = self.listen
            # The language being heard goes first, and its voice is made while the others translate.
            ordered = sorted(self.request.targets, key=lambda target: target.code != listen)
            speech: asyncio.Task[bytes] | None = None
            for target in ordered:
                translated = await self._run_translate(text, target)
                await self._send({"type": "translation", "id": caption_id, "lang": target.code, "text": translated})
                if translated and target.code == listen:
                    speech = asyncio.create_task(self._run_synthesize(translated, target.code))
            if speech is not None:
                audio = await speech
                # Skip it if the visitor switched language while it was being made.
                if audio and listen == self.listen:
                    await self._send(
                        {"type": "audio", "id": caption_id, "lang": listen, "sample_rate": TTS_SAMPLE_RATE},
                        audio,
                    )

    async def _run_translate(self, text: str, target: DemoLanguage) -> str:
        try:
            return (await self.engines.translate(text, self.request, target)) or ""
        except Exception:  # a failed language shows as blank, the others continue
            logger.exception("[LiveDemo] Translation into %s failed", target.code)
            return ""

    async def _run_synthesize(self, text: str, language: str) -> bytes:
        try:
            return await self.engines.synthesize(text, language)
        except Exception:  # captions still work without the voice
            logger.exception("[LiveDemo] Speech synthesis in %s failed", language)
            return b""


# ── Admission ────────────────────────────────────────────────────────────────

_active_sessions = 0


def try_admit(limit: int) -> bool:
    """Reserve a session slot; False when ``limit`` sessions are already running."""
    global _active_sessions
    if _active_sessions >= limit:
        return False
    _active_sessions += 1
    return True


def release() -> None:
    global _active_sessions
    _active_sessions = max(0, _active_sessions - 1)


def active_sessions() -> int:
    return _active_sessions
