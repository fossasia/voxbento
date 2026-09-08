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
# Live interpretation drops audio once a chunk takes ~9-12s, so a slow prediction must fail fast.
MAX_POLL_ATTEMPTS = 12
POLL_INTERVAL_SECONDS = 1.0
# Transient poll failures are retried inside the budget above; anything else is reported at once.
RETRYABLE_STATUS_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


class AtlasCloudError(RuntimeError):
    """Atlas Cloud rejected the request or reported a failed prediction."""


def _error_detail(payload: object) -> str:
    if isinstance(payload, Mapping):
        for key in ("msg", "message", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return "no error message"


def _response_payload(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:
        return None


def _raise_for_error_status(response: httpx.Response, action: str) -> None:
    if response.is_error:
        raise AtlasCloudError(
            f"Atlas Cloud {action} failed with HTTP {response.status_code}: {_error_detail(_response_payload(response))}"
        )


def prediction_data(payload: object) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise ValueError("Atlas Cloud returned a non-object response")
    code = payload.get("code")
    if code not in (None, 0, 200):
        raise AtlasCloudError(f"Atlas Cloud API error {code}: {_error_detail(payload)}")
    data = payload.get("data", payload)
    if not isinstance(data, Mapping):
        raise ValueError(f"Atlas Cloud returned invalid prediction data: {_error_detail(payload)}")
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


def _failed(prediction: Mapping[str, object]) -> AtlasCloudError:
    return AtlasCloudError(f"Atlas Cloud transcription failed: {_error_detail(prediction)}")


class AtlasCloudProvider(TranscriptionProvider):
    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
        """Transcribe one chunk.

        Returns "" only when Atlas Cloud completed and heard no speech. Everything else that goes wrong
        raises, so the worker's consecutive-error handling reports it instead of treating it as silence.
        The generation request is submitted once (it is billed); only the poll requests are retried.
        """
        del booth_state
        api_key = config.get_key()
        if not api_key:
            raise AtlasCloudError("Atlas Cloud API key missing")

        wav_data = pcm_to_wav(chunk)
        payload: dict[str, object] = {
            "model": model_variant,
            "audio_url": "data:audio/wav;base64," + base64.b64encode(wav_data).decode("ascii"),
            "format": "wav",
            "enable_punc": True,
        }
        if language_code:
            payload["language"] = language_code
        headers = {"Authorization": f"Bearer {api_key}"}
        client = get_http_client()

        response = await client.post(
            f"{ATLAS_API_BASE}/api/v1/model/generateAudio",
            headers=headers,
            json=payload,
        )
        _raise_for_error_status(response, "submission")
        prediction = prediction_data(_response_payload(response))
        status = prediction.get("status")
        if status == "completed":
            return transcript_text(prediction)
        if status == "failed":
            raise _failed(prediction)

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
                if result.status_code in RETRYABLE_STATUS_CODES:
                    raise httpx.HTTPStatusError(f"HTTP {result.status_code}", request=result.request, response=result)
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                if attempt == MAX_POLL_ATTEMPTS - 1:
                    raise
                logger.warning("Atlas Cloud prediction poll failed, will retry: %s", exc)
                continue

            _raise_for_error_status(result, "prediction poll")
            prediction = prediction_data(_response_payload(result))
            status = prediction.get("status")
            if status == "completed":
                return transcript_text(prediction)
            if status == "failed":
                raise _failed(prediction)

        raise TimeoutError("Atlas Cloud transcription prediction timed out")
