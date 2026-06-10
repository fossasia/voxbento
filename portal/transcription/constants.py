from enum import Enum

class ProviderEnum(str, Enum):
    LOCAL = "local"
    OPENAI = "openai"
    DEEPGRAM = "deepgram"
    NVIDIA = "nvidia"
    ELEVENLABS = "elevenlabs"

ALLOWED_MODELS = {
    ProviderEnum.LOCAL: {"tiny", "base", "small", "medium", "large-v2", "large-v3"},
    ProviderEnum.OPENAI: {"whisper-1", "gpt-4o-realtime-preview", "gpt-4o-mini-realtime-preview"},
    ProviderEnum.DEEPGRAM: {"nova-2"},
    ProviderEnum.NVIDIA: {"parakeet-rnnt", "parakeet-ctc"},
    ProviderEnum.ELEVENLABS: {"scribe_v2_realtime"}
}
