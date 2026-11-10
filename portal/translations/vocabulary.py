from __future__ import annotations

import asyncio
import csv
import io
import itertools
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

import pycountry
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.models import AIVocabularyEntry
from portal.translations.prompts import MAX_DESCRIPTION_CHARS, MAX_TERM_CHARS

REQUIRED_COLUMNS = {"source_term", "target_language", "target_term"}
MAX_ROWS = 5000
SUPPORTED_MATCH_TYPES = {"exact", "phrase"}
HIGH_PRIORITY_THRESHOLD = 90
_RESOLVER_YIELD_EVERY = 500
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


def vocabulary_terms_overlap(
    left: VocabularyEntryInput | AIVocabularyEntry,
    right: VocabularyEntryInput | AIVocabularyEntry,
) -> bool:
    if left.case_sensitive and right.case_sensitive:
        return left.source_term == right.source_term
    return left.source_term.casefold() == right.source_term.casefold()


class VocabularyOverlapIndex:
    """Constant-time equivalent of vocabulary_terms_overlap over a growing set of entries."""

    def __init__(self, per_language: bool = True) -> None:
        self._per_language = per_language
        self._case_sensitive_terms: set[tuple[str, str]] = set()
        self._case_sensitive_folded: set[tuple[str, str]] = set()
        self._case_insensitive_folded: set[tuple[str, str]] = set()

    def _lang(self, entry: VocabularyEntryInput | AIVocabularyEntry) -> str:
        return entry.target_language if self._per_language else ""

    def conflicts(self, entry: VocabularyEntryInput | AIVocabularyEntry) -> bool:
        lang = self._lang(entry)
        exact = (lang, entry.source_term)
        folded = (lang, entry.source_term.casefold())
        if folded in self._case_insensitive_folded:
            return True
        if entry.case_sensitive:
            return exact in self._case_sensitive_terms
        return folded in self._case_sensitive_folded

    def add(self, entry: VocabularyEntryInput | AIVocabularyEntry) -> None:
        lang = self._lang(entry)
        folded = (lang, entry.source_term.casefold())
        if entry.case_sensitive:
            self._case_sensitive_terms.add((lang, entry.source_term))
            self._case_sensitive_folded.add(folded)
        else:
            self._case_insensitive_folded.add(folded)


class VocabularyRowError(ValueError):
    pass


class VocabularyScopeError(ValueError):
    pass


async def validate_vocabulary_scope(
    session: AsyncSession, event_id: int, room_id: int | None = None, booth_id: int | None = None
) -> None:
    from portal.models import DBBooth, Room

    if room_id is not None:
        room = await session.get(Room, room_id)
        if room is None or room.event_id != event_id:
            raise VocabularyScopeError("Room does not belong to this event.")
    if booth_id is not None:
        booth = await session.get(DBBooth, booth_id)
        if booth is None or booth.event_id != event_id or (room_id is not None and booth.room_id != room_id):
            raise VocabularyScopeError("Booth does not belong to this room/event.")


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


def _bounded(value: str, field: str, limit: int) -> str:
    if len(value) > limit:
        raise VocabularyRowError(f"{field} is longer than {limit} characters")
    return value


def validate_vocabulary_entry(row: dict[str, str], row_number: int) -> VocabularyEntryInput:
    try:
        source_term = _bounded(
            _safe_csv_text(_required_text(row, "source_term"), "source_term"), "source_term", MAX_TERM_CHARS
        )
        target_language = _required_text(row, "target_language").lower()
        target_term = _bounded(
            _safe_csv_text(_required_text(row, "target_term"), "target_term"), "target_term", MAX_TERM_CHARS
        )
        description = (row.get("description") or row.get("notes") or "").strip() or None
        if description:
            description = _bounded(
                _safe_csv_text(description, "description"), "description", MAX_DESCRIPTION_CHARS
            )
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
    seen = VocabularyOverlapIndex()
    for row_number, row in enumerate(reader, start=2):
        if len(entries) >= MAX_ROWS:
            warnings.append(f"Stopped after {MAX_ROWS} entries; the rest of the file was ignored.")
            break
        try:
            entry = validate_vocabulary_entry(row, row_number)
        except VocabularyRowError as exc:
            warnings.append(str(exc))
            continue
        if seen.conflicts(entry):
            warnings.append(
                f"Row {row_number}: duplicate term '{entry.source_term}' for target language '{entry.target_language}'"
            )
            continue
        seen.add(entry)
        entries.append(entry)
    return entries, warnings


def _term_matches(entry: AIVocabularyEntry, transcript_text: str) -> bool:
    if entry.priority >= HIGH_PRIORITY_THRESHOLD:
        return True
    if entry.match_type == "exact":
        # Word boundaries, so "US" does not match inside "USB" or "status".
        # Case folding rather than re.IGNORECASE, to stay consistent with the
        # phrase branch and the overlap rules (so "Straße" matches "STRASSE").
        term = entry.source_term if entry.case_sensitive else entry.source_term.casefold()
        haystack = transcript_text if entry.case_sensitive else transcript_text.casefold()
        return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack) is not None
    haystack = transcript_text if entry.case_sensitive else transcript_text.casefold()
    needle = entry.source_term if entry.case_sensitive else entry.source_term.casefold()
    return needle in haystack


# NLLB keeps "[1]" to "[15]" as single tokens and copies them through unchanged in about
# 95% of segments; "⟦1⟧" is not in its vocabulary and never survives. Matched loosely, so a
# mangled copy such as "[ 1]", "[01]", "［1］" or "【1】" is caught instead of shown to listeners.
TERM_PLACEHOLDER = re.compile(r"[\[［【]\s*\d+\s*[\]］】]")
_SPACED_PLACEHOLDER = re.compile(rf"(\s?)({TERM_PLACEHOLDER.pattern})(\s?)")
# Chinese and Japanese, which never put a space between two of their characters.
_CJK_SCRIPTS = ("CJK", "HIRAGANA", "KATAKANA")
# Scripts written without spaces between words, where a term can start or end anywhere.
_UNSPACED_SCRIPTS = (*_CJK_SCRIPTS, "THAI", "LAO", "KHMER", "MYANMAR")


@dataclass(frozen=True)
class ProtectedSegment:
    text: str
    target_terms: tuple[str, ...]
    # Placeholder numbers whose target term keeps the glossary's casing, even when it opens a sentence.
    exact_case: frozenset[int] = frozenset()


def _in_word(char: str) -> bool:
    # Combining marks (accents, Indic vowel signs) are part of the letter they follow.
    return char.isalnum() or char == "_" or unicodedata.category(char).startswith("M")


def _in_scripts(char: str, scripts: tuple[str, ...]) -> bool:
    return bool(char) and unicodedata.name(char, "").startswith(scripts)


def _cuts_word(edge: str, neighbour: str) -> bool:
    """Whether a match would cut a word apart between its ``edge`` character and the ``neighbour`` outside it."""
    return _in_word(neighbour) and not any(_in_scripts(char, _UNSPACED_SCRIPTS) for char in (edge, neighbour))


def _keeps_case(entry: VocabularyEntryInput | AIVocabularyEntry) -> bool:
    # "Keep as is" entries (npm -> npm) and case-sensitive ones are written exactly as the glossary says.
    return entry.case_sensitive or entry.target_term.casefold() == entry.source_term.casefold()


# Abbreviations that end in a full stop but rarely end a sentence: titles and "for example" forms.
# Words such as "etc." that often close a sentence are left out.
_ABBREVIATIONS = frozenset("dr mr mrs ms prof m mme mlle sr sra srta hr nr ex vs cf vgl bzw".split())


def _opens_sentence(before: str) -> bool:
    """Whether a placeholder that follows ``before`` starts a sentence of the translation."""
    # Opening quotes and marks such as "«" or "¿" are not words.
    words = [word for word in before.split() if any(char.isalnum() for char in word) or word[-1] in ".!?"]
    if not words:
        return True
    last = words[-1].rstrip("\"'»”’)")
    if last.endswith(("!", "?")):
        return True
    if not last.endswith("."):
        return False
    stem = last[:-1]
    # Dotted ("e.g.") and common title or "for example" abbreviations ("Dr.", "par ex.") do not end a sentence.
    if "." in stem or stem.casefold() in _ABBREVIATIONS:
        return False
    previous = words[-2] if len(words) > 1 else ""
    # The second part of a spaced abbreviation such as "z. B." or "p. ex.".
    if len(previous) == 2 and previous[0].isalpha() and previous[1] == "." and (stem.islower() or len(stem) == 1):
        return False
    if len(stem) == 1 and stem.isalpha():
        # An initial follows a capitalised name ("John F."); "plan B." ends its sentence.
        return not previous[:1].isupper()
    return True


def find_term_spans(entry: VocabularyEntryInput | AIVocabularyEntry, text: str) -> list[tuple[int, int]]:
    """Where ``entry`` occurs in ``text`` as whole words, folded as in ``_term_matches``.

    Phrase entries are matched inside words when picking glossary lines for a prompt, but
    replacing such a match would cut the surrounding word apart ("AI" in "said"). Chinese,
    Japanese and other scripts written without spaces have no word boundaries to keep.
    """
    term = entry.source_term if entry.case_sensitive else entry.source_term.casefold()
    if not term:
        return []
    pieces = [char if entry.case_sensitive else char.casefold() for char in text]
    folded = "".join(pieces)
    # Case folding can lengthen the text ("ß" -> "ss"), so map folded offsets back to the original ones.
    offsets = dict(zip(itertools.accumulate(map(len, pieces), initial=0), range(len(text) + 1)))
    spans = []
    for match in re.finditer(re.escape(term), folded):
        start, end = match.span()
        # A match that starts or ends inside a folded character cannot be cut out without taking letters
        # the term did not match; that occurrence is translated as usual.
        if start not in offsets or end not in offsets:
            continue
        if start > 0 and _cuts_word(folded[start], folded[start - 1]):
            continue
        # A combining mark after the match belongs to its last letter, whatever the script.
        if end < len(folded) and (
            unicodedata.category(folded[end]).startswith("M") or _cuts_word(folded[end - 1], folded[end])
        ):
            continue
        spans.append((offsets[start], offsets[end]))
    return spans


def protect_terms(text: str, entries: Iterable[VocabularyEntryInput | AIVocabularyEntry]) -> ProtectedSegment:
    """Replace glossary matches with numbered placeholders for a model that takes no instructions."""
    # Numbers already in brackets could not be told apart from the placeholders.
    if TERM_PLACEHOLDER.search(text):
        return ProtectedSegment(text, ())
    spans = sorted(
        (
            (start, end, rank, entry.target_term, _keeps_case(entry))
            for rank, entry in enumerate(entries)
            for start, end in find_term_spans(entry, text)
        ),
        # The longest match wins an overlap; ties go to the entry the resolver ranked first.
        key=lambda span: (span[0] - span[1], span[2], span[0]),
    )
    chosen: list[tuple[int, int, int, str, bool]] = []
    for span in spans:
        if all(span[1] <= other[0] or span[0] >= other[1] for other in chosen):
            chosen.append(span)
    chosen.sort()
    parts: list[str] = []
    cursor = 0
    for number, (start, end, _, _, _) in enumerate(chosen, 1):
        # A placeholder only touches a letter in text written without spaces. Spacing it there keeps NLLB
        # from gluing it to a word of the translation ("We met at[1]").
        before = " " if start > 0 and _in_word(text[start - 1]) else ""
        after = " " if end < len(text) and _in_word(text[end]) else ""
        parts += (text[cursor:start], f"{before}[{number}]{after}")
        cursor = end
    parts.append(text[cursor:])
    return ProtectedSegment(
        "".join(parts),
        tuple(span[3] for span in chosen),
        frozenset(number for number, span in enumerate(chosen, 1) if span[4]),
    )


def restore_terms(translated: str, segment: ProtectedSegment) -> str | None:
    """Swap placeholders for target terms, or return None unless each one came back intact, exactly once."""
    numbers = {f"[{number}]": number for number in range(1, len(segment.target_terms) + 1)}
    if sorted(TERM_PLACEHOLDER.findall(translated)) != sorted(numbers):
        return None

    def target_term(match: re.Match[str]) -> str:
        space_before, placeholder, space_after = match.groups()
        number = numbers[placeholder]
        term = segment.target_terms[number - 1]
        # A lowercase term that opens a sentence ("[1] ouvre…") takes a capital, unless the glossary fixes its
        # casing ("npm"); "iPhone" is not lowercase, so it always stays as written.
        if term.islower() and number not in segment.exact_case and _opens_sentence(translated[: match.start(2)]):
            term = term.capitalize()
        # NLLB can keep the spaces around a placeholder, which do not belong between two Chinese or Japanese
        # characters ("我们在 展台 见面"). Thai and the like do use spaces, between phrases.
        char_before = translated[match.start() - 1 : match.start()]
        char_after = translated[match.end() : match.end() + 1]
        if _in_scripts(term[:1], _CJK_SCRIPTS) and _in_scripts(char_before, _CJK_SCRIPTS):
            space_before = ""
        if _in_scripts(term[-1:], _CJK_SCRIPTS) and _in_scripts(char_after, _CJK_SCRIPTS):
            space_after = ""
        return space_before + term + space_after

    return _SPACED_PLACEHOLDER.sub(target_term, translated)


async def resolve_vocabulary_entries(
    session: AsyncSession,
    event_id: int,
    room_id: int | None,
    booth_id: int | None,
    target_language: str,
    transcript_text: str,
    max_entries: int = 80,
) -> list[AIVocabularyEntry]:
    # Each scope is matched exactly: an entry pinned to one booth must not be
    # selected for its siblings just because they share a room.
    scope_filters = [
        (AIVocabularyEntry.room_id.is_(None) & AIVocabularyEntry.booth_id.is_(None)),
    ]
    if room_id is not None:
        scope_filters.append((AIVocabularyEntry.room_id == room_id) & AIVocabularyEntry.booth_id.is_(None))
    if booth_id is not None:
        booth_filter = AIVocabularyEntry.booth_id == booth_id
        if room_id is not None:
            # Guard against rows whose room and booth disagree.
            booth_filter = booth_filter & (
                AIVocabularyEntry.room_id.is_(None) | (AIVocabularyEntry.room_id == room_id)
            )
        scope_filters.append(booth_filter)
    stream = await session.stream_scalars(
        select(AIVocabularyEntry).where(
            AIVocabularyEntry.event_id == event_id,
            AIVocabularyEntry.target_language.in_(("all", target_language)),
            or_(*scope_filters),
        )
    )
    candidates: list[AIVocabularyEntry] = []
    processed = 0
    async for entry in stream:
        if _term_matches(entry, transcript_text):
            candidates.append(entry)
        processed += 1
        if processed % _RESOLVER_YIELD_EVERY == 0:
            await asyncio.sleep(0)

    def scope_rank(entry: AIVocabularyEntry) -> int:
        if booth_id is not None and entry.booth_id == booth_id:
            return 2
        if room_id is not None and entry.booth_id is None and entry.room_id == room_id:
            return 1
        return 0

    # "all" and target-specific rows compete for the same source term; scope then
    # priority decides which translation wins, before any cap is applied.
    candidates.sort(key=lambda entry: (scope_rank(entry), entry.priority, entry.id), reverse=True)
    deduplicated: list[AIVocabularyEntry] = []
    seen = VocabularyOverlapIndex(per_language=False)
    for entry in candidates:
        if seen.conflicts(entry):
            continue
        seen.add(entry)
        deduplicated.append(entry)

    # High-priority entries are documented as always included, so they take the cap first.
    deduplicated.sort(
        key=lambda entry: (
            entry.priority >= HIGH_PRIORITY_THRESHOLD,
            scope_rank(entry),
            entry.priority,
            entry.id,
        ),
        reverse=True,
    )
    return deduplicated[:max_entries]


def _neutralize_csv_text(value: str) -> str:
    """Prefix a leading formula character so spreadsheets treat the cell as text."""
    if value.lstrip().startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def serialize_vocabulary_csv(entries: list[AIVocabularyEntry]) -> str:
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        ("source_term", "target_language", "target_term", "description", "case_sensitive", "match_type", "priority")
    )
    for entry in entries:
        writer.writerow(
            (
                _neutralize_csv_text(entry.source_term),
                entry.target_language,
                _neutralize_csv_text(entry.target_term),
                _neutralize_csv_text(entry.description or ""),
                str(entry.case_sensitive).lower(),
                entry.match_type,
                entry.priority,
            )
        )
    return output.getvalue()
