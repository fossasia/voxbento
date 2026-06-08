import asyncio
import logging
import numpy as np
import io
import wave
import httpx
import json
import threading
from faster_whisper import WhisperModel
from portal.config import settings

logger = logging.getLogger(__name__)

# --- Local Model Caching ---
_current_model_size = None
_current_model = None
_model_lock = threading.Lock()

def get_model(model_size: str):
    global _current_model_size, _current_model
    if _current_model_size == model_size and _current_model is not None:
        return _current_model
    with _model_lock:
        if _current_model_size != model_size:
            logger.info(f"Loading faster-whisper model: {model_size}")
            if _current_model is not None:
                del _current_model
                import gc
                gc.collect()
            _current_model = WhisperModel(model_size, device="cpu", compute_type="int8")
            _current_model_size = model_size
    return _current_model

# --- Audio Utilities ---
def pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()

from dataclasses import dataclass
from tenacity import AsyncRetrying, wait_exponential, stop_after_attempt, retry_if_exception_type
import httpx

# --- Providers ---
shared_http_client: httpx.AsyncClient | None = None

@dataclass
class ProviderConfig:
    api_key: str | None
    
    def get_key(self) -> str | None:
        return self.api_key

class TranscriptionProvider:
    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        raise NotImplementedError

    async def run_stream(self, process: asyncio.subprocess.Process, language_code: str, model_variant: str, config: ProviderConfig, broadcast_callback, booth_id: str) -> None:
        consecutive_errors = 0
        chunk_size_bytes = 16000 * 2 * 3 # 3 seconds
        
        while True:
            chunk = await process.stdout.readexactly(chunk_size_bytes)
            if not chunk:
                break
                
            try:
                text = await self.process_chunk(chunk, language_code, model_variant, config)
                consecutive_errors = 0
                
                if text:
                    logger.debug(f"[{booth_id}] Transcribed: {text}")
                    await broadcast_callback(booth_id, text)
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[{booth_id}] Provider error ({consecutive_errors}/3): {e}")
                if consecutive_errors >= 3:
                    await broadcast_callback(booth_id, "[Transcription provider failed. Check logs.]")
                    break

class LocalProvider(TranscriptionProvider):
    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        audio_data = np.frombuffer(chunk, np.int16).astype(np.float32) / 32768.0
        return await asyncio.to_thread(self._run_inference, audio_data, language_code, model_variant)
        
    def _run_inference(self, audio_data: np.ndarray, language_code: str, model_size: str) -> str:
        model = get_model(model_size)
        segments, _ = model.transcribe(audio_data, beam_size=5, vad_filter=True, language=language_code)
        text = " ".join(segment.text for segment in segments)
        return text.strip()

class OpenAIProvider(TranscriptionProvider):
    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        api_key = config.get_key()
        if not api_key:
            logger.error(f"OpenAI API key missing")
            return ""
        
        wav_data = pcm_to_wav(chunk)
        headers = {"Authorization": f"Bearer {api_key}"}
        files = {
            "file": ("audio.wav", wav_data, "audio/wav"),
        }
        data = {
            "model": model_variant,
            "language": language_code
        }
        
        try:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, min=2, max=10),
                stop=stop_after_attempt(3),
                retry=retry_if_exception_type((httpx.ReadTimeout, httpx.ConnectError, httpx.HTTPStatusError))
            ):
                with attempt:
                    resp = await shared_http_client.post("https://api.openai.com/v1/audio/transcriptions", headers=headers, files=files, data=data)
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

class DeepgramProvider(TranscriptionProvider):
    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        # Fallback just in case, but run_stream handles it directly now.
        return ""

    async def run_stream(self, process: asyncio.subprocess.Process, language_code: str, model_variant: str, config: ProviderConfig, broadcast_callback, booth_id: str) -> None:
        import websockets
        import json
        
        api_key = config.get_key()
        if not api_key:
            logger.error(f"Deepgram API key missing")
            return
            
        url = f"wss://api.deepgram.com/v1/listen?model={model_variant}&language={language_code}&encoding=linear16&sample_rate=16000&channels=1&interim_results=false&keepalive=true&endpointing=2000&smart_format=true&punctuate=true"
        headers = {"Authorization": f"Token {api_key}"}
        
        consecutive_errors = 0
        while process.returncode is None:
            try:
                async with websockets.connect(url, additional_headers=headers) as ws:
                    consecutive_errors = 0
                    
                    async def sender():
                        try:
                            while True:
                                chunk = await process.stdout.read(4096)
                                if not chunk:
                                    return "EOF"
                                await ws.send(chunk)
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
                                        if transcript:
                                            logger.debug(f"[{booth_id}] Transcribed: {transcript}")
                                            await broadcast_callback(booth_id, transcript)
                                    except (KeyError, IndexError):
                                        pass
                        except Exception as e:
                            logger.error(f"[{booth_id}] Deepgram WS receiver error: {e}")
                            return "ERROR"
                            
                    sender_task = asyncio.create_task(sender())
                    receiver_task = asyncio.create_task(receiver())
                    
                    done, pending = await asyncio.wait(
                        [sender_task, receiver_task], 
                        return_when=asyncio.FIRST_COMPLETED
                    )
                    
                    for task in pending:
                        task.cancel()
                        
                    if sender_task in done and sender_task.result() == "EOF":
                        return # Clean exit

            except Exception as e:
                consecutive_errors += 1
                logger.error(f"[{booth_id}] Deepgram connection failed ({consecutive_errors}/3): {e}")
                if consecutive_errors >= 3:
                    await broadcast_callback(booth_id, "[Transcription provider failed. Check logs.]")
                    return
                await asyncio.sleep(2)

class NVIDIAProvider(TranscriptionProvider):
    def __init__(self):
        self._services = {}

    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        # Fallback just in case, but run_stream handles it directly now.
        return ""

    async def run_stream(self, process: asyncio.subprocess.Process, language_code: str, model_variant: str, config: ProviderConfig, broadcast_callback, booth_id: str) -> None:
        import queue
        import riva.client as rc
        
        api_key = config.get_key()
        if not api_key:
            logger.error(f"NVIDIA API key missing")
            return
            
        riva_lang = "en-US" if language_code == "en" else language_code
        
        if api_key not in self._services:
            from portal.config import settings
            function_id = settings.nvidia_function_id
            
            auth = rc.Auth(
                use_ssl=True,
                uri="grpc.nvcf.nvidia.com:443",
                metadata_args=[
                    ["function-id", function_id],
                    ["authorization", f"Bearer {api_key}"]
                ]
            )
            self._services[api_key] = rc.ASRService(auth)
            
        asr_service = self._services[api_key]
        
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
                        # Don't broadcast error immediately, let it retry
                        
                thread_task = loop.run_in_executor(None, run_riva_sync)
                
                consecutive_errors = 0
                while True:
                    # Read 4096 byte chunks natively
                    chunk = await process.stdout.read(4096)
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
                logger.error(f"[{booth_id}] NVIDIA connection failed ({consecutive_errors}/3): {e}")
                if consecutive_errors >= 3:
                    await broadcast_callback(booth_id, "[Transcription provider failed. Check logs.]")
                    return
                await asyncio.sleep(2)
            finally:
                q.put(None)
                try:
                    await thread_task
                except Exception:
                    pass

class ElevenLabsProvider(TranscriptionProvider):
    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        api_key = config.get_key()
        if not api_key:
            logger.error(f"ElevenLabs API key missing")
            return ""
            
        wav_data = pcm_to_wav(chunk)
        headers = {"xi-api-key": api_key}
        files = {
            "file": ("audio.wav", wav_data, "audio/wav"),
        }
        data = {
            "model_id": model_variant,
            "language_code": language_code
        }
        
        try:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, min=2, max=10),
                stop=stop_after_attempt(3),
                retry=retry_if_exception_type((httpx.ReadTimeout, httpx.ConnectError, httpx.HTTPStatusError))
            ):
                with attempt:
                    resp = await shared_http_client.post("https://api.elevenlabs.io/v1/speech-to-text", headers=headers, files=files, data=data)
                    if resp.status_code in (429, 502, 503, 504):
                        resp.raise_for_status()
                        
                    if resp.status_code == 200:
                        return resp.json().get("text", "").strip()
                    else:
                        logger.error(f"ElevenLabs error status={resp.status_code}")
        except Exception as e:
            logger.error(f"ElevenLabs request failed: {e}")
            raise e
        return ""

PROVIDERS = {
    'local': LocalProvider(),
    'openai': OpenAIProvider(),
    'deepgram': DeepgramProvider(),
    'nvidia': NVIDIAProvider(),
    'elevenlabs': ElevenLabsProvider(),
}

# --- Worker ---
active_workers: dict[str, asyncio.Task] = {}
active_processes: dict[str, asyncio.subprocess.Process] = {}

async def transcription_worker(event_slug: str, language_code: str, booth_id: str, broadcast_callback, provider_name: str, model_size: str, config: ProviderConfig):
    logger.info(f"Starting {provider_name} transcription worker for booth {booth_id}")
    rtsp_url = f"rtsp://mediamtx:8554/{event_slug}/{language_code}"
    
    provider = PROVIDERS.get(provider_name, PROVIDERS['local'])
    
    cmd = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ar", "16000",
        "-ac", "1",
        "-"
    ]
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL
    )
    active_processes[booth_id] = process
    
    try:
        await provider.run_stream(process, language_code, model_size, config, broadcast_callback, booth_id)
    except asyncio.IncompleteReadError:
        logger.error(f"[{booth_id}] ffmpeg stream ended abruptly.")
    except asyncio.CancelledError:
        logger.info(f"[{booth_id}] Transcription worker cancelled.")
        raise
    except Exception as e:
        logger.error(f"[{booth_id}] Transcription error: {e}")
    finally:
        if process.returncode is None:
            try:
                process.terminate()
                await process.wait()
            except ProcessLookupError:
                pass
        active_processes.pop(booth_id, None)
        active_workers.pop(booth_id, None)
        logger.info(f"[{booth_id}] Transcription worker exited.")

async def start_transcription_worker(event_slug: str, language_code: str, booth_id: str, broadcast_callback, provider: str, model_size: str, config: ProviderConfig):
    if booth_id in active_workers:
        logger.info(f"Transcription worker for {booth_id} is already running.")
        return
        
    task = asyncio.create_task(transcription_worker(event_slug, language_code, booth_id, broadcast_callback, provider, model_size, config))
    active_workers[booth_id] = task

async def stop_transcription_worker(booth_id: str):
    task = active_workers.get(booth_id)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Task finished with exception: {e}")
    
    process = active_processes.get(booth_id)
    if process and process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
