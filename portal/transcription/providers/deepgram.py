import asyncio
import json
import logging

from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig, TranscriptionProvider

logger = logging.getLogger(__name__)


class DeepgramProvider(TranscriptionProvider):
    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
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
        # Local import required to avoid circular dependency
        import websockets

        # Local import required to avoid circular dependency
        from portal.transcription.aggregator import CaptionAggregator

        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)

        api_key = config.get_key()
        if not api_key:
            logger.error("Deepgram API key missing")
            return

        url = f"wss://api.deepgram.com/v1/listen?model={model_variant}&language={language_code}&encoding=linear16&sample_rate=16000&channels=1&interim_results=true&keepalive=true&endpointing=2000&smart_format=true&punctuate=true"
        headers = {"Authorization": f"Token {api_key}"}

        consecutive_errors = 0
        while process.returncode is None:
            try:
                async with websockets.connect(url, additional_headers=headers) as ws:
                    consecutive_errors = 0

                    async def sender():
                        try:
                            while True:
                                try:
                                    chunk = await asyncio.wait_for(process.stdout.read(4096), timeout=5.0)
                                    if not chunk:
                                        return "EOF"
                                    await ws.send(chunk)
                                except asyncio.TimeoutError:
                                    await ws.send(json.dumps({"type": "KeepAlive"}))
                        except Exception as e:
                            logger.error(f"[{booth_id}] Deepgram WS sender error: {e}")
                            return "ERROR"

                    async def receiver():
                        try:
                            async for msg in ws:
                                data = json.loads(msg)
                                if "channel" in data:
                                    try:
                                        transcript = data["channel"]["alternatives"][0]["transcript"].strip()
                                        is_final = data.get("is_final", False)
                                        speech_final = data.get("speech_final", False)

                                        if transcript:
                                            if speech_final or is_final:
                                                await aggregator.handle_final(booth_id, transcript)
                                            else:
                                                await aggregator.handle_partial(booth_id, transcript)

                                        if speech_final and not transcript:
                                            await aggregator.handle_clear(booth_id)
                                    except (KeyError, IndexError):
                                        pass
                        except Exception as e:
                            logger.error(f"[{booth_id}] Deepgram WS receiver error: {e}")
                            return "ERROR"

                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())

                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED
                    )

                    if sender_task in done and sender_task.result() == "EOF":
                        try:
                            await ws.send(b"")
                            await receiver_task
                        except Exception:
                            pass
                        for task in pending:
                            task.cancel()
                        return  # Clean exit

                    for task in pending:
                        task.cancel()

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[{booth_id}] Deepgram connection failed ({consecutive_errors}): {e}")
                if consecutive_errors >= 5:
                    logger.error(f"[{booth_id}] Deepgram Realtime connection repeatedly failed. Giving up.")
                    break
                await asyncio.sleep(2)
