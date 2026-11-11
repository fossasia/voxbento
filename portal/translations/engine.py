"""The interface every translation backend implements (#1184).

The translation worker talks to a ``TranslationEngine`` and nothing else, so a backend can
be an in-process model, a hosted API or a remote translator service without the worker
changing. This module imports no backend, so backends can import it freely.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from portal.models import AIVocabularyEntry
    from portal.translations.vocabulary import VocabularyEntryInput

    #: A glossary line: a stored entry, or one not saved yet (same fields).
    GlossaryEntry = AIVocabularyEntry | VocabularyEntryInput

logger = logging.getLogger(__name__)

#: One row per source text, one column per target language, in the order they were passed.
#: ``None`` marks a translation that failed. An empty string means the engine ran and produced
#: no text, which is not a failure: the worker closes the segment and listeners see nothing.
TranslationRows = list[list[str | None]]


class EngineKind(str, Enum):
    """How an engine translates, which decides what it can be asked to do."""

    #: One multilingual CTranslate2 model for every language pair (NLLB, M2M100, MADLAD).
    CT2_SEQ2SEQ = "ct2_seq2seq"
    #: One CTranslate2 model per language pair (OPUS-MT / Marian).
    CT2_PAIR = "ct2_pair"
    #: A chat model behind an OpenAI-compatible endpoint, hosted or self-hosted.
    OPENAI_COMPATIBLE_LLM = "openai_compatible_llm"
    #: A chat model behind a vendor's own API (Gemini, Anthropic).
    LLM_API = "llm_api"
    #: A machine-translation API (DeepL, Google, Azure, LibreTranslate).
    MT_API = "mt_api"


@dataclass(frozen=True, slots=True)
class Language:
    """A language as engines need it: prompts use the name, models and APIs the code."""

    #: ISO 639-1 code, or ``""`` when the caller only knows the name.
    code: str
    #: English name, e.g. ``"French"``.
    name: str


class _Glossaries(Mapping[str, "tuple[GlossaryEntry, ...]"]):
    """A read-only copy of the glossaries by target language code.

    A plain class, not ``MappingProxyType``, so a context can still be pickled, deep-copied
    and passed to ``dataclasses.asdict`` (a remote backend may need to serialise it). The
    entry lists are stored as tuples, so they cannot be changed either.
    """

    __slots__ = ("_by_code",)

    def __init__(self, vocabulary: Mapping[str, Sequence[GlossaryEntry]]) -> None:
        self._by_code = {code: tuple(entries) for code, entries in vocabulary.items()}

    def __getitem__(self, code: str) -> tuple[GlossaryEntry, ...]:
        return self._by_code[code]

    def __iter__(self) -> Iterator[str]:
        return iter(self._by_code)

    def __len__(self) -> int:
        return len(self._by_code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._by_code!r})"


@dataclass(frozen=True, slots=True)
class TranslationContext:
    """What one source stream (a room's floor or a booth) wants applied to its translations.

    Engines apply what they support and ignore the rest: a sequence-to-sequence model takes
    no persona or style, and a plain machine-translation API may take none of it.
    """

    persona: str | None = None
    style: str | None = None
    #: Glossary entries by target language code. Entries carry the term in one target
    #: language, so each target has its own list. Read-only once the context is built.
    vocabulary: Mapping[str, Sequence[GlossaryEntry]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A read-only copy, lists included, so neither the engine nor the caller's own
        # dict or lists can change a context that is already on its way to a model.
        object.__setattr__(self, "vocabulary", _Glossaries(self.vocabulary))

    def vocabulary_for(self, target: Language) -> Sequence[GlossaryEntry]:
        return self.vocabulary.get(target.code, ())


class TranslationEngineError(RuntimeError):
    """An engine broke the interface contract, e.g. returned rows of the wrong shape."""


class TranslationEngine(ABC):
    """A translation backend.

    One engine instance serves every room that selected it, so it holds no per-room state:
    the model and credentials arrive with each call.
    """

    @property
    @abstractmethod
    def kind(self) -> EngineKind:
        """How this engine translates. A class attribute is enough: ``kind = EngineKind.MT_API``."""

    @abstractmethod
    async def translate_batch(
        self,
        source: Language,
        targets: Sequence[Language],
        texts: Sequence[str],
        context: TranslationContext,
        *,
        model: str,
        api_key: str | None = None,
    ) -> TranslationRows:
        """Translate every text into every target language.

        All texts come from one source stream, so ``context`` applies to each of them.

        Returns ``len(texts)`` rows of ``len(targets)`` translations each. A translation
        that failed is ``None`` in its place, so one unsupported language or one failed
        request does not cost the others. The call raises only when every request failed
        with an error. A backend answering ``None`` itself (say for a language it lacks) is
        not an error, so a mix of errors and such answers returns all ``None`` without raising.
        """

    async def translate_text(
        self,
        source: Language,
        target: Language,
        text: str,
        context: TranslationContext,
        *,
        model: str,
        api_key: str | None = None,
    ) -> str | None:
        """Translate one text into one language: ``translate_batch`` for the single case."""
        rows = await self.translate_batch(source, [target], [text], context, model=model, api_key=api_key)
        if len(rows) != 1 or len(rows[0]) != 1:
            raise TranslationEngineError(
                f"{type(self).__name__}.translate_batch returned the wrong shape for 1 text and 1 target"
            )
        return rows[0][0]


async def translate_each(
    translate_one: Callable[[str, Language], Awaitable[str | None]],
    targets: Sequence[Language],
    texts: Sequence[str],
) -> TranslationRows:
    """Build ``translate_batch`` rows from a backend that translates one text into one language.

    A call that raises becomes ``None`` in its place, unless every call raised: then the
    first error is re-raised, as ``translate_batch`` promises. Every error is logged,
    including the ones that are not re-raised. A cancelled call is re-raised as it is.

    Every call starts at once: ``len(texts) * len(targets)`` of them, with no limit here.
    That is safe only while the caller bounds how much it sends, as the worker does today
    with one text and one target per call. Bounding concurrency across rooms is the
    scheduler's job (#1188), and a model that can translate several targets in one pass
    should do that in its own ``translate_batch`` instead of using this helper (#1190).
    """
    if not targets or not texts:
        return [[] for _ in texts]

    width = len(targets)
    outcomes = await asyncio.gather(
        *(translate_one(text, target) for text in texts for target in targets),
        return_exceptions=True,
    )
    # A cancelled call (or any other non-Exception) is not a failed translation: pass it on.
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, Exception):
            raise outcome
    errors = [(position, outcome) for position, outcome in enumerate(outcomes) if isinstance(outcome, Exception)]
    all_failed = len(errors) == len(outcomes)
    # When every call failed the first error is re-raised and logged by the caller.
    for position, error in errors[1:] if all_failed else errors:
        # The text's index, never the text: transcripts stay out of the logs.
        logger.error(
            "Translation into %r failed for text %d of %d in a batch: %r",
            targets[position % width].code,
            position // width + 1,
            len(texts),
            error,
        )
    if all_failed:
        raise errors[0][1]

    return [
        [None if isinstance(outcome, Exception) else outcome for outcome in outcomes[start : start + width]]
        for start in range(0, len(outcomes), width)
    ]
