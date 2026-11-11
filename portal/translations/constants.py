from __future__ import annotations

from enum import Enum
from typing import Dict, List

from portal.translations.registry import LOCAL_TRANSLATION_MODELS


class TranslationProviderEnum(str, Enum):
    # Translation Providers
    LOCAL = "local"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    GROQ = "groq"
    SELF_HOSTED = "self_hosted"


TRANSLATION_MODELS: Dict[str, List[str]] = {
    TranslationProviderEnum.OPENAI.value: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4o-mini-realtime-preview"],
    TranslationProviderEnum.OPENROUTER.value: [
        "meta-llama/llama-3.3-70b-instruct",
        "google/gemini-flash-1.5",
        "anthropic/claude-3.5-sonnet",
    ],
    TranslationProviderEnum.GEMINI.value: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"],
    TranslationProviderEnum.ANTHROPIC.value: ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"],
    TranslationProviderEnum.GROQ.value: ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "groq/compound-mini"],
    # Self-hosted models are described in the registry; this is only their names.
    TranslationProviderEnum.LOCAL.value: list(LOCAL_TRANSLATION_MODELS),
}


# OpenAI-compatible chat-completions endpoints. These providers share the same
# request/response shape, so both the translation worker (blocking) and the TTS
# worker (streaming) select an endpoint from this single map.
OPENAI_COMPATIBLE_ENDPOINTS: Dict[str, str] = {
    TranslationProviderEnum.OPENAI.value: "https://api.openai.com/v1/chat/completions",
    TranslationProviderEnum.OPENROUTER.value: "https://openrouter.ai/api/v1/chat/completions",
    TranslationProviderEnum.GROQ.value: "https://api.groq.com/openai/v1/chat/completions",
}
