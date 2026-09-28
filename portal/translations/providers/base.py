from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any


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
