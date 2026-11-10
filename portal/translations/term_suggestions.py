"""Suggest glossary terms from a talk's source material (#1172).

Extraction is deterministic and needs no API key. Translations are an optional one-off request
made while an owner prepares the glossary, never from the live path. Nothing here saves anything:
only rows the owner accepts reach the glossary, through ``validate_vocabulary_entry``.
"""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import IntEnum

import pycountry
from starlette.datastructures import FormData

from portal.models import Event, Room
from portal.translations.constants import TranslationProviderEnum
from portal.translations.keys import get_translation_api_key
from portal.translations.prompts import MAX_TERM_CHARS
from portal.translations.vocabulary import (
    VocabularyEntryInput,
    VocabularyOverlapIndex,
    VocabularyRowError,
    validate_vocabulary_entry,
)
from portal.translations.worker import PROVIDERS

MAX_SUGGESTIONS = 100
MAX_PHRASE_WORDS = 4
MAX_ACRONYM_CHARS = 8
MAX_WORD_CHARS = 64
#: How often a lowercase word or pair must recur before it counts as a term of the talk.
MIN_REPEATS = 3
#: Without a word-frequency table, a long word that keeps recurring stands in for a rare one.
MIN_RARE_WORD_CHARS = 9
MIN_PAIR_WORD_CHARS = 4

TERM_BATCH_SIZE = 25
TRANSLATION_CONCURRENCY = 3
#: The owner is waiting on this request, and a proxy may give up on it soon after.
TRANSLATION_BUDGET_SECONDS = 45.0
ALL_LANGUAGES = ("all", "All languages")
TERM_LIST_STYLE = (
    "The text is a numbered list of glossary terms, not speech. Translate each line on its own "
    "and keep its number, one term per line. Keep names, products and acronyms unchanged."
)

# Uploads are untrusted and ``re`` holds the GIL for a whole match, even in a worker thread, so a
# pattern that goes quadratic on one huge token would stall live booths. These stay linear.
_MARKUP = re.compile(r"<[^<>\n]{1,256}>|\]\([^()\]\n]{0,2048}\)")
_SOFT_HYPHEN_BREAK = re.compile(r"(?<=[^\W\d_])-\n(?=[^\W\d_]*[a-z])")
_WORD = re.compile(r"[^\W_](?:[\w+#]|[-'’](?=[^\W_])|\.(?=[a-z]{1,4}\b))*")
_POSSESSIVE = re.compile(r"['’]s$")
_SENTENCE_BREAK = re.compile(r"[.!?:;•|]")
_VERSION_LIKE = re.compile(r"[vVqQ]\d+")
_NUMBERED_LINE = re.compile(r"^\s*(\d{1,3})[.):]\s*(.+?)\s*$", re.MULTILINE)

CONNECTORS = frozenset("of de del der di du la le van von".split())

COMMON_WORDS = frozenset(
    """
    a about above across after again against all almost also although always am among an and another any
    anyone anything are around as ask at away back be became because become been before behind being below
    best better between beyond both but by call came can cannot change clear come could day days did do does
    doing done down during each early easy either else end enough even ever every everyone example few find
    first five follow following for four from full further get getting give given go going good got great
    had half hand has have having he hello help her here hers high him his how however i idea if important
    in include including instead into introduction is it its just keep kind know large last later learn left
    less let like little long look looking made make makes making many may maybe me mean might more most
    much must my name need needs never new next no nor not note now number of off often ok okay old on once
    one only open or other others our ours out over overview own part people perhaps place plan point
    possible problem project question questions quite rather real really right said same say second see seen
    she should show side since six small so some someone something sometimes soon start started still such
    summary sure take talk team tell than thank thanks that the their theirs them then there therefore these
    they thing things think third this those three through thus time to today together tomorrow too top
    toward two under until up us use used useful uses using very want was way ways we welcome well went were
    what when where whether which while who whom whose why will with within without work working world would
    year years yes yesterday yet you your yours
    monday tuesday wednesday thursday friday saturday sunday
    january february march april may june july august september october november december
    """.split()
)


class TermKind(IntEnum):
    """Ordered so a stronger signal outranks a weaker one for the same frequency."""

    REPEATED = 1
    NAME = 2
    PHRASE = 3
    PRODUCT = 4
    ACRONYM = 5


@dataclass(frozen=True)
class Token:
    text: str
    opens_sentence: bool
    follows_directly: bool


@dataclass(frozen=True)
class TermSuggestion:
    term: str
    count: int
    case_sensitive: bool


@dataclass(frozen=True)
class TranslationEngine:
    provider: str
    model: str
    api_key: str | None


@dataclass(frozen=True)
class TranslationProposals:
    by_language: dict[str, dict[str, str]]
    complete: bool


@dataclass(frozen=True)
class TargetCell:
    code: str
    language_name: str
    value: str


@dataclass(frozen=True)
class SuggestionRow:
    index: int
    term: str
    count: int
    case_sensitive: bool
    targets: tuple[TargetCell, ...]


class TermTally:
    """Counts each term under one key, remembering how it was usually written and how it was found."""

    def __init__(self) -> None:
        self.spellings: dict[str, Counter[str]] = {}
        self.kinds: dict[str, TermKind] = {}
        self.mid_sentence: set[str] = set()

    def add(self, term: str, kind: TermKind, mid_sentence: bool = True) -> None:
        key = term.casefold()
        self.spellings.setdefault(key, Counter())[term] += 1
        self.kinds[key] = max(kind, self.kinds.get(key, kind))
        if mid_sentence:
            self.mid_sentence.add(key)


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    for line in text.splitlines():
        previous_end: int | None = None
        for match in _WORD.finditer(line):
            gap = None if previous_end is None else line[previous_end : match.start()]
            tokens.append(
                Token(
                    text=_POSSESSIVE.sub("", match.group()),
                    opens_sentence=gap is None or _SENTENCE_BREAK.search(gap) is not None,
                    follows_directly=gap is not None and gap.isspace(),
                )
            )
            previous_end = match.end()
    return tokens


def acronym_stem(word: str) -> str | None:
    """The acronym ``word`` spells, without a plural "s" ("APIs" -> "API"), or None."""
    stem = word[:-1] if word.endswith("s") else word
    if not 2 <= len(stem) <= MAX_ACRONYM_CHARS or not stem.isupper():
        return None
    if sum(char.isupper() for char in stem) < 2 or stem.casefold() in COMMON_WORDS:
        return None
    return stem


def is_product_style(word: str) -> bool:
    """Inner capitals ("WebRTC", "iPhone") or letters mixed with digits ("K8s", "H264")."""
    if len(word) < 2 or acronym_stem(word) or _VERSION_LIKE.fullmatch(word):
        return False
    inner_capital = any(char.isupper() for char in word[1:]) and any(char.islower() for char in word)
    return inner_capital or (word[0].isalpha() and any(char.isdigit() for char in word))


def is_name_like(word: str) -> bool:
    if acronym_stem(word) or is_product_style(word):
        return True
    return len(word) >= 2 and word[0].isupper() and word.casefold() not in COMMON_WORDS


def phrase_end(tokens: Sequence[Token], start: int) -> int:
    """Where the run of capitalised words beginning at ``start`` stops."""

    def continues(index: int) -> bool:
        return index < len(tokens) and tokens[index].follows_directly and not tokens[index].opens_sentence

    end = start + 1
    while continues(end):
        if is_name_like(tokens[end].text):
            end += 1
        elif tokens[end].text in CONNECTORS and continues(end + 1) and is_name_like(tokens[end + 1].text):
            end += 2
        else:
            break
    return end


def record_word(tally: TermTally, token: Token) -> None:
    stem = acronym_stem(token.text)
    if stem:
        tally.add(stem, TermKind.ACRONYM)
    elif is_product_style(token.text):
        tally.add(token.text, TermKind.PRODUCT)
    else:
        tally.add(token.text, TermKind.NAME, mid_sentence=not token.opens_sentence)


def record_run(tally: TermTally, run: Sequence[Token], lowercase: Counter[str]) -> None:
    first = run[0]
    # "Deploying Kubernetes" opens a sentence; "Deploying" is dropped if the document also writes it in lowercase.
    if len(run) > 1 and first.opens_sentence and first.text.lower() in lowercase:
        run = run[1:]
    if len(run) > MAX_PHRASE_WORDS:
        # A Title Case Heading rather than a name; only unmistakable terms inside it count.
        for token in run:
            if acronym_stem(token.text) or is_product_style(token.text):
                record_word(tally, token)
    elif len(run) == 1:
        record_word(tally, run[0])
    else:
        tally.add(" ".join(token.text for token in run), TermKind.PHRASE)


def is_all_caps_line(line: Sequence[Token]) -> bool:
    return len(line) >= 3 and all(token.text.isupper() or not token.text.isalpha() for token in line)


def record_repeated_lowercase(tally: TermTally, tokens: Sequence[Token]) -> None:
    def is_content_word(word: str, min_chars: int) -> bool:
        return len(word) >= min_chars and word.isalpha() and word.islower() and word not in COMMON_WORDS

    words = Counter(token.text for token in tokens if is_content_word(token.text, MIN_RARE_WORD_CHARS))
    pairs = Counter(
        f"{left.text} {right.text}"
        for left, right in zip(tokens, tokens[1:])
        if right.follows_directly
        and not right.opens_sentence
        and is_content_word(left.text, MIN_PAIR_WORD_CHARS)
        and is_content_word(right.text, MIN_PAIR_WORD_CHARS)
    )
    for term, count in (*words.items(), *pairs.items()):
        if count >= MIN_REPEATS and tally.kinds.get(term, TermKind.NAME) <= TermKind.NAME:
            tally.spellings[term] = Counter({term: count})
            tally.kinds[term] = TermKind.REPEATED
            tally.mid_sentence.add(term)


def is_address_or_blob(word: str) -> bool:
    return "://" in word or "@" in word or word.startswith("www.") or len(word) > MAX_WORD_CHARS


def extract_terms(text: str, existing_terms: Iterable[str] = (), limit: int = MAX_SUGGESTIONS) -> list[TermSuggestion]:
    """Candidate glossary terms, likeliest first, leaving out terms already in the glossary."""
    cleaned = _SOFT_HYPHEN_BREAK.sub("", _MARKUP.sub(" ", text))
    tally = TermTally()
    tokens: list[Token] = []
    for line in cleaned.splitlines():
        # "|" rather than nothing, so the words either side of an address don't join into a phrase.
        line_tokens = tokenize(" ".join("|" if is_address_or_blob(word) else word for word in line.split(" ")))
        # A heading typed in capitals would otherwise turn each short word into an "acronym".
        if not is_all_caps_line(line_tokens):
            tokens.extend(line_tokens)
    lowercase = Counter(token.text for token in tokens if token.text.islower())

    index = 0
    while index < len(tokens):
        if not is_name_like(tokens[index].text):
            index += 1
            continue
        end = phrase_end(tokens, index)
        record_run(tally, tokens[index:end], lowercase)
        index = end
    record_repeated_lowercase(tally, tokens)

    skip = {term.casefold() for term in existing_terms}
    suggestions: list[tuple[TermKind, TermSuggestion]] = []
    for key, spellings in tally.spellings.items():
        term, _ = spellings.most_common(1)[0]
        count = spellings.total()
        kind = tally.kinds[key]
        if key in skip or len(term) > MAX_TERM_CHARS:
            continue
        # A lone capitalised word may just open a sentence; it counts only if the document
        # never writes it in lowercase, and it was seen mid-sentence or more than once.
        if kind is TermKind.NAME and (term.lower() in lowercase or (key not in tally.mid_sentence and count < 2)):
            continue
        suggestions.append((kind, TermSuggestion(term, count, case_sensitive=kind is TermKind.ACRONYM)))
    suggestions.sort(
        key=lambda item: (item[0] is TermKind.REPEATED, -item[1].count * item[0], item[1].term.casefold())
    )
    return [suggestion for _, suggestion in suggestions[:limit]]


def suggestion_engine(event: Event, room: Room) -> TranslationEngine | None:
    """The room's translation LLM, or None when it has no LLM to ask (NLLB, or no key)."""
    provider = room.floor_translation_provider
    model = room.floor_translation_model
    if not provider or not model or provider == TranslationProviderEnum.LOCAL.value or provider not in PROVIDERS:
        return None
    try:
        api_key = get_translation_api_key(event, provider)
    except ValueError:
        # A key that no longer decrypts is reported where keys are managed; it must not break this page.
        return None
    if not api_key and provider != TranslationProviderEnum.SELF_HOSTED.value:
        return None
    return TranslationEngine(provider, model, api_key)


def suggestion_languages(room: Room) -> tuple[tuple[str, str], ...]:
    """(code, name) of each language a suggestion can be translated into."""
    languages = tuple(
        (language.language_code, language.language_name)
        for language in room.translation_languages
        if language.enabled and language.language_code != room.floor_language_code
    )
    return languages or (ALL_LANGUAGES,)


def source_language_name(room: Room) -> str:
    code = room.floor_language_code
    language = pycountry.languages.get(alpha_2=code) if code else None
    return language.name if language else (code or "English")


def parse_numbered_reply(reply: str, batch: Sequence[str]) -> dict[str, str]:
    """Map a model's numbered reply back to ``batch``, ignoring lines that fit no term."""
    translations: dict[str, str] = {}
    for number, translation in _NUMBERED_LINE.findall(reply):
        position = int(number) - 1
        cleaned = translation.strip("\"'`*“” ")
        if 0 <= position < len(batch) and cleaned and len(cleaned) <= MAX_TERM_CHARS:
            translations[batch[position]] = cleaned
    return translations


async def propose_translations(
    engine: TranslationEngine,
    source_language: str,
    languages: Sequence[tuple[str, str]],
    terms: Sequence[str],
) -> TranslationProposals:
    """Ask the room's LLM to translate the terms. Only the terms are sent, never the document."""
    semaphore = asyncio.Semaphore(TRANSLATION_CONCURRENCY)

    async def translate_batch(code: str, name: str, batch: Sequence[str]) -> tuple[str, dict[str, str]]:
        numbered = "\n".join(f"{position}. {term}" for position, term in enumerate(batch, 1))
        async with semaphore:
            reply = await PROVIDERS[engine.provider].translate(
                provider_name=engine.provider,
                text=numbered,
                target_lang_name=name,
                target_lang_code=code,
                source_lang_name=source_language,
                model=engine.model,
                api_key=engine.api_key,
                style=TERM_LIST_STYLE,
            )
        return code, parse_numbered_reply(reply or "", batch)

    batches = [terms[start : start + TERM_BATCH_SIZE] for start in range(0, len(terms), TERM_BATCH_SIZE)]
    translatable = [(code, name) for code, name in languages if code != ALL_LANGUAGES[0]]
    tasks = [
        asyncio.create_task(translate_batch(code, name, batch)) for code, name in translatable for batch in batches
    ]
    by_language: dict[str, dict[str, str]] = {code: {} for code, _ in translatable}
    if not tasks:
        return TranslationProposals(by_language, complete=False)

    done, pending = await asyncio.wait(tasks, timeout=TRANSLATION_BUDGET_SECONDS)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        code, translations = task.result()
        by_language[code].update(translations)
    complete = all(term in by_language[code] for code, _ in translatable for term in terms)
    return TranslationProposals(by_language, complete)


def build_suggestion_rows(
    suggestions: Sequence[TermSuggestion],
    languages: Sequence[tuple[str, str]],
    proposals: TranslationProposals | None = None,
) -> list[SuggestionRow]:
    """Review rows. Each target starts as the term itself, since most extracted names stay as written."""
    by_language = proposals.by_language if proposals else {}
    return [
        SuggestionRow(
            index=index,
            term=suggestion.term,
            count=suggestion.count,
            case_sensitive=suggestion.case_sensitive,
            targets=tuple(
                TargetCell(code, name, by_language.get(code, {}).get(suggestion.term, suggestion.term))
                for code, name in languages
            ),
        )
        for index, suggestion in enumerate(suggestions)
    ]


def parse_accepted_suggestions(
    form: FormData, languages: Sequence[tuple[str, str]]
) -> tuple[list[VocabularyEntryInput], list[str]]:
    """Turn the ticked review rows into validated entries, one per language with a translation."""
    entries: list[VocabularyEntryInput] = []
    warnings: list[str] = []
    seen = VocabularyOverlapIndex()
    accepted_rows = list(dict.fromkeys(str(value) for value in form.getlist("accept")))[:MAX_SUGGESTIONS]
    for row_number, index in enumerate(accepted_rows, start=1):
        source_term = str(form.get(f"source_{index}", ""))
        row_entries = 0
        for code, _ in languages:
            target_term = str(form.get(f"target_{index}_{code}", "")).strip()
            if not target_term:
                continue
            row_entries += 1
            row = {
                "source_term": source_term,
                "target_language": code,
                "target_term": target_term,
                "case_sensitive": str(form.get(f"case_sensitive_{index}", "")),
                "match_type": "exact",
            }
            try:
                entry = validate_vocabulary_entry(row, row_number)
            except VocabularyRowError as exc:
                warnings.append(str(exc))
                continue
            if seen.conflicts(entry):
                warnings.append(f"Row {row_number}: duplicate term '{entry.source_term}' for '{entry.target_language}'")
                continue
            seen.add(entry)
            entries.append(entry)
        if not row_entries:
            warnings.append(f"Row {row_number}: '{source_term}' has no translation")
    return entries, warnings
