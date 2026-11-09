from __future__ import annotations

from enum import Enum
from typing import Dict, Set


class ProviderEnum(str, Enum):
    # STT Providers
    LOCAL = "local"
    OPENAI = "openai"
    DEEPGRAM = "deepgram"
    NVIDIA = "nvidia"
    ELEVENLABS = "elevenlabs"
    NONE = "none"
    ATLASCLOUD = "atlascloud"
    SARVAM = "sarvam"


ALLOWED_MODELS: Dict[ProviderEnum, Set[str]] = {
    ProviderEnum.LOCAL: {"tiny", "base", "small", "medium", "large-v2", "large-v3"},
    ProviderEnum.OPENAI: {"whisper-1", "gpt-4o-realtime-preview", "gpt-4o-mini-realtime-preview"},
    ProviderEnum.DEEPGRAM: {"nova-2"},
    ProviderEnum.NVIDIA: {"parakeet-rnnt", "parakeet-ctc"},
    ProviderEnum.ELEVENLABS: {"scribe_v2_realtime"},
    ProviderEnum.NONE: {"none"},
    ProviderEnum.ATLASCLOUD: {"bytedance/seed-asr-2.0"},
    ProviderEnum.SARVAM: {"saaras:v3-realtime"},
}

# VoxBento's ISO 639-1 codes to the BCP-47 codes of saaras:v3-realtime. Sarvam's other
# languages (Konkani, Santali, Manipuri, Bodo, Maithili, Dogri) have no ISO 639-1 code.
SARVAM_LANGUAGE_CODES: Dict[str, str] = {
    "as": "as-IN",
    "bn": "bn-IN",
    "en": "en-IN",
    "gu": "gu-IN",
    "hi": "hi-IN",
    "kn": "kn-IN",
    "ks": "ks-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "ne": "ne-IN",
    "or": "or-IN",
    "pa": "pa-IN",
    "sa": "sa-IN",
    "sd": "sd-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "ur": "ur-IN",
}


def sarvam_language_code(language_code: str | None) -> str | None:
    """Sarvam's code for a stored language, "auto" for none, or None if Sarvam lacks it."""
    if not language_code or language_code == "floor":
        return "auto"
    return SARVAM_LANGUAGE_CODES.get(language_code)


def speech_language_supported(provider: str | None, language_code: str | None) -> bool:
    """Whether this speech provider can transcribe this language."""
    return provider != ProviderEnum.SARVAM or sarvam_language_code(language_code) is not None
