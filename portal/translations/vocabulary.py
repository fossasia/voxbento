"""Vocabulary CSV import, validation, and prompt-injection resolver.

Organizers upload event-specific glossaries as CSV files. This module
parses those CSVs, validates each row, persists entries to the database,
and resolves the *relevant subset* for each LLM call so prompts remain
compact.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from portal.database import get_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from portal.models import AIVocabularyEntry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = frozenset({"source_term", "target_language", "target_term"})
OPTIONAL_COLUMNS = frozenset({"description", "case_sensitive", "match_type", "priority", "notes"})
SUPPORTED_MATCH_TYPES = frozenset({"exact", "phrase"})
TRUTHY_VALUES = frozenset({"true", "yes", "1"})
FALSY_VALUES = frozenset({"false", "no", "0"})

# ISO 639-1 two-letter language codes (plus "all" sentinel)
_LANG_RE = re.compile(r"^[a-z]{2}$")

# Maximum keywords injected into the Deepgram WebSocket URL
MAX_DEEPGRAM_KEYWORDS = 50

# Default priority threshold -- entries at or above this are always included
# in the prompt regardless of whether the source term appears in the segment.
HIGH_PRIORITY_THRESHOLD = 90


# ---------------------------------------------------------------------------
# Data Transfer Objects
# ---------------------------------------------------------------------------


@dataclass
class VocabularyEntryInput:
    """Validated vocabulary row ready for database insertion."""

    source_term: str
    target_language: str
    target_term: str
    description: str | None = None
    case_sensitive: bool = False
    match_type: str = "phrase"
    priority: int = 0


@dataclass
class VocabularyImportResult:
    """Summary returned after a CSV import operation."""

    imported: int = 0
    updated: int = 0
    skipped: int = 0
    entries: list[VocabularyEntryInput] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    languages: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# CSV Parsing & Validation
# ---------------------------------------------------------------------------


def _sanitize_cell(value: str) -> str:
    """Strip whitespace from CSV cell values."""
    return value.strip()


def _parse_bool(value: str, default: bool = False) -> bool:
    """Parse a boolean from common truthy/falsy string values."""
    v = value.strip().lower()
    if v in TRUTHY_VALUES:
        return True
    if v in FALSY_VALUES:
        return False
    return default


def _parse_int(value: str, default: int = 0) -> int:
    """Parse an integer, returning *default* on failure."""
    try:
        return int(value.strip())
    except (ValueError, TypeError):
        return default


def parse_vocabulary_csv(file_content: str) -> tuple[list[VocabularyEntryInput], list[str]]:
    """Parse and validate a vocabulary CSV string.

    Returns
    -------
    entries : list[VocabularyEntryInput]
        Successfully validated rows.
    warnings : list[str]
        Human-readable warnings for skipped or corrected rows.
    """
    entries: list[VocabularyEntryInput] = []
    warnings: list[str] = []

    reader = csv.DictReader(io.StringIO(file_content))

    if reader.fieldnames is None:
        warnings.append("CSV file is empty or has no header row.")
        return entries, warnings

    # Normalise header names
    normalised_fields = {f.strip().lower() for f in reader.fieldnames}
    missing = REQUIRED_COLUMNS - normalised_fields
    if missing:
        warnings.append(f"Missing required column(s): {', '.join(sorted(missing))}")
        return entries, warnings

    seen: set[tuple[str, str]] = set()

    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        # Normalise keys
        row = {k.strip().lower(): (v or "") for k, v in raw_row.items()}

        source_term = _sanitize_cell(row.get("source_term", ""))
        target_language = _sanitize_cell(row.get("target_language", "")).strip().lower()
        target_term = _sanitize_cell(row.get("target_term", ""))

        # --- required field checks ---
        if not source_term:
            warnings.append(f"Row {row_num}: empty source_term, skipped.")
            continue
        if not target_language:
            warnings.append(f"Row {row_num}: empty target_language, skipped.")
            continue
        if not target_term:
            warnings.append(f"Row {row_num}: empty target_term, skipped.")
            continue

        # --- language code validation ---
        if target_language != "all" and not _LANG_RE.match(target_language):
            warnings.append(
                f"Row {row_num}: invalid target_language '{target_language}', "
                f"expected ISO 639-1 code or 'all'. Skipped."
            )
            continue

        # --- match type ---
        match_type = _sanitize_cell(row.get("match_type", "phrase")).strip().lower() or "phrase"
        if match_type not in SUPPORTED_MATCH_TYPES:
            warnings.append(f"Row {row_num}: unsupported match_type '{match_type}', downgraded to 'phrase'.")
            match_type = "phrase"

        # --- boolean / int fields ---
        case_sensitive = _parse_bool(row.get("case_sensitive", ""), default=False)
        priority = _parse_int(row.get("priority", ""), default=0)

        # --- duplicate detection ---
        key = (source_term.lower(), target_language)
        if key in seen:
            warnings.append(f"Row {row_num}: duplicate term '{source_term}' for target language '{target_language}'.")
            # Still import -- last-one-wins during DB upsert

        seen.add(key)

        description = _sanitize_cell(row.get("description", "")) or None

        entries.append(
            VocabularyEntryInput(
                source_term=source_term,
                target_language=target_language,
                target_term=target_term,
                description=description,
                case_sensitive=case_sensitive,
                match_type=match_type,
                priority=priority,
            )
        )

    return entries, warnings


# ---------------------------------------------------------------------------
# Database Persistence (upsert)
# ---------------------------------------------------------------------------


async def import_vocabulary_entries(
    session: "AsyncSession",
    event_id: int,
    room_id: int | None,
    booth_id: int | None,
    entries: list[VocabularyEntryInput],
) -> VocabularyImportResult:
    """Persist parsed vocabulary entries with append-and-update semantics.

    Conflicts on ``(source_term, target_language)`` for the same scope
    (event + room + booth) are resolved by updating the existing row.
    """
    from portal.models import AIVocabularyEntry

    result = VocabularyImportResult()

    # Load existing entries for this scope
    q = select(AIVocabularyEntry).where(AIVocabularyEntry.event_id == event_id)
    if room_id is not None:
        q = q.where(AIVocabularyEntry.room_id == room_id)
    else:
        q = q.where(AIVocabularyEntry.room_id.is_(None))
    if booth_id is not None:
        q = q.where(AIVocabularyEntry.booth_id == booth_id)
    else:
        q = q.where(AIVocabularyEntry.booth_id.is_(None))

    rows = (await session.scalars(q)).all()
    existing: dict[tuple[str, str], AIVocabularyEntry] = {
        (row.source_term.lower(), row.target_language): row for row in rows
    }

    for entry in entries:
        key = (entry.source_term.lower(), entry.target_language)
        result.languages.add(entry.target_language)

        if key in existing:
            # Update existing entry
            row = existing[key]
            row.target_term = entry.target_term
            row.description = entry.description
            row.case_sensitive = entry.case_sensitive
            row.match_type = entry.match_type
            row.priority = entry.priority
            result.updated += 1
        else:
            new_entry = AIVocabularyEntry(
                event_id=event_id,
                room_id=room_id,
                booth_id=booth_id,
                source_term=entry.source_term,
                target_language=entry.target_language,
                target_term=entry.target_term,
                description=entry.description,
                case_sensitive=entry.case_sensitive,
                match_type=entry.match_type,
                priority=entry.priority,
            )
            session.add(new_entry)
            existing[key] = new_entry
            result.imported += 1

    result.entries = entries
    await session.flush()
    return result


# ---------------------------------------------------------------------------
# Vocabulary Resolver (for prompt injection)
# ---------------------------------------------------------------------------


async def resolve_vocabulary_entries(
    session: "AsyncSession",
    event_id: int,
    room_id: int | None,
    booth_id: int | None,
    target_language: str,
    transcript_text: str,
    *,
    max_entries: int = 80,
) -> list["AIVocabularyEntry"]:
    """Return the vocabulary entries relevant to a specific translation call.

    Resolution order:
    1. Fetch all entries for the event where ``target_language`` matches
       the requested language OR is ``'all'``.
    2. Filter in Python: keep entries whose ``source_term`` appears in
       ``transcript_text`` (phrase match) or that match as a whole word
       (exact match), plus any entry with ``priority >= HIGH_PRIORITY_THRESHOLD``.
    3. Prefer booth-specific > room-specific > event-wide entries.
    4. Limit to ``max_entries``.
    """
    from portal.models import AIVocabularyEntry

    q = (
        select(AIVocabularyEntry)
        .where(AIVocabularyEntry.event_id == event_id)
        .where(AIVocabularyEntry.target_language.in_([target_language, "all"]))
    )
    all_entries = list((await session.scalars(q)).all())

    # --- scope bucketing ---
    # Booth-specific entries take precedence over room, which takes
    # precedence over event-wide entries. We de-duplicate by
    # (source_term_lower, target_language) keeping the most specific.
    def _scope_rank(e: AIVocabularyEntry) -> int:
        if e.booth_id is not None and e.booth_id == booth_id:
            return 2  # most specific
        if e.room_id is not None and e.room_id == room_id:
            return 1
        if e.room_id is None and e.booth_id is None:
            return 0  # event-wide
        return -1  # different room/booth -- irrelevant

    # Group by key, keep highest scope rank
    best: dict[tuple[str, str], AIVocabularyEntry] = {}
    for entry in all_entries:
        rank = _scope_rank(entry)
        if rank < 0:
            continue
        key = (entry.source_term.lower(), entry.target_language)
        existing_rank = _scope_rank(best[key]) if key in best else -1
        if rank > existing_rank:
            best[key] = entry

    # --- relevance filtering ---
    matched: list[AIVocabularyEntry] = []
    for entry in best.values():
        # Always include high-priority entries
        if entry.priority >= HIGH_PRIORITY_THRESHOLD:
            matched.append(entry)
            continue

        # Check if source term appears in the transcript
        if entry.case_sensitive:
            haystack = transcript_text
            needle = entry.source_term
        else:
            haystack = transcript_text.lower()
            needle = entry.source_term.lower()

        if entry.match_type == "exact":
            # Word-boundary match
            pattern = r"\b" + re.escape(needle) + r"\b"
            flags = 0 if entry.case_sensitive else re.IGNORECASE
            if re.search(pattern, transcript_text, flags):
                matched.append(entry)
        else:
            # Phrase (substring) match
            if needle in haystack:
                matched.append(entry)

    # Sort by priority descending, then alphabetically
    matched.sort(key=lambda e: (-e.priority, e.source_term.lower()))
    return matched[:max_entries]


# ---------------------------------------------------------------------------
# Deepgram keyword helper
# ---------------------------------------------------------------------------


async def get_deepgram_keywords(
    event_id: int,
    room_id: int | None,
    booth_id: int | None,
) -> list[str]:
    """Return Deepgram-formatted keyword boost params for the WebSocket URL.

    Fetches the top ``MAX_DEEPGRAM_KEYWORDS`` vocabulary entries (by
    priority) and returns strings like ``"FOSSASIA:3"`` suitable for
    appending as ``&keywords=...`` query parameters.

    The boost value is derived from priority:
    - priority >= 90 -> boost 3 (strong)
    - priority >= 50 -> boost 2 (moderate)
    - else           -> boost 1 (mild)
    """
    from portal.models import AIVocabularyEntry

    async with get_session() as session:
        q = (
            select(AIVocabularyEntry)
            .where(AIVocabularyEntry.event_id == event_id)
            .where(AIVocabularyEntry.target_language == "all")
            .order_by(AIVocabularyEntry.priority.desc())
            .limit(MAX_DEEPGRAM_KEYWORDS)
        )

        # Prefer room/booth-specific, but fall back to event-wide
        if booth_id is not None:
            q = q.where(
                (AIVocabularyEntry.booth_id == booth_id)
                | (AIVocabularyEntry.room_id == room_id)
                | (AIVocabularyEntry.booth_id.is_(None) & AIVocabularyEntry.room_id.is_(None))
            )
        elif room_id is not None:
            q = q.where(
                (AIVocabularyEntry.room_id == room_id)
                | (AIVocabularyEntry.room_id.is_(None) & AIVocabularyEntry.booth_id.is_(None))
            )
        else:
            q = q.where(AIVocabularyEntry.room_id.is_(None) & AIVocabularyEntry.booth_id.is_(None))

        entries = list((await session.scalars(q)).all())

    # De-duplicate by source_term (case-insensitive), keep highest priority
    seen: dict[str, tuple[str, int]] = {}
    for e in entries:
        key = e.source_term.lower()
        if key not in seen or e.priority > seen[key][1]:
            seen[key] = (e.source_term, e.priority)

    keywords: list[str] = []
    for source_term, priority in seen.values():
        if priority >= 90:
            boost = 3
        elif priority >= 50:
            boost = 2
        else:
            boost = 1
        keywords.append(f"{source_term}:{boost}")

    return keywords[:MAX_DEEPGRAM_KEYWORDS]


# ---------------------------------------------------------------------------
# CSV Export
# ---------------------------------------------------------------------------


def _escape_csv_injection(val: str) -> str:
    """Prefix dangerous characters with a single quote to prevent spreadsheet formula injection."""
    if val and val[0] in ("=", "+", "-", "@", "\t", "\r"):
        return f"'{val}"
    return val


def export_vocabulary_csv(entries: list["AIVocabularyEntry"]) -> str:
    """Serialize vocabulary entries to a CSV string for download."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "source_term",
            "target_language",
            "target_term",
            "description",
            "case_sensitive",
            "match_type",
            "priority",
        ]
    )
    for e in entries:
        writer.writerow(
            [
                _escape_csv_injection(e.source_term),
                _escape_csv_injection(e.target_language),
                _escape_csv_injection(e.target_term),
                _escape_csv_injection(e.description or ""),
                str(e.case_sensitive).lower(),
                _escape_csv_injection(e.match_type),
                e.priority,
            ]
        )
    return output.getvalue()
