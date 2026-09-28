from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import pycountry
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.models import AIVocabularyEntry

REQUIRED_COLUMNS = {"source_term", "target_language", "target_term"}
SUPPORTED_MATCH_TYPES = {"exact", "phrase"}
HIGH_PRIORITY_THRESHOLD = 90
_TRUE_VALUES = {"true", "yes", "1"}
_FALSE_VALUES = {"false", "no", "0", ""}
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


@dataclass(frozen=True)
class VocabularyEntryInput:
    source_term: str
    target_language: str
    target_term: str
    description: str | None = None
    case_sensitive: bool = False
    match_type: str = "phrase"
    priority: int = 0


class VocabularyRowError(ValueError):
    pass


def _required_text(row: dict[str, str], key: str) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise VocabularyRowError(f"{key} is required")
    return value


def _safe_csv_text(value: str, field: str) -> str:
    if value.lstrip().startswith(_CSV_FORMULA_PREFIXES):
        raise VocabularyRowError(f"{field} starts with a spreadsheet formula character")
    return value


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise VocabularyRowError("case_sensitive must be true, false, yes, no, 1, or 0")


def validate_vocabulary_entry(row: dict[str, str], row_number: int) -> VocabularyEntryInput:
    try:
        source_term = _safe_csv_text(_required_text(row, "source_term"), "source_term")
        target_language = _required_text(row, "target_language").lower()
        target_term = _safe_csv_text(_required_text(row, "target_term"), "target_term")
        description = (row.get("description") or row.get("notes") or "").strip() or None
        if description:
            description = _safe_csv_text(description, "description")
        if target_language != "all" and pycountry.languages.get(alpha_2=target_language) is None:
            raise VocabularyRowError(f"unsupported target_language '{target_language}'")
        match_type = (row.get("match_type") or "phrase").strip().lower()
        if match_type not in SUPPORTED_MATCH_TYPES:
            raise VocabularyRowError(f"unsupported match_type '{match_type}'")
        try:
            priority = int((row.get("priority") or "0").strip())
        except ValueError as exc:
            raise VocabularyRowError("priority must be an integer") from exc
        return VocabularyEntryInput(
            source_term=source_term,
            target_language=target_language,
            target_term=target_term,
            description=description,
            case_sensitive=_parse_bool(row.get("case_sensitive") or ""),
            match_type=match_type,
            priority=priority,
        )
    except VocabularyRowError as exc:
        raise VocabularyRowError(f"Row {row_number}: {exc}") from exc


def parse_vocabulary_csv(file_content: str) -> tuple[list[VocabularyEntryInput], list[str]]:
    reader = csv.DictReader(io.StringIO(file_content.lstrip("\ufeff")))
    headers = set(reader.fieldnames or ())
    missing = sorted(REQUIRED_COLUMNS - headers)
    if missing:
        return [], [f"Missing required columns: {', '.join(missing)}"]

    entries: list[VocabularyEntryInput] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for row_number, row in enumerate(reader, start=2):
        try:
            entry = validate_vocabulary_entry(row, row_number)
        except VocabularyRowError as exc:
            warnings.append(str(exc))
            continue
        duplicate_key = (entry.source_term.casefold(), entry.target_language)
        if duplicate_key in seen:
            warnings.append(
                f"Row {row_number}: duplicate term '{entry.source_term}' for target language '{entry.target_language}'"
            )
            continue
        seen.add(duplicate_key)
        entries.append(entry)
    return entries, warnings


def _term_matches(entry: AIVocabularyEntry, transcript_text: str) -> bool:
    if entry.priority >= HIGH_PRIORITY_THRESHOLD:
        return True
    haystack = transcript_text if entry.case_sensitive else transcript_text.casefold()
    needle = entry.source_term if entry.case_sensitive else entry.source_term.casefold()
    return needle in haystack


async def resolve_vocabulary_entries(
    session: AsyncSession,
    event_id: int,
    room_id: int | None,
    booth_id: int | None,
    target_language: str,
    transcript_text: str,
    max_entries: int = 80,
) -> list[AIVocabularyEntry]:
    scope_filters = [
        (AIVocabularyEntry.room_id.is_(None) & AIVocabularyEntry.booth_id.is_(None)),
    ]
    if room_id is not None:
        scope_filters.append(AIVocabularyEntry.room_id == room_id)
    if booth_id is not None:
        scope_filters.append(AIVocabularyEntry.booth_id == booth_id)
    result = await session.scalars(
        select(AIVocabularyEntry).where(
            AIVocabularyEntry.event_id == event_id,
            AIVocabularyEntry.target_language.in_(("all", target_language)),
            or_(*scope_filters),
        )
    )

    candidates = [entry for entry in result if _term_matches(entry, transcript_text)]

    def scope_rank(entry: AIVocabularyEntry) -> int:
        if booth_id is not None and entry.booth_id == booth_id:
            return 2
        if room_id is not None and entry.room_id == room_id:
            return 1
        return 0

    candidates.sort(key=lambda entry: (scope_rank(entry), entry.priority, entry.id), reverse=True)
    selected: list[AIVocabularyEntry] = []
    seen: set[str] = set()
    for entry in candidates:
        # "all" and target-specific rows compete for the same source term.
        # The scope/priority sort above decides which translation wins.
        key = entry.source_term.casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(entry)
        if len(selected) >= max_entries:
            break
    return selected


def serialize_vocabulary_csv(entries: list[AIVocabularyEntry]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        ("source_term", "target_language", "target_term", "description", "case_sensitive", "match_type", "priority")
    )
    for entry in entries:
        writer.writerow(
            (
                entry.source_term,
                entry.target_language,
                entry.target_term,
                entry.description or "",
                str(entry.case_sensitive).lower(),
                entry.match_type,
                entry.priority,
            )
        )
    return output.getvalue()
