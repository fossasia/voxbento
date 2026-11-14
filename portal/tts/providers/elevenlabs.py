from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import httpx

from portal.globals import get_http_client
from portal.tts.constants import ELEVENLABS_DEFAULT_VOICE_ID
from portal.tts.providers.base import AudioCallback, TTSProvider

logger = logging.getLogger(__name__)

# Eleven v4 is served through the Text to Dialogue API; a single input with one voice is plain speech.
STREAM_URL = "https://api.elevenlabs.io/v1/text-to-dialogue/stream"
# The same 16-bit, 24 kHz mono PCM every other voice produces, so listeners need no change.
OUTPUT_FORMAT = "pcm_24000"
# Fastest first. v4 Turbo is documented for the dialogue WebSocket; when this endpoint refuses it,
# v4 (same voices, same 90+ languages including Thai) speaks instead.
MODELS = ("eleven_v4_turbo", "eleven_v4")
# Refused by the API: a validation error names the request, not a passing outage.
_REJECTED_STATUSES = {400, 422}
# Synthesis of one translated phrase; the translation worker sets its own, shorter deadline.
REQUEST_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# Models this server's key was refused for, so later phrases go straight to one that speaks.
_refused_models: set[str] = set()


class ElevenLabsTTSProvider(TTSProvider):
    """ElevenLabs Eleven v4 over HTTP streaming: one request per translated phrase.

    The translation worker hands over a whole translated phrase, so it is sent in one request:
    the model hears the full sentence and its intonation, and audio streams back as it is made.
    """

    def __init__(self, api_key: str, voice_id: str = ELEVENLABS_DEFAULT_VOICE_ID):
        self.api_key = api_key
        self.voice_id = voice_id

    async def synthesize_stream(
        self,
        *,
        text_chunks: AsyncIterator[str],
        language_code: str,
        voice: str,
        on_audio: AudioCallback,
    ) -> None:
        text = "".join([chunk async for chunk in text_chunks]).strip()
        if not text:
            return
        language = language_code.split("-")[0].lower()
        for model in MODELS:
            if model in _refused_models and model != MODELS[-1]:
                continue
            refused = await self._stream(model, text, language, on_audio)
            if not refused:
                return
            _refused_models.add(model)

    async def _stream(self, model: str, text: str, language: str, on_audio: AudioCallback) -> bool:
        """Stream one phrase's audio. True when the API refused this model, so the next can try.

        Raises ``httpx.HTTPError`` when the stream breaks after audio was sent.
        """
        body = {
            "inputs": [{"text": text, "voice_id": self.voice_id}],
            "model_id": model,
            "language_code": language,
        }
        leftover = b""
        emitted = False
        try:
            async with get_http_client().stream(
                "POST",
                STREAM_URL,
                params={"output_format": OUTPUT_FORMAT},
                headers={"xi-api-key": self.api_key},
                json=body,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status_code in _REJECTED_STATUSES:
                    detail = (await response.aread())[:300]
                    logger.warning(
                        "[TTS] ElevenLabs refused %s for lang %s (HTTP %s): %r",
                        model,
                        language,
                        response.status_code,
                        detail,
                    )
                    return True
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    # Keep whole 16-bit samples: a split sample would shift every later one into noise.
                    data = leftover + chunk
                    usable = len(data) - len(data) % 2
                    leftover = data[usable:]
                    if usable:
                        emitted = True
                        await on_audio(data[:usable])
        except httpx.HTTPStatusError as e:
            logger.error("[TTS] ElevenLabs error for lang %s: HTTP %s", language, e.response.status_code)
        except httpx.HTTPError as e:
            if emitted:
                # Part of the phrase is already out: report the failure so the caller drops the
                # partial audio, instead of playing half a sentence as if it were complete.
                raise
            logger.error("[TTS] ElevenLabs request failed for lang %s: %s", language, type(e).__name__)
        return False
