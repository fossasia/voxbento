from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Mapping

import httpx

from portal.globals import get_http_client
from portal.transcription.providers.base import (
    BoothTranscriptionState,
    ProviderConfig,
    TranscriptionProvider,
    pcm_to_wav,
)

logger = logging.getLogger(__name__)

ATLAS_API_BASE = "https://api.atlascloud.ai"
MAX_POLL_ATTEMPTS = 30
POLL_INTERVAL_SECONDS = 1.0


def prediction_data(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("Atlas Cloud returned a non-object response")
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        raise ValueError("Atlas Cloud returned invalid prediction data")
    return data


def transcript_text(prediction: Mapping[str, object]) -> str:
    stt_result = prediction.get("stt_result")
    if isinstance(stt_result, Mapping):
        text = stt_result.get("text")
        if isinstance(text, str):
            return text.strip()

    outputs = prediction.get("outputs")
    if isinstance(outputs, list) and outputs and isinstance(outputs[0], str):
        return outputs[0].strip()
    return ""


class AtlasCloudProvider(TranscriptionProvider):
    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
        del booth_state
        api_key = config.get_key()
        if not api_key:
            logger.error("Atlas Cloud API key missing")
            return ""

        wav_data = pcm_to_wav(chunk)
        payload = {
            "model": model_variant,
            "audio_url": "data:audio/wav;base64," + base64.b64encode(wav_data).decode("ascii"),
            "format": "wav",
            "language": language_code,
            "enable_punc": True,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        client = get_http_client()

        response = await client.post(
            f"{ATLAS_API_BASE}/api/v1/model/generateAudio",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        prediction = prediction_data(response.json())
        status = prediction.get("status")
        if status == "completed":
            return transcript_text(prediction)
        if status == "failed":
            logger.error("Atlas Cloud transcription failed during submission")
            return ""

        request_id = prediction.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("Atlas Cloud response did not include a prediction id")

        for attempt in range(MAX_POLL_ATTEMPTS):
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                result = await client.get(
                    f"{ATLAS_API_BASE}/api/v1/model/prediction/{request_id}",
                    headers=headers,
                )
                result.raise_for_status()
            except httpx.RequestError as exc:
                if attempt == MAX_POLL_ATTEMPTS - 1:
                    raise
                logger.warning("Atlas Cloud prediction poll failed: %s", exc)
                continue

            prediction = prediction_data(result.json())
            status = prediction.get("status")
            if status == "completed":
                return transcript_text(prediction)
            if status == "failed":
                logger.error("Atlas Cloud transcription prediction failed")
                return ""

        raise TimeoutError("Atlas Cloud transcription prediction timed out")
