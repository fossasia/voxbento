"""Which languages a chosen translation model can handle (#1186).

Only self-hosted models have a fixed language list (in the registry). A hosted model, or the
operator's self-hosted LLM, is asked by prompt and is not restricted here.
"""

from __future__ import annotations

from collections.abc import Iterable

import pycountry

from portal.translations.constants import TranslationProviderEnum
from portal.translations.registry import NLLB_200_DISTILLED_600M, TranslationModel, get_translation_model


def fixed_language_model(provider: str | None, model: str | None) -> TranslationModel | None:
    """The registry entry that limits this choice's languages, or None when nothing limits them.

    A built-in model name without an entry (an operator's own NLLB build) uses the NLLB
    language list, as the local provider does at translation time.
    """
    if provider != TranslationProviderEnum.LOCAL.value:
        return None
    return get_translation_model(model or "") or NLLB_200_DISTILLED_600M


def supported_languages(provider: str | None, model: str | None) -> list[str] | None:
    """The ISO 639-1 codes this choice translates, or None when it is not limited to a list."""
    entry = fixed_language_model(provider, model)
    return sorted(entry.languages) if entry else None


def unsupported_languages(provider: str | None, model: str | None, language_codes: Iterable[str | None]) -> list[str]:
    """The given languages this choice cannot translate; a language that is not set is skipped."""
    entry = fixed_language_model(provider, model)
    if entry is None:
        return []
    return entry.unsupported(code for code in language_codes if code)


def language_support_error(provider: str | None, model: str | None, language_codes: Iterable[str | None]) -> str | None:
    """A message naming the languages this choice cannot translate, or None when it handles them all.

    Pass the language being spoken and every language being translated into.
    """
    unsupported = unsupported_languages(provider, model, language_codes)
    if not unsupported:
        return None
    names = ", ".join(_name(code) for code in unsupported)
    return (
        f"The built-in translator ({fixed_language_model(provider, model).id}) doesn't support {names}. "
        "Choose another translation service or change the languages."
    )


def _name(code: str) -> str:
    """The English name for an ISO 639-1 code, falling back to the code itself."""
    language = pycountry.languages.get(alpha_2=code.strip().lower())
    return language.name if language else code
