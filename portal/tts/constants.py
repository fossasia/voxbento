from typing import Dict

# Deepgram Aura-2 Voice Mappings
# Fallback to English if a language is not natively supported.

DEEPGRAM_VOICE_MAPPING: Dict[str, str] = {
    "en": "aura-2-thalia-en",
    "es": "aura-2-celeste-es",
    "nl": "aura-2-rhea-nl",
    "fr": "aura-2-agathe-fr",
    "de": "aura-2-julius-de",
    "it": "aura-2-livia-it",
    "ja": "aura-2-fujin-ja",
}

def get_deepgram_voice_for_language(language_code: str) -> str:
    """Returns the best Deepgram voice model for a given ISO 639-1 language code."""
    # We extract the primary language subtag (e.g., 'en' from 'en-US')
    primary_lang = language_code.split('-')[0].lower()
    return DEEPGRAM_VOICE_MAPPING.get(primary_lang, "aura-2-thalia-en")


# Supertonic (self-hosted, in-process ONNX) TTS — 10 built-in preset voices.
SUPERTONIC_PRESET_VOICES = ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"]
SUPERTONIC_DEFAULT_VOICE = "M1"

# Language-to-voice fallback mapping (used when no explicit room voice is set).
SUPERTONIC_VOICE_BY_LANG: Dict[str, str] = {
    "en": "M1", "de": "M2", "fr": "F1", "es": "F2",
    "hi": "M3", "ar": "M4", "pt": "F3", "ru": "F4",
    "ja": "F5", "ko": "M5", "it": "M1",
}

# ISO 639-1 codes natively supported by Supertonic 3 (31 languages).
# Unsupported languages fall back to the language-agnostic "na" tag.
SUPERTONIC_SUPPORTED_LANGS = {
    "ar", "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr",
    "de", "el", "hi", "hu", "id", "it", "ja", "ko", "lv", "lt",
    "pl", "pt", "ro", "ru", "sk", "sl", "es", "sv", "tr", "uk", "vi",
}
