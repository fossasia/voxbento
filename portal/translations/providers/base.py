from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from portal.translations.engine import (
    EngineKind,
    Language,
    TranslationContext,
    TranslationEngine,
    TranslationRows,
    translate_each,
)


class TranslationProvider(ABC):
    @abstractmethod
    async def translate(
        self,
        provider_name: str,
        text: str,
        target_lang_name: str,
        target_lang_code: str,
        source_lang_name: str,
        model: str,
        api_key: str | None,
        persona: str | None = None,
        style: str | None = None,
        vocabulary_entries: Sequence[Any] = (),
    ) -> str | None:
        """
        Translates text to the target language.

        Args:
            text: The source text to translate
            target_lang_name: The human-readable name of the target language (e.g., 'French')
            target_lang_code: The ISO code of the target language (e.g., 'fr')
            model: The specific model to use
            api_key: The API key for the provider (None for local models)

        Returns:
            The translated text, or None if translation failed.
        """
        pass


class ProviderEngine(TranslationEngine):
    """Runs a prompt-based ``TranslationProvider`` behind the ``TranslationEngine`` interface.

    One instance per provider name, because providers that share a class (OpenAI, OpenRouter
    and Groq) are still told apart by that name.
    """

    def __init__(
        self,
        provider_name: str,
        provider: TranslationProvider,
        kind: EngineKind,
        *,
        max_concurrency: Callable[[], int] | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.provider = provider
        self._kind = kind
        # Read when asked, so the limit comes from the settings in force rather than at import.
        self._max_concurrency = max_concurrency

    @property
    def kind(self) -> EngineKind:
        return self._kind

    @property
    def max_concurrency(self) -> int:
        if self._max_concurrency is None:
            return TranslationEngine.max_concurrency
        return self._max_concurrency()

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
        def translate_one(text: str, target: Language) -> Awaitable[str | None]:
            return self.provider.translate(
                provider_name=self.provider_name,
                text=text,
                target_lang_name=target.name,
                target_lang_code=target.code,
                source_lang_name=source.name,
                model=model,
                api_key=api_key,
                persona=context.persona,
                style=context.style,
                vocabulary_entries=context.vocabulary_for(target),
            )

        return await translate_each(translate_one, targets, texts)
