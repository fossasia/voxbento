from __future__ import annotations

from portal.crypto import decrypt_val
from portal.models import Event
from portal.translations.constants import TranslationProviderEnum


def get_translation_api_key(event: Event, provider: str) -> str | None:
    """Return the decrypted translation API key for ``provider`` on ``event``.

    Returns ``None`` when no key is stored for the provider. This is the single
    source of truth shared by the translation and TTS workers.
    """
    key_map = {
        TranslationProviderEnum.OPENAI.value: event.encrypted_translation_openai_api_key,
        TranslationProviderEnum.OPENROUTER.value: event.encrypted_openrouter_api_key,
        TranslationProviderEnum.GEMINI.value: event.encrypted_gemini_api_key,
        TranslationProviderEnum.ANTHROPIC.value: event.encrypted_anthropic_api_key,
        TranslationProviderEnum.GROQ.value: event.encrypted_groq_api_key,
    }
    encrypted = key_map.get(provider)
    return decrypt_val(encrypted) if encrypted else None
