"""Glossary terms that bias speech recognition (#1164).

The event glossary (``ai_vocabulary_entries``) is written for translation, but its source terms are
exactly the words a recogniser tends to mishear: product names, people's names and jargon. A term
spelled wrongly in the transcript can no longer be matched by the glossary downstream, so the error
reaches captions, translation and TTS. This module turns the glossary into a short list of source
terms that a transcription provider can use to bias recognition.

The terms are looked up once when a transcription worker starts (never per audio chunk) and carried
to the provider in ``ProviderConfig.terms``. A failed lookup means "no terms", never a failed worker.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from portal.database import get_event_by_slug, get_room_by_id, get_session
from portal.models import AIVocabularyEntry, DBBooth
from portal.translations.vocabulary import HIGH_PRIORITY_THRESHOLD, vocabulary_scope_filter, vocabulary_scope_rank

logger = logging.getLogger(__name__)

# Whisper reads at most ~224 tokens of prompt; 400 characters is about 100 tokens, leaving room to spare.
MAX_TERMS_CHARS = 400
# Terms are optional context: a slow lookup must not delay the caption worker for long.
VOCABULARY_LOOKUP_TIMEOUT = 2.0
# A bound on how many glossary rows one lookup reads, highest priority first.
MAX_ENTRIES_SCANNED = 1000

_SEPARATOR = ", "
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")


def clean_term(term: str) -> str:
    """One line of plain text: no control characters or newlines, no commas, single spaces."""
    text = _CONTROL_CHARS.sub(" ", term).replace(",", " ")
    return _WHITESPACE.sub(" ", text).strip()


def select_terms(
    entries: Iterable[AIVocabularyEntry],
    room_id: int | None,
    booth_id: int | None,
    max_chars: int = MAX_TERMS_CHARS,
) -> tuple[str, ...]:
    """The source terms to bias recognition with, most important first, within ``max_chars`` once joined.

    Ordered like the translation glossary: high-priority entries first (they are documented as always
    included), then booth entries over room entries over event-wide ones, then priority. A term
    that appears twice (for example once per target language) is kept once, whatever its case.
    """
    ranked = sorted(
        entries,
        key=lambda entry: (
            entry.priority >= HIGH_PRIORITY_THRESHOLD,
            vocabulary_scope_rank(entry, room_id, booth_id),
            entry.priority,
            entry.id,
        ),
        reverse=True,
    )
    terms: list[str] = []
    seen: set[str] = set()
    used = 0
    for entry in ranked:
        term = clean_term(entry.source_term)
        key = term.casefold()
        if not term or key in seen:
            continue
        cost = len(term) + (len(_SEPARATOR) if terms else 0)
        if used + cost > max_chars:
            break
        seen.add(key)
        terms.append(term)
        used += cost
    return tuple(terms)


async def _lookup_terms(event_slug: str, room_id: int, language_code: str) -> tuple[str, ...]:
    async with get_session() as session:
        event = await get_event_by_slug(session, event_slug)
        room = await get_room_by_id(session, room_id)
        if event is None or room is None or room.event_id != event.id or not room.floor_ai_vocabulary_enabled:
            return ()
        # The floor has no booth row, so it only sees the event-wide and room entries.
        booth_id = await session.scalar(
            select(DBBooth.id).where(DBBooth.room_id == room_id, DBBooth.language_code == language_code)
        )
        entries = await session.scalars(
            select(AIVocabularyEntry)
            .where(AIVocabularyEntry.event_id == event.id, vocabulary_scope_filter(room_id, booth_id))
            .order_by(AIVocabularyEntry.priority.desc(), AIVocabularyEntry.id)
            .limit(MAX_ENTRIES_SCANNED)
        )
        return select_terms(entries.all(), room_id, booth_id)


async def load_vocabulary_terms(event_slug: str, room_id: int | None, language_code: str) -> tuple[str, ...]:
    """The glossary's source terms for one transcription worker, or ``()`` when there are none.

    ``()`` also covers a room that has switched the glossary off and a lookup that failed or timed out.
    """
    if room_id is None:
        return ()
    try:
        async with asyncio.timeout(VOCABULARY_LOOKUP_TIMEOUT):
            return await _lookup_terms(event_slug, room_id, language_code)
    except (SQLAlchemyError, TimeoutError) as exc:
        logger.warning(
            "Glossary lookup for transcription failed for room %s (%s). Transcribing without glossary terms.",
            room_id,
            exc,
        )
        return ()
