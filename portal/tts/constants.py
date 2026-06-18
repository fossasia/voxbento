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
