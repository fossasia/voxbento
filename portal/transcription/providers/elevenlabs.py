import asyncio
import json
import logging

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from portal.transcription.errors import TranscriptionAuthError
from portal.transcription.providers.base import (
    BoothTranscriptionState,
    ProviderConfig,
    TranscriptionProvider,
    pcm_to_wav,
)
from portal.transcription.providers.openai import get_http_client

logger = logging.getLogger(__name__)


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
                retry=retry_if_exception_type((httpx.ReadTimeout, httpx.ConnectError, httpx.HTTPStatusError)),
            ):
                with attempt:
                    resp = await client.post(
                        "https://api.elevenlabs.io/v1/speech-to-text", headers=headers, files=files, data=data
                    )
                    if resp.status_code in (429, 502, 503, 504):
                        resp.raise_for_status()

                    if resp.status_code in (401, 403):
                        raise TranscriptionAuthError(
                            "elevenlabs",
                            f"ElevenLabs API key was rejected ({resp.status_code} {resp.reason_phrase}).",
                            resp.status_code,
                        )
                    if resp.status_code == 200:
                        return resp.json().get("text", "").strip()
                    else:
                        logger.error(f"ElevenLabs error status={resp.status_code}")
        except TranscriptionAuthError:
            raise
        except Exception as e:
            logger.error(f"ElevenLabs request failed: {e}")
            raise e
        return ""

    @staticmethod
    def _auth_error_from_frame(data: dict) -> TranscriptionAuthError | None:
        """Return an auth error if this frame is ElevenLabs rejecting the key."""
        message_type = (data.get("message_type") or "").lower()
        detail = str(data.get("error") or data.get("message") or data).strip()
        if message_type in ("auth_error", "authentication_error", "unauthorized"):
            return TranscriptionAuthError(
                "elevenlabs", f"ElevenLabs API key was rejected ({detail})."
            )
        if "error" in message_type and any(
            token in detail.lower() for token in ("api key", "api_key", "unauthorized", "authentication")
        ):
            return TranscriptionAuthError(
                "elevenlabs", f"ElevenLabs API key was rejected ({detail})."
            )
        return None

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

        url = f"wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id={model_variant}&language_code={language_code}&audio_format=pcm_16000&commit_strategy=vad"
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
                        auth_error = self._auth_error_from_frame(init_data)
                        if auth_error:
                            raise auth_error
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
                                    auth_error = self._auth_error_from_frame(data)
                                    if auth_error:
                                        raise auth_error
                                    logger.error(f"[{booth_id}] ElevenLabs Realtime Error: {data}")
                        except TranscriptionAuthError:
                            raise
                        except Exception as e:
                            logger.error(f"[{booth_id}] ElevenLabs WS receiver error: {e}")
                            return "ERROR"

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())

                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED
                    )

                    for task in pending:
                        task.cancel()

                    for task in done:
                        try:
                            error = task.exception()
                        except asyncio.CancelledError:
                            continue
                        if error is not None:
                            raise error

                    if process.returncode is not None:
                        return

                    if sender_task in done and sender_task.result() == "EOF":
                        return

            except TranscriptionAuthError:
                raise
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
                if status in (401, 403):
                    raise TranscriptionAuthError(
                        "elevenlabs",
                        f"ElevenLabs API key was rejected ({status} during the websocket handshake).",
                        status,
                    ) from e
                consecutive_errors += 1
                logger.error(f"[{booth_id}] ElevenLabs connection failed ({consecutive_errors}): {e}")
                if consecutive_errors >= 5:
                    logger.error(f"[{booth_id}] ElevenLabs Realtime connection repeatedly failed. Giving up.")
                    break
                await asyncio.sleep(2)
