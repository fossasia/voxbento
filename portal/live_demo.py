"""Home-page live demo: speak into the browser, get captions, translations and a voice back.

It runs the booth pipeline's own pieces on the server's own models, so it needs no
provider keys and behaves as a room's AI captions do:

    browser mic (16 kHz PCM over /ws/demo/live)
      -> 3 s chunks, silence skipped, a pause finishes the caption
      -> local Whisper (``transcription.providers.local``, as for rooms)
      -> CaptionAggregator (words so far, a finished caption at each sentence end)
      -> local NLLB engine (``translations.providers.local.translate_batch``)
      -> Supertonic (the voice for the language the visitor is listening to)

The glossary combines the Admin's site-wide list with the names and translations a visitor
types in. Whisper gets its terms as a prompt so it spells them as written; NLLB keeps them,
or uses their set translation, through the translation context as a room's glossary does.

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
from portal.transcription.aggregator import CaptionAggregator
from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig
from portal.transcription.providers.local import LocalProvider as LocalWhisper
from portal.transcription.providers.local import get_model as get_whisper_model
from portal.translations.engine import Language, TranslationContext
from portal.translations.providers.local import LocalProvider as LocalTranslator
from portal.translations.providers.local import get_model_and_tokenizer
from portal.translations.registry import NLLB_200_DISTILLED_600M
from portal.translations.vocabulary import (
    VocabularyEntryInput,
    VocabularyOverlapIndex,
    VocabularyRowError,
    clean_speech_term,
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

#: Audio is transcribed exactly as a booth's local Whisper captions are
#: (``portal.transcription.providers.local``): 3 s chunks, each heard with the last second
#: of the one before, keeping only the words that end after that second.
CHUNK_SECONDS = 3.0
OVERLAP_BYTES = BYTES_PER_SECOND  # the provider's one-second overlap
#: Silence that ends what the visitor was saying, so its caption is finished at once.
END_SILENCE_SECONDS = 0.7
#: When transcription falls behind, waiting chunks are transcribed together up to this length.
MAX_MERGED_SECONDS = 9.0
#: Someone who never pauses or punctuates still gets a finished caption this often.
FORCE_FINAL_WORDS = 25
FORCE_FINAL_SECONDS = 8.0
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
            # "fr: …" names a language; any other text before a colon is part of the translation.
            # Split on the colon rather than with a regex: Admin input, kept linear in its length.
            code, has_code, value = rest.partition(":")
            code = code.strip().lower()
            if (
                has_code
                and len(code) == 2
                and code.isascii()
                and code.isalpha()
                and pycountry.languages.get(alpha_2=code)
            ):
                language, translation = code, value.strip()
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


def is_prompt_echo(text: str, hints: Sequence[str]) -> bool:
    """Whether Whisper only repeated its glossary prompt, as it does on noise or silence."""
    words = re.findall(r"\w+", text.casefold())
    if not words or words[0] == "glossary":
        return True
    hint_words = {word for hint in hints for word in re.findall(r"\w+", hint.casefold())}
    return bool(hint_words) and all(word in hint_words for word in words)


@dataclass(frozen=True)
class Chunk:
    """New audio since the last chunk. Silent chunks are not transcribed, but still become the
    next chunk's overlap. ``ends_utterance`` finishes the caption (the visitor paused)."""

    audio: bytes
    has_speech: bool = False
    ends_utterance: bool = False

    def merged_with(self, later: Chunk) -> Chunk:
        """This chunk followed by a later one, so both are transcribed in one call."""
        return Chunk(self.audio + later.audio, self.has_speech or later.has_speech, later.ends_utterance)


class Chunker:
    """Cuts incoming PCM into chunks for Whisper, and marks where the visitor paused.

    Speech is told from silence by level against a noise floor that adapts to the room,
    so a fan or laptop hum does not count.
    """

    CHUNK_BYTES = int(CHUNK_SECONDS * BYTES_PER_SECOND)

    def __init__(self) -> None:
        self.pending = bytearray()
        self.pending_speech = False
        #: Speech heard since the caption was last finished.
        self.speaking = False
        self.trailing_silence = 0.0
        self.noise_floor = 120.0

    def is_speech(self, frame: bytes) -> bool:
        rms = frame_rms(frame)
        if rms >= max(MIN_SPEECH_RMS, self.noise_floor * 3.0):
            return True
        self.noise_floor = 0.95 * self.noise_floor + 0.05 * rms
        return False

    def push(self, frame: bytes) -> Chunk | None:
        self.pending.extend(frame)
        if self.is_speech(frame):
            self.pending_speech = True
            self.speaking = True
            self.trailing_silence = 0.0
        elif self.speaking:
            self.trailing_silence += len(frame) / BYTES_PER_SECOND
        if self.speaking and self.trailing_silence >= END_SILENCE_SECONDS:
            return self.flush()
        if len(self.pending) >= self.CHUNK_BYTES:
            return self._cut(ends_utterance=False)
        return None

    def flush(self) -> Chunk | None:
        """Whatever audio is waiting, finishing the caption; None when nothing was said."""
        if not self.speaking and not self.pending_speech:
            return None
        self.speaking = False
        self.trailing_silence = 0.0
        return self._cut(ends_utterance=True)

    def _cut(self, *, ends_utterance: bool) -> Chunk:
        chunk = Chunk(bytes(self.pending), self.pending_speech, ends_utterance)
        self.pending.clear()
        self.pending_speech = False
        return chunk


# ── Engines ───────────────────────────────────────────────────────────────────


Transcribe = Callable[[bytes, DemoRequest, BoothTranscriptionState], Awaitable[str]]
#: Translates one caption into every target; returns the text per language code.
Translate = Callable[[str, DemoRequest, Sequence[DemoLanguage]], Awaitable[dict[str, str]]]
Synthesize = Callable[[str, str], Awaitable[bytes]]


@dataclass(frozen=True)
class Engines:
    transcribe: Transcribe
    translate: Translate
    synthesize: Synthesize


_whisper = LocalWhisper()


async def _local_transcribe(audio: bytes, request: DemoRequest, state: BoothTranscriptionState) -> str:
    """One chunk through the booth pipeline's local Whisper, with the glossary as its prompt.

    The prompt is the one addition: rooms' local Whisper has none. It keeps names spelled as
    the glossary has them ("Event IA" became "Eventyay", "Vox Bento" "VoxBento").
    """
    hints = request.hints
    text = await _whisper.process_chunk(
        audio,
        request.source.code,
        request.whisper_model,
        ProviderConfig(api_key=None),
        booth_state=state,
        prompt=f"Glossary: {', '.join(hints)}." if hints else None,
    )
    text = clean_transcript(text)
    return "" if text and is_prompt_echo(text, hints) else text


_translator_instance: LocalTranslator | None = None


def _translator() -> LocalTranslator:
    # Built on first use: its constructor starts the model eviction loop, which needs a running loop.
    global _translator_instance
    if _translator_instance is None:
        _translator_instance = LocalTranslator()
    return _translator_instance


async def _local_translate(text: str, request: DemoRequest, targets: Sequence[DemoLanguage]) -> dict[str, str]:
    """Translate as the booth pipeline does: the local engine's ``translate_batch``, with the
    glossary for each language in the translation context."""
    rows = await _translator().translate_batch(
        Language(request.source.code, request.source.name),
        [Language(target.code, target.name) for target in targets],
        [text],
        TranslationContext(vocabulary={target.code: entries_for(request.glossary, target.code) for target in targets}),
        model=settings.live_demo_translation_model,
    )
    return {target.code: translated or "" for target, translated in zip(targets, rows[0], strict=True)}


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


#: The aggregator keys its state by booth; a session has one stream.
_STREAM = "live-demo"


@dataclass
class LiveDemoSession:
    """One visitor's demo run: audio in, caption/translation/voice events out.

    Captions follow the booth pipeline: overlapping chunks go to Whisper once each, and
    ``CaptionAggregator`` shows the words so far and finishes a caption at each sentence end.
    Transcription and translation run in separate loops so the next sentence is captioned
    while the previous one is translated and spoken.
    """

    request: DemoRequest
    send_json: SendJson
    send_bytes: SendBytes
    engines: Engines = LOCAL_ENGINES
    chunker: Chunker = field(default_factory=Chunker)

    def __post_init__(self) -> None:
        self.listen = self.request.listen
        self._next_id = 1
        self._send_lock = asyncio.Lock()
        self._chunks: asyncio.Queue[Chunk | None] = asyncio.Queue()
        self._captions: asyncio.Queue[tuple[int, str] | None] = asyncio.Queue()
        self._aggregator = CaptionAggregator(self._on_caption)
        # The provider drops words inside the first second of every chunk (the overlap), so
        # the stream starts with a second of silence for the first words to survive.
        self._whisper_state = BoothTranscriptionState(booth_id=_STREAM, overlap_buffer=bytes(OVERLAP_BYTES))
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
        chunk = self.chunker.push(frame)
        if chunk is not None:
            self._chunks.put_nowait(chunk)

    async def finish(self) -> None:
        """Caption whatever is still being said, then wait for every result to be sent."""
        if self._closing:
            return
        self._closing = True
        last = self.chunker.flush()
        if last is not None:
            self._chunks.put_nowait(last)
        self._chunks.put_nowait(None)
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def cancel(self) -> None:
        for task in self._tasks:
            task.cancel()

    async def _on_caption(self, _stream: str, message: dict[str, Any]) -> None:
        """Turn the aggregator's caption messages into the demo protocol."""
        text = message.get("text") or ""
        if message.get("status") == "partial":
            await self._send({"type": "partial", "id": self._next_id, "text": text})
        elif message.get("status") == "final" and text:
            caption_id = self._next_id
            self._next_id += 1
            await self._send({"type": "final", "id": caption_id, "text": text})
            await self._captions.put((caption_id, text))

    async def _transcription_loop(self) -> None:
        carry: Chunk | None = None
        while True:
            chunk = carry if carry is not None else await self._chunks.get()
            carry = None
            if chunk is None:
                await self._aggregator.handle_clear(_STREAM)
                await self._captions.put(None)
                return
            # Behind? Transcribe the chunks already waiting together, once, instead of dropping
            # audio as the booth pipeline does when it lags: a visitor wants every word.
            while not chunk.ends_utterance and not self._chunks.empty():
                later = self._chunks.get_nowait()
                if later is None or len(chunk.audio) + len(later.audio) > MAX_MERGED_SECONDS * BYTES_PER_SECOND:
                    carry = later
                    break
                chunk = chunk.merged_with(later)
            if chunk.has_speech:
                text = await self._run_transcribe(chunk.audio)
            else:
                # Not worth a model call; it is still the audio the next chunk overlaps.
                self._whisper_state.overlap_buffer = (self._whisper_state.overlap_buffer + chunk.audio)[-OVERLAP_BYTES:]
                text = ""
            # As in the booth pipeline: words extend the caption, and nothing heard finishes it.
            if text:
                await self._aggregator.handle_chunk(_STREAM, text)
                await self._finish_long_caption()
            if not text or chunk.ends_utterance:
                await self._aggregator.handle_clear(_STREAM)

    async def _finish_long_caption(self) -> None:
        """Finish a caption that has run long without a full stop, so translation can start."""
        state = self._aggregator.states.get(_STREAM)
        if state is None or not state.current_utterance:
            return
        if state.current_word_count >= FORCE_FINAL_WORDS or time.time() - state.utterance_start_time >= (
            FORCE_FINAL_SECONDS
        ):
            await self._aggregator.handle_final(_STREAM, "")

    async def _run_transcribe(self, audio: bytes) -> str:
        try:
            return await self.engines.transcribe(audio, self.request, self._whisper_state)
        except Exception:  # one failed chunk must not end the visitor's demo
            logger.exception("[LiveDemo] Transcription failed")
            return ""

    async def _translation_loop(self) -> None:
        """Translate and speak each caption with the booth pipeline's engines.

        Work runs one model at a time beside Whisper, in the order a listener notices it: the
        language being heard, its voice, then the other languages, one call each. Run at once
        they slowed Whisper below real time. Each row shows only the latest caption, so when
        captions arrive faster than the CPU keeps up, the older ones are skipped and the rows
        and the voice stay with the speaker.
        """
        while True:
            item = await self._captions.get()
            ended = item is None
            while not ended and not self._captions.empty():
                newer = self._captions.get_nowait()
                ended = newer is None
                item = item if ended else newer
            if item is not None:
                await self._translate_and_speak(*item)
            if ended:
                return

    async def _translate_and_speak(self, caption_id: int, text: str) -> None:
        listen = self.listen
        heard = [target for target in self.request.targets if target.code == listen]
        others = [target for target in self.request.targets if target.code != listen]
        for target in heard + others:
            translated = (await self._run_translate(text, [target]))[target.code]
            await self._send({"type": "translation", "id": caption_id, "lang": target.code, "text": translated})
            if target.code == listen and translated:
                audio = await self._run_synthesize(translated, listen)
                # Skip it if the visitor switched language while it was being made.
                if audio and listen == self.listen:
                    await self._send(
                        {"type": "audio", "id": caption_id, "lang": listen, "sample_rate": TTS_SAMPLE_RATE},
                        audio,
                    )

    async def _run_translate(self, text: str, targets: Sequence[DemoLanguage]) -> dict[str, str]:
        empty = {target.code: "" for target in targets}
        try:
            return {**empty, **await self.engines.translate(text, self.request, targets)}
        except Exception:  # a failed translation shows as blank rows; captions carry on
            logger.exception("[LiveDemo] Translation failed")
            return empty

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
