from typing import Dict

# Currently Deepgram Aura models are primarily English.
# If other languages become available, we can add them to this mapping.
# For now, we fallback to an English voice, which may speak with an accent.

DEEPGRAM_VOICE_MAPPING: Dict[str, str] = {
    "en": "aura-asteria-en",
    # Add other mappings if/when Deepgram releases multi-lingual Aura models:
    # "es": "aura-asteria-es",
    # "fr": "aura-asteria-fr",
}

def get_deepgram_voice_for_language(language_code: str) -> str:
    """Returns the best Deepgram voice model for a given ISO 639-1 language code."""
    return DEEPGRAM_VOICE_MAPPING.get(language_code.lower(), "aura-asteria-en")
