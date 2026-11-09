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


def get_deepgram_voice_for_language(language_code: str, preferred_voice: str = "") -> str:
    """Returns the best Deepgram voice model for a given ISO 639-1 language code.

    Deepgram voices speak one language each, while a room has one voice for all of its
    TTS languages. *preferred_voice* is used only for the language it speaks.
    """
    # We extract the primary language subtag (e.g., 'en' from 'en-US')
    primary_lang = language_code.split("-")[0].lower()
    if preferred_voice.startswith("aura") and preferred_voice.endswith(f"-{primary_lang}"):
        return preferred_voice
    return DEEPGRAM_VOICE_MAPPING.get(primary_lang, "aura-2-thalia-en")


# Voices an admin can pick for a room, per provider. An empty voice lets the provider
# choose a voice for each target language.
DEEPGRAM_VOICES: list[dict[str, str]] = [
    {"id": "aura-asteria-en", "name": "Asteria (English - Female)"},
    {"id": "aura-luna-en", "name": "Luna (English - Female)"},
    {"id": "aura-stella-en", "name": "Stella (English - Female)"},
    {"id": "aura-athena-en", "name": "Athena (English - Female)"},
    {"id": "aura-hera-en", "name": "Hera (English - Female)"},
    {"id": "aura-orion-en", "name": "Orion (English - Male)"},
    {"id": "aura-arcas-en", "name": "Arcas (English - Male)"},
    {"id": "aura-perseus-en", "name": "Perseus (English - Male)"},
    {"id": "aura-angus-en", "name": "Angus (English - Male)"},
    {"id": "aura-orpheus-en", "name": "Orpheus (English - Male)"},
    {"id": "aura-helios-en", "name": "Helios (English - Male)"},
    {"id": "aura-zeus-en", "name": "Zeus (English - Male)"},
    {"id": "aura-2-thalia-en", "name": "Thalia (English v2)"},
    {"id": "aura-2-celeste-es", "name": "Celeste (Spanish)"},
    {"id": "aura-2-rhea-nl", "name": "Rhea (Dutch)"},
    {"id": "aura-2-agathe-fr", "name": "Agathe (French)"},
    {"id": "aura-2-julius-de", "name": "Julius (German)"},
    {"id": "aura-2-livia-it", "name": "Livia (Italian)"},
    {"id": "aura-2-fujin-ja", "name": "Fujin (Japanese)"},
]

# Supertonic (self-hosted, in-process ONNX) TTS — 10 built-in preset voices.
SUPERTONIC_PRESET_VOICES = ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"]
SUPERTONIC_DEFAULT_VOICE = "M1"

TTS_VOICE_OPTIONS: dict[str, list[dict[str, str]]] = {
    "deepgram": DEEPGRAM_VOICES,
    "supertonic": [
        {"id": v, "name": f"{v} ({'Male' if v.startswith('M') else 'Female'} {v[1:]})"}
        for v in SUPERTONIC_PRESET_VOICES
    ],
}


def normalize_tts_voice(provider: str, voice: str) -> str:
    """Return *voice* if *provider* offers it, otherwise "" so the provider picks per language."""
    voice = (voice or "").strip()
    return voice if any(v["id"] == voice for v in TTS_VOICE_OPTIONS.get(provider, [])) else ""

# Language-to-voice fallback mapping (used when no explicit room voice is set).
SUPERTONIC_VOICE_BY_LANG: Dict[str, str] = {
    "en": "M1",
    "de": "M2",
    "fr": "F1",
    "es": "F2",
    "hi": "M3",
    "ar": "M4",
    "pt": "F3",
    "ru": "F4",
    "ja": "F5",
    "ko": "M5",
    "it": "M1",
}

# ISO 639-1 codes natively supported by Supertonic 3 (31 languages).
# Unsupported languages fall back to the language-agnostic "na" tag.
SUPERTONIC_SUPPORTED_LANGS = {
    "ar",
    "bg",
    "hr",
    "cs",
    "da",
    "nl",
    "en",
    "et",
    "fi",
    "fr",
    "de",
    "el",
    "hi",
    "hu",
    "id",
    "it",
    "ja",
    "ko",
    "lv",
    "lt",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "es",
    "sv",
    "tr",
    "uk",
    "vi",
}
