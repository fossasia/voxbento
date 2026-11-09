from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.parse

import websockets

from portal.outage_log import OutageLog
from portal.transcription.providers.base import (
    BoothTranscriptionState,
    ProviderConfig,
    TranscriptionProvider,
)

logger = logging.getLogger(__name__)

SARVAM_WS_URL = "wss://api.sarvam.ai/speech-to-text-realtime/ws"
DEFAULT_MODEL = "saaras:v3-realtime"
KEEPALIVE_TIMEOUT_SECONDS = 5.0

# Explicit mapping from VoxBento / ISO 639 codes to Sarvam BCP-47 language tags.
# Odia uses 'or-IN' on the realtime WebSocket endpoint per official Sarvam documentation.
SARVAM_LANGUAGE_MAP: dict[str, str] = {
    "hi": "hi-IN",
    "bn": "bn-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "od": "or-IN",
    "or": "or-IN",
    "pa": "pa-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "en": "en-IN",
    "gu": "gu-IN",
    "as": "as-IN",
    "ur": "ur-IN",
    "ne": "ne-IN",
    "kok": "kok-IN",
    "ks": "ks-IN",
    "sd": "sd-IN",
    "sa": "sa-IN",
    "sat": "sat-IN",
    "mni": "mni-IN",
    "brx": "brx-IN",
    "mai": "mai-IN",
    "doi": "doi-IN",
}


def is_sarvam_supported_language(language_code: str | None) -> bool:
    """Return True if language_code is supported by Sarvam AI realtime transcription.

    Empty or 'floor' audio is treated as auto-detection ('auto').
    """
    if not language_code or language_code.strip().lower() in ("floor", ""):
        return True
    return language_code.strip().lower() in SARVAM_LANGUAGE_MAP


def get_sarvam_language(language_code: str | None) -> str:
    """Map ISO language code to Sarvam BCP-47 tag or 'auto' for floor audio.

    Raises ValueError if the language is unsupported.
    """
    if not language_code or language_code.strip().lower() in ("floor", ""):
        return "auto"
    norm = language_code.strip().lower()
    if norm in SARVAM_LANGUAGE_MAP:
        return SARVAM_LANGUAGE_MAP[norm]
    raise ValueError(f"Language '{language_code}' is not supported by Sarvam AI realtime transcription.")


class SarvamProvider(TranscriptionProvider):
    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
        # Real-time streaming only; batch processing is not used for this provider.
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
        prompt: str | None = None,
    ) -> None:
        from portal.transcription.aggregator import CaptionAggregator

        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)
        api_key = config.get_key()
        if not api_key:
            logger.error(f"[{booth_id}] Sarvam API key missing")
            return

        try:
            bcp47_lang = get_sarvam_language(language_code)
        except ValueError as e:
            logger.error(f"[{booth_id}] {e}")
            return

        model = model_variant or DEFAULT_MODEL
        params: dict[str, str] = {
            "model": model,
            "language_code": bcp47_lang,
            "stream_type": "balanced",
            "mode": "transcribe",
            "endpointing": "vad",
        }
        # TODO(#1164): Pass glossary source terms as contextual prompt once #1164 lands.
        if prompt:
            params["prompt"] = prompt

        query_string = urllib.parse.urlencode(params)
        url = f"{SARVAM_WS_URL}?{query_string}"
        headers = {"api-subscription-key": api_key}

        outage = OutageLog(logger, f"[{booth_id}] sarvam_realtime_connection_unavailable")
        consecutive_errors = 0

        while process.returncode is None:
            sender_task: asyncio.Task | None = None
            receiver_task: asyncio.Task | None = None
            try:
                async with websockets.connect(url, additional_headers=headers) as ws:
                    outage.recovered()
                    consecutive_errors = 0

                    async def sender():
                        try:
                            while True:
                                try:
                                    chunk = await asyncio.wait_for(
                                        process.stdout.read(4096), timeout=KEEPALIVE_TIMEOUT_SECONDS
                                    )
                                    if not chunk:
                                        return "EOF"
                                    payload = {
                                        "event": "audio_input",
                                        "audio": base64.b64encode(chunk).decode("utf-8"),
                                    }
                                    await ws.send(json.dumps(payload))
                                except asyncio.TimeoutError:
                                    # Send documented keepalive ping when audio is temporarily idle
                                    await ws.send(json.dumps({"event": "ping"}))
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.error(f"[{booth_id}] Sarvam WS sender error: {e}")
                            return "ERROR"

                    async def receiver():
                        try:
                            async for msg in ws:
                                data = json.loads(msg)
                                event = data.get("event")
                                if event == "transcript.partial":
                                    text = data.get("transcript", "").strip()
                                    if text:
                                        await aggregator.handle_partial(booth_id, text)
                                elif event == "transcript.final":
                                    text = data.get("transcript", "").strip()
                                    if text:
                                        await aggregator.handle_final(booth_id, text)
                                    else:
                                        await aggregator.handle_clear(booth_id)
                                elif event == "error":
                                    is_fatal = data.get("is_fatal", False)
                                    message = data.get("message", "Unknown error")
                                    logger.error(f"[{booth_id}] Sarvam Realtime error: {message} (fatal={is_fatal})")
                                    if is_fatal:
                                        return "FATAL_ERROR"
                                elif event == "session.begin":
                                    logger.debug(f"[{booth_id}] Sarvam session begun: {data.get('request_id')}")
                                elif event == "pong":
                                    pass
                        except asyncio.CancelledError:
                            raise
                        except Exception as e:
                            logger.error(f"[{booth_id}] Sarvam WS receiver error: {e}")
                            return "ERROR"

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())

                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED
                    )

                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)

                    if sender_task in done and sender_task.result() == "EOF":
                        return

                    if receiver_task in done and receiver_task.result() == "FATAL_ERROR":
                        logger.error(f"[{booth_id}] Fatal Sarvam error encountered. Stopping worker.")
                        return

            except Exception as e:
                # Extract HTTP status code from websockets handshake failures if present
                status_code = getattr(e, "status_code", getattr(getattr(e, "response", None), "status_code", None))
                if status_code in (401, 403):
                    logger.error(f"[{booth_id}] Sarvam authentication failed (HTTP {status_code}). Check API key.")
                    return

                consecutive_errors += 1
                outage.failed()
                logger.error(f"[{booth_id}] Sarvam connection failed ({consecutive_errors}): {e}")
                if consecutive_errors >= 5:
                    logger.error(f"[{booth_id}] Sarvam Realtime connection repeatedly failed. Giving up.")
                    break
                await asyncio.sleep(min(2**consecutive_errors, 10))
            finally:
                tasks_to_clean = [t for t in (sender_task, receiver_task) if t is not None and not t.done()]
                for t in tasks_to_clean:
                    t.cancel()
                if tasks_to_clean:
                    await asyncio.gather(*tasks_to_clean, return_exceptions=True)
