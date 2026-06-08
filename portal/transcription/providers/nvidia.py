import asyncio
import logging

from portal.transcription.providers.base import TranscriptionProvider, ProviderConfig

logger = logging.getLogger(__name__)

class NVIDIAProvider(TranscriptionProvider):

    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        return ""

    async def run_stream(self, process: asyncio.subprocess.Process, language_code: str, model_variant: str, config: ProviderConfig, broadcast_callback, booth_id: str) -> None:
        import queue
        import riva.client as rc
        
        api_key = config.get_key()
        if not api_key:
            logger.error(f"NVIDIA API key missing")
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
            metadata_args=[
                ["function-id", function_id],
                ["authorization", f"Bearer {api_key}"]
            ]
        )
        asr_service = rc.ASRService(auth)
        
        config_rc = rc.RecognitionConfig(
            encoding=rc.AudioEncoding.LINEAR_PCM,
            sample_rate_hertz=16000,
            audio_channel_count=1,
            language_code=riva_lang,
            max_alternatives=1,
            enable_automatic_punctuation=True
        )
        
        streaming_config = rc.StreamingRecognitionConfig(
            config=config_rc,
            interim_results=False
        )
        
        consecutive_errors = 0
        while process.returncode is None:
            try:
                q = queue.Queue(maxsize=100)
                
                def audio_generator():
                    while True:
                        chunk = q.get()
                        if chunk is None:
                            break
                        yield chunk
                        
                loop = asyncio.get_running_loop()
                
                def run_riva_sync():
                    try:
                        responses = asr_service.streaming_recognize(audio_generator(), streaming_config)
                        for response in responses:
                            if not response.results:
                                continue
                            for result in response.results:
                                if not result.is_final:
                                    continue
                                if not result.alternatives:
                                    continue
                                transcript = result.alternatives[0].transcript.strip()
                                if transcript:
                                    asyncio.run_coroutine_threadsafe(broadcast_callback(booth_id, transcript), loop)
                    except Exception as e:
                        logger.error(f"[{booth_id}] NVIDIA streaming error: {e}")
                        
                thread_task = loop.run_in_executor(None, run_riva_sync)
                
                consecutive_errors = 0
                while True:
                    try:
                        chunk = await process.stdout.read(4096)
                        if not chunk:
                            q.put(None)
                            await thread_task
                            return
                    except Exception as e:
                        logger.error(f"[{booth_id}] Error reading stdout: {e}")
                        q.put(None)
                        await thread_task
                        return
                            
                    if not chunk:
                        q.put(None)
                        await thread_task
                        return # EOF, cleanly exit
                    
                    try:
                        q.put_nowait(chunk)
                    except queue.Full:
                        # Queue is full because thread died or froze, break to reconnect
                        break
                        
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[{booth_id}] NVIDIA streaming loop failed ({consecutive_errors}): {e}")
                if consecutive_errors >= 5:
                    logger.error(f"[{booth_id}] NVIDIA Realtime connection repeatedly failed. Giving up.")
                    break
                await asyncio.sleep(2)
