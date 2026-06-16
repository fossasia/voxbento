import asyncio
import logging

from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig, TranscriptionProvider

logger = logging.getLogger(__name__)


class NVIDIAProvider(TranscriptionProvider):
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
        try:
            import riva.client as rc
        except ImportError:
            raise RuntimeError("nvidia-riva-client is not installed. Please install the [nvidia] extra.")

        import queue
        import threading

        api_key = config.get_key()
        if not api_key:
            logger.error("NVIDIA API key missing")
            return

        riva_lang = "en-US" if language_code == "en" else language_code

        from portal.config import settings

        function_id = settings.nvidia_function_id
        if not function_id:
            logger.error("NVIDIA_FUNCTION_ID is not configured in the environment.")
            return

        auth = rc.Auth(
            use_ssl=True,
            uri="grpc.nvcf.nvidia.com:443",
            metadata_args=[["function-id", function_id], ["authorization", f"Bearer {api_key}"]],
        )
        asr_service = rc.ASRService(auth)

        config_rc = rc.RecognitionConfig(
            encoding=rc.AudioEncoding.LINEAR_PCM,
            sample_rate_hertz=16000,
            audio_channel_count=1,
            language_code=riva_lang,
            max_alternatives=1,
            enable_automatic_punctuation=True,
        )

        from portal.transcription.aggregator import CaptionAggregator

        aggregator = CaptionAggregator(broadcast_callback, room_id=room_id)

        streaming_config = rc.StreamingRecognitionConfig(config=config_rc, interim_results=True)

        consecutive_errors = 0
        while process.returncode is None:
            try:
                q = queue.Queue(maxsize=100)

                stop_event = threading.Event()

                def audio_generator(stop_ev):
                    while not stop_ev.is_set():
                        try:
                            chunk = q.get(timeout=0.2)
                            if chunk is None:
                                break
                            yield chunk
                        except queue.Empty:
                            continue

                loop = asyncio.get_running_loop()

                def run_riva_sync(stop_ev):
                    try:
                        responses = asr_service.streaming_recognize(audio_generator(stop_ev), streaming_config)
                        for response in responses:
                            if stop_ev.is_set():
                                break
                            if not response.results:
                                continue
                            for result in response.results:
                                if not result.alternatives:
                                    continue
                                transcript = result.alternatives[0].transcript.strip()
                                if not transcript:
                                    continue

                                if result.is_final:
                                    asyncio.run_coroutine_threadsafe(
                                        aggregator.handle_final(booth_id, transcript), loop
                                    )
                                else:
                                    asyncio.run_coroutine_threadsafe(
                                        aggregator.handle_partial(booth_id, transcript), loop
                                    )
                    except Exception as e:
                        logger.error(f"[{booth_id}] NVIDIA streaming error: {e}")

                thread_task = loop.run_in_executor(None, run_riva_sync, stop_event)

                try:
                    consecutive_errors = 0
                    while True:
                        try:
                            chunk = await process.stdout.read(4096)
                        except Exception as e:
                            logger.error(f"[{booth_id}] Error reading stdout: {e}")
                            q.put(None)
                            await thread_task
                            return

                        if not chunk:
                            q.put(None)
                            await thread_task
                            return  # EOF, cleanly exit

                        try:
                            q.put_nowait(chunk)
                        except queue.Full:
                            # Queue is full because thread died or froze, break to reconnect
                            break
                finally:
                    stop_event.set()

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[{booth_id}] NVIDIA streaming loop failed ({consecutive_errors}): {e}")
                if consecutive_errors >= 5:
                    logger.error(f"[{booth_id}] NVIDIA Realtime connection repeatedly failed. Giving up.")
                    break
                await asyncio.sleep(2)
