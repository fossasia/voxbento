import asyncio
import json
import logging

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt, wait_exponential

import portal.transcription as ts
from portal.transcription.providers.base import (
    BoothTranscriptionState,
    ProviderConfig,
    TranscriptionProvider,
    pcm_to_wav,
)

logger = logging.getLogger(__name__)


def get_http_client() -> httpx.AsyncClient:
    if ts.shared_http_client is None:
        ts.shared_http_client = httpx.AsyncClient(timeout=10.0)
    return ts.shared_http_client


class OpenAIProvider(TranscriptionProvider):
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
            logger.error("OpenAI API key missing")
            return ""

        wav_data = pcm_to_wav(chunk)
        headers = {"Authorization": f"Bearer {api_key}"}
        files = {
            "file": ("audio.wav", wav_data, "audio/wav"),
        }
        data = {"model": model_variant, "language": language_code}

        client = get_http_client()
        try:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, min=2, max=10),
                stop=stop_after_attempt(3),
                retry=retry_if_exception_type((httpx.ReadTimeout, httpx.ConnectError, httpx.HTTPStatusError)),
            ):
                with attempt:
                    resp = await client.post(
                        "https://api.openai.com/v1/audio/transcriptions", headers=headers, files=files, data=data
                    )
                    if resp.status_code in (429, 502, 503, 504):
                        resp.raise_for_status()

                    if resp.status_code == 200:
                        return resp.json().get("text", "").strip()
                    else:
                        logger.error(f"OpenAI error status={resp.status_code}")
        except Exception as e:
            logger.error(f"OpenAI request failed: {e}")
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
        if model_variant in ("whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"):
            await super().run_stream(
                process, language_code, model_variant, config, broadcast_callback, booth_id, room_id
            )
            return

        from portal.transcription.aggregator import CaptionAggregator

        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)

        api_key = config.get_key()
        if not api_key:
            logger.error("OpenAI API key missing")
            return

        url = f"wss://api.openai.com/v1/realtime?model={model_variant}"
        headers = {"Authorization": f"Bearer {api_key}", "OpenAI-Beta": "realtime=v1"}

        consecutive_errors = 0
        while process.returncode is None:
            try:
                import base64

                import numpy as np
                import websockets

                async with websockets.connect(url, additional_headers=headers) as ws:
                    consecutive_errors = 0

                    session_update = {
                        "type": "session.update",
                        "session": {
                            "type": "transcription",
                            "audio": {
                                "input": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "transcription": {"model": model_variant, "language": language_code},
                                }
                            },
                        },
                    }
                    await ws.send(json.dumps(session_update))

                    async def sender():
                        try:
                            silence_frames = 0
                            while True:
                                chunk = await process.stdout.read(4096)
                                if not chunk:
                                    return "EOF"

                                # Manual VAD: Calculate RMS energy
                                audio_data = np.frombuffer(chunk, dtype=np.int16)
                                rms = np.sqrt(np.mean(np.square(audio_data.astype(np.float32))))

                                if rms < 500.0:
                                    silence_frames += 1
                                else:
                                    silence_frames = 0

                                payload = {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(chunk).decode("utf-8"),
                                }
                                await ws.send(json.dumps(payload))

                                # If silence for ~1 second (12 frames @ 24kHz)
                                if silence_frames == 12:
                                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                                    silence_frames = 0

                        except Exception as e:
                            logger.error(f"[{booth_id}] OpenAI WS sender error: {e}")
                            return "ERROR"

                    async def receiver():
                        try:
                            current_transcription = ""
                            async for msg in ws:
                                data = json.loads(msg)
                                event_type = data.get("type")

                                if event_type == "conversation.item.input_audio_transcription.completed":
                                    transcript = data.get("transcript", "").strip()
                                    if transcript:
                                        await aggregator.handle_final(booth_id, transcript)
                                    current_transcription = ""

                                elif event_type == "conversation.item.input_audio_transcription.delta":
                                    delta = data.get("delta", "")
                                    if delta:
                                        current_transcription += delta
                                        await aggregator.handle_partial(booth_id, current_transcription)

                                elif event_type == "input_audio_buffer.speech_stopped":
                                    if current_transcription:
                                        await aggregator.handle_clear(booth_id)
                                    current_transcription = ""

                        except Exception as e:
                            logger.error(f"[{booth_id}] OpenAI WS receiver error: {e}")
                            return "ERROR"

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())

                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED
                    )

                    if sender_task in done and sender_task.result() == "EOF":
                        try:
                            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                            await receiver_task
                        except Exception:
                            pass
                        for task in pending:
                            task.cancel()
                        return

                    for task in pending:
                        task.cancel()

                    raise Exception("WebSocket disconnected.")

            except Exception as e:
                consecutive_errors += 1
                logger.warning(f"[{booth_id}] OpenAI Realtime connection failed ({consecutive_errors}): {e}")
                if consecutive_errors > 5:
                    logger.error(f"[{booth_id}] OpenAI Realtime connection repeatedly failed. Giving up.")
                    break
                await asyncio.sleep(2)
