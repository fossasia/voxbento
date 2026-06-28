from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable

# Callback invoked with each PCM (s16le, 24000 Hz, mono) audio chunk produced by
# a provider. Wired by the worker to ConnectionManager.broadcast_audio.
AudioCallback = Callable[[bytes], Awaitable[None]]


class TTSProviderEnum(str, enum.Enum):
    DEEPGRAM = "deepgram"
    SUPERTONIC = "supertonic"


class TTSProvider(ABC):
    """Abstract base for floor-audio TTS providers.

    Implementations consume a stream of translated text chunks (typically from
    an LLM token stream) and emit raw PCM s16le 24 kHz mono audio via ``on_audio``.
    The output format is identical across providers so the listener client never
    needs to change.
    """

    @abstractmethod
    async def synthesize_stream(
        self,
        *,
        text_chunks: AsyncIterator[str],
        language_code: str,
        voice: str,
        on_audio: AudioCallback,
    ) -> None:
        """Consume ``text_chunks`` and emit PCM audio chunks via ``on_audio``."""
        raise NotImplementedError


def get_tts_provider(provider: str, *, deepgram_api_key: str | None = None) -> TTSProvider:
    """Factory returning a TTS provider instance for the given provider name.

    Falls back to Deepgram for unknown provider values to preserve existing
    behaviour for rooms created before the provider column existed.
    """
    # Imported lazily so that the optional ``supertonic`` dependency is only
    # touched when actually requested.
    if provider == TTSProviderEnum.SUPERTONIC.value:
        from portal.tts.providers.supertonic import SupertonicTTSProvider

        return SupertonicTTSProvider()

    from portal.tts.providers.deepgram import DeepgramTTSProvider

    return DeepgramTTSProvider(api_key=deepgram_api_key or "")
