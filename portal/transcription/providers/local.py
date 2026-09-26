from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig

from portal.ray_serve.client import RayClient
from portal.transcription.providers.base import TranscriptionProvider

logger = logging.getLogger(__name__)


class LocalProvider(TranscriptionProvider):
    def __init__(self):
        self.ray_client = RayClient()

    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: "ProviderConfig",
        booth_state: "BoothTranscriptionState" | None = None,
    ) -> str:
        if booth_state:
            # Append overlap buffer (last 1.0s) to current 3.0s chunk -> 4.0s total
            overlap_audio = booth_state.overlap_buffer + chunk

            # Save the last 1.0s of the *current* chunk for the next iteration
            # 16000 hz * 2 bytes/sample * 1 channel * 1.0 seconds = 32000 bytes
            overlap_bytes = 32000
            if len(chunk) >= overlap_bytes:
                booth_state.overlap_buffer = chunk[-overlap_bytes:]
            else:
                booth_state.overlap_buffer = chunk
        else:
            overlap_audio = chunk

        # Convert bytes to float32 numpy array, same as original
        audio_data = np.frombuffer(overlap_audio, np.int16).astype(np.float32) / 32768.0

        # Encode float32 numpy array bytes directly into base64 for JSON transmission
        audio_bytes = audio_data.tobytes()
        audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

        payload = {
            "audio_b64": audio_b64,
            "language_code": language_code,
            "model_size": model_variant,
        }

        headers = {"X-Serve-Multiplexed-Model-Id": model_variant}

        try:
            result = await self.ray_client.predict("transcriber", payload, headers=headers)
            if "transcribed_text" in result:
                return result["transcribed_text"]
            else:
                logger.error(f"[Whisper Ray] Unexpected response: {result}")
                raise RuntimeError(f"Unexpected response from Ray Serve: {result}")
        except Exception as e:
            logger.error(f"[Whisper Ray] Request to Ray Serve failed: {e}")
            raise
