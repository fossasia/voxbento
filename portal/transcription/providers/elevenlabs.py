from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from urllib.parse import urlencode

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential
from websockets.exceptions import InvalidStatus

from portal.transcription.providers.base import (
    BoothTranscriptionState,
    ProviderConfig,
    TranscriptionProvider,
    is_retryable_transcription_error,
    load_glossary_terms,
    pcm_to_wav,
)
from portal.transcription.providers.openai import get_http_client
from portal.translations.vocabulary import fit_speech_terms

logger = logging.getLogger(__name__)

# Allow delayed finals after audio EOF without retaining a session indefinitely.
FINAL_TRANSCRIPT_TIMEOUT_SECONDS = 3.0

REALTIME_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
# The realtime API answers invalid_request instead of session_started past 100 keyterms or
# 1,000 characters in total (counted per code point).
MAX_KEYTERMS = 100
MAX_KEYTERM_CHARS = 1000
# Keyterms travel in the URL, and a request line over about 8 KB is refused with HTTP 400.
# Non-Latin terms grow up to 12 times once percent-encoded, so their encoded size is capped too.
MAX_KEYTERM_URL_CHARS = 6000


def keyterm_cost(term: str) -> int:
    """A term's share of the 1,000 characters, raised when its encoded size would fill the URL first.

    Keeping the total within 1,000 then also keeps the encoded keyterms within the URL budget.
    """
    encoded = len(urlencode([("keyterms", term)])) + 1  # with its "&"
    return max(len(term), -(-encoded * MAX_KEYTERM_CHARS // MAX_KEYTERM_URL_CHARS))


def fit_keyterms(terms: Sequence[str]) -> list[str]:
    return fit_speech_terms(terms, max_terms=MAX_KEYTERMS, budget=MAX_KEYTERM_CHARS, cost=keyterm_cost)


def build_realtime_url(model: str, language_code: str, keyterms: Sequence[str] = ()) -> str:
    params = [
        ("model_id", model),
        ("language_code", language_code),
        ("audio_format", "pcm_16000"),
        ("commit_strategy", "vad"),
    ]
    params.extend(("keyterms", term) for term in keyterms)
    return f"{REALTIME_URL}?{urlencode(params)}"


async def load_keyterms(room_id: int | None) -> list[str]:
    return await load_glossary_terms(room_id, max_terms=MAX_KEYTERMS, budget=MAX_KEYTERM_CHARS, cost=keyterm_cost)


class ElevenLabsProvider(TranscriptionProvider):
    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
        api_key = config.get_key()
        if not api_key:
            logger.error("ElevenLabs API key missing")
            return ""

        wav_data = pcm_to_wav(chunk)
        headers = {"xi-api-key": api_key}
        files = {
            "file": ("audio.wav", wav_data, "audio/wav"),
        }
        data = {"model_id": model_variant, "language_code": language_code}

        client = get_http_client()
        try:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, min=2, max=10),
                stop=stop_after_attempt(3),
                retry=retry_if_exception(is_retryable_transcription_error),
            ):
                with attempt:
                    resp = await client.post(
                        "https://api.elevenlabs.io/v1/speech-to-text", headers=headers, files=files, data=data
                    )
                    # Surface every non-2xx (401/403/400/500…) instead of dropping the chunk silently.
                    resp.raise_for_status()
                    return resp.json().get("text", "").strip()
        except Exception as e:
            logger.error(f"ElevenLabs request failed: {e}")
            raise e
        return ""

    async def run_stream(
        self,
        process: asyncio.subprocess.Process,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        broadcast_callback,
        booth_id: str,
        room_id: int | None = None,
    ) -> None:
        import websockets

        from portal.transcription.aggregator import CaptionAggregator

        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)
        api_key = config.get_key()
        if not api_key:
            logger.error("ElevenLabs API key missing")
            return

        # ElevenLabs Realtime WebSocket only accepts scribe_v2_realtime per the API spec.
        # Normalise any legacy "scribe_v2" value stored in the DB.
        model_variant = "scribe_v2_realtime"

        keyterms = fit_keyterms(config.keyterms) if config.keyterms else await load_keyterms(room_id)
        if keyterms:
            logger.info("[%s] ElevenLabs listening for %d glossary terms", booth_id, len(keyterms))
        url = build_realtime_url(model_variant, language_code, keyterms)
        headers = {"xi-api-key": api_key}

        consecutive_errors = 0
        while process.returncode is None:
            try:
                import base64

                import websockets

                async with websockets.connect(url, additional_headers=headers) as ws:
                    consecutive_errors = 0

                    # Wait for session initialization before sending audio
                    init_msg = await ws.recv()
                    init_data = json.loads(init_msg)
                    if init_data.get("message_type") != "session_started":
                        if keyterms and init_data.get("message_type") == "invalid_request":
                            # The terms are the likeliest cause (e.g. over a limit that changed), so try
                            # once without them: captions without the glossary beat none.
                            logger.warning(
                                "[%s] ElevenLabs refused the session (%s); retrying without the glossary terms",
                                booth_id,
                                init_data.get("error"),
                            )
                            keyterms = []
                            url = build_realtime_url(model_variant, language_code)
                            continue
                        logger.error(f"[{booth_id}] Expected ElevenLabs session_started, got: {init_data}")
                        return

                    async def sender():
                        try:
                            while True:
                                try:
                                    chunk = await process.stdout.readexactly(4096)
                                except asyncio.IncompleteReadError as e:
                                    chunk = e.partial
                                    if not chunk:
                                        return "EOF"
                                if not chunk:
                                    return "EOF"
                                payload = {
                                    "message_type": "input_audio_chunk",
                                    "audio_base_64": base64.b64encode(chunk).decode("utf-8"),
                                    "commit": False,
                                    "sample_rate": 16000,
                                }
                                await ws.send(json.dumps(payload))
                        except Exception as e:
                            logger.error(f"[{booth_id}] ElevenLabs WS sender error: {e}")
                            return "ERROR"

                    async def receiver():
                        try:
                            async for msg in ws:
                                data = json.loads(msg)
                                message_type = data.get("message_type")
                                if (
                                    message_type == "committed_transcript"
                                    or message_type == "committed_transcript_with_timestamps"
                                ):
                                    text = data.get("text", "").strip()
                                    if text:
                                        await aggregator.handle_final(booth_id, text)
                                    else:
                                        await aggregator.handle_clear(booth_id)
                                elif message_type == "partial_transcript":
                                    text = data.get("text", "").strip()
                                    if text:
                                        await aggregator.handle_partial(booth_id, text)
                                elif message_type and "error" in message_type:
                                    logger.error(f"[{booth_id}] ElevenLabs Realtime Error: {data}")
                        except Exception as e:
                            logger.error(f"[{booth_id}] ElevenLabs WS receiver error: {e}")
                            return "ERROR"

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())

                    try:
                        done, _ = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
                        if sender_task in done and sender_task.result() == "EOF":
                            # Commit does not close the socket. Keep receiving delayed finals
                            # for a bounded interval, including replies to earlier VAD commits.
                            await ws.send(
                                json.dumps(
                                    {
                                        "message_type": "input_audio_chunk",
                                        "audio_base_64": "",
                                        "commit": True,
                                        "sample_rate": 16000,
                                    }
                                )
                            )
                            try:
                                await asyncio.wait_for(receiver_task, FINAL_TRANSCRIPT_TIMEOUT_SECONDS)
                            except TimeoutError:
                                logger.debug("[%s] Final transcript drain finished", booth_id)
                            return
                        if receiver_task in done:
                            receiver_task.result()
                        raise OSError("Speech WebSocket disconnected")
                    finally:
                        for task in (sender_task, receiver_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(sender_task, receiver_task, return_exceptions=True)

            except Exception as e:
                if keyterms and isinstance(e, InvalidStatus) and e.response.status_code == 400:
                    # A request line too long for the server is refused before the session starts.
                    logger.warning(
                        "[%s] ElevenLabs refused the glossary terms (HTTP 400); continuing without them", booth_id
                    )
                    keyterms = []
                    url = build_realtime_url(model_variant, language_code)
                    continue
                consecutive_errors += 1
                logger.error(f"[{booth_id}] ElevenLabs connection failed ({consecutive_errors}): {e}")
                if consecutive_errors >= 5:
                    logger.error(f"[{booth_id}] ElevenLabs Realtime connection repeatedly failed. Giving up.")
                    break
                await asyncio.sleep(2)
