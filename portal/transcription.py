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
        api_key = config.get_key()
        if not api_key:
            logger.error(f"Deepgram API key missing")
            return ""
            
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "audio/raw; encoding=linear16; sample_rate=16000; channels=1"
        }
        
        url = f"https://api.deepgram.com/v1/listen?model={model_variant}&language={language_code}&encoding=linear16&sample_rate=16000&channels=1"
        
        try:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(multiplier=1, min=2, max=10),
                stop=stop_after_attempt(3),
                retry=retry_if_exception_type((httpx.ReadTimeout, httpx.ConnectError, httpx.HTTPStatusError))
            ):
                with attempt:
                    resp = await shared_http_client.post(url, headers=headers, content=chunk)
                    if resp.status_code in (429, 502, 503, 504):
                        resp.raise_for_status()
                        
                    if resp.status_code == 200:
                        data = resp.json()
                        try:
                            return data["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
                        except (KeyError, IndexError):
                            pass
                    else:
                        logger.error(f"Deepgram error status={resp.status_code}")
        except Exception as e:
            logger.error(f"Deepgram request failed: {e}")
            raise e
        return ""

class NVIDIAProvider(TranscriptionProvider):
    def __init__(self):
        self._auth = None
        self._asr_service = None

    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        api_key = config.get_key()
        if not api_key:
            logger.error(f"NVIDIA API key missing")
            return ""
            
        def _run_riva():
            import riva.client as rc
            
            # Map standard language code 'en' to 'en-US' since NVIDIA requires region
            riva_lang = "en-US" if language_code == "en" else language_code
            
            if self._asr_service is None:
                from portal.config import settings
                function_id = settings.nvidia_function_id
                
                self._auth = rc.Auth(
                    use_ssl=True,
                    uri="grpc.nvcf.nvidia.com:443",
                    metadata_args=[
                        ["function-id", function_id],
                        ["authorization", f"Bearer {api_key}"]
                    ]
                )
                self._asr_service = rc.ASRService(self._auth)
                
            config_rc = rc.RecognitionConfig(
                encoding=rc.AudioEncoding.LINEAR_PCM,
                sample_rate_hertz=16000,
                audio_channel_count=1,
                language_code=riva_lang,
                max_alternatives=1,
                enable_automatic_punctuation=True
            )
            
            try:
                response = self._asr_service.offline_recognize(chunk, config_rc)
                text = ""
                for result in response.results:
                    if result.alternatives:
                        text += result.alternatives[0].transcript + " "
                return text.strip()
            except Exception as e:
                logger.error(f"NVIDIA API Error: {e}")
                raise e
                
        return await asyncio.to_thread(_run_riva)

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
    
    consecutive_errors = 0
    try:
        chunk_size_bytes = 16000 * 2 * 3 # 3 seconds
        
        while True:
            chunk = await process.stdout.readexactly(chunk_size_bytes)
            if not chunk:
                break
                
            try:
                text = await provider.process_chunk(chunk, language_code, model_size, config)
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
