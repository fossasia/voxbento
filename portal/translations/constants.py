from enum import Enum
from typing import Dict, List


class TranslationProviderEnum(str, Enum):
    # Translation Providers
    LOCAL = "local"
    OPENAI = "openai"
    OPENROUTER = "openrouter"
    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    GROQ = "groq"


TRANSLATION_MODELS: Dict[str, List[str]] = {
    TranslationProviderEnum.OPENAI.value: ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4o-mini-realtime-preview"],
    TranslationProviderEnum.OPENROUTER.value: [
        "meta-llama/llama-3.3-70b-instruct",
        "google/gemini-flash-1.5",
        "anthropic/claude-3.5-sonnet",
    ],
    TranslationProviderEnum.GEMINI.value: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-flash"],
    TranslationProviderEnum.ANTHROPIC.value: ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"],
    TranslationProviderEnum.GROQ.value: ["llama-3.3-70b-versatile", "mixtral-8x7b-32768", "gemma2-9b-it"],
    TranslationProviderEnum.LOCAL.value: [
        "llama-3-8b-instruct",
        "qwen-2.5-7b-instruct",
    ],  # Placeholder for local models
}
