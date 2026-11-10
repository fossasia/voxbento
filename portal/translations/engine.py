"""The interface every translation backend implements (#1184).

The translation worker talks to a ``TranslationEngine`` and nothing else, so a backend can
be an in-process model, a hosted API or a remote translator service without the worker
changing. This module imports no backend, so backends can import it freely.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

#: One row per source text, one column per target language, in the order they were passed.
#: ``None`` marks a translation that failed; an empty string is a valid, empty translation.
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


@dataclass(frozen=True, slots=True)
class TranslationContext:
    """What one source stream (a room's floor or a booth) wants applied to its translations.

    Engines apply what they support and ignore the rest: a sequence-to-sequence model takes
    no persona or style, and a plain machine-translation API may take none of it.
    """

    persona: str | None = None
    style: str | None = None
    #: Glossary entries by target language code. Entries carry the term in one target
    #: language, so each target has its own list.
    vocabulary: Mapping[str, Sequence[Any]] = field(default_factory=dict)

    def vocabulary_for(self, target: Language) -> Sequence[Any]:
        return self.vocabulary.get(target.code, ())


class TranslationEngineError(RuntimeError):
    """An engine broke the interface contract, e.g. returned rows of the wrong shape."""


class TranslationEngine(ABC):
    """A translation backend.

    One engine instance serves every room that selected it, so it holds no per-room state:
    the model and credentials arrive with each call.
    """

    kind: EngineKind

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
        request does not cost the others. The call raises only when nothing was translated.
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

    The calls run concurrently. A call that raises becomes ``None`` in its place, unless
    every call raised: then the first error is re-raised, as ``translate_batch`` promises.
    """
    if not targets or not texts:
        return [[] for _ in texts]

    outcomes = await asyncio.gather(
        *(translate_one(text, target) for text in texts for target in targets),
        return_exceptions=True,
    )
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    if len(errors) == len(outcomes):
        raise errors[0]
    for error in errors:
        logger.error("Translation call failed inside a batch: %r", error)

    width = len(targets)
    return [
        [None if isinstance(outcome, BaseException) else outcome for outcome in outcomes[start : start + width]]
        for start in range(0, len(outcomes), width)
    ]
