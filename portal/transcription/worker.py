import asyncio
import logging
from typing import Dict, Any

from portal.transcription.providers.base import ProviderConfig
from portal.transcription.providers.local import LocalProvider
from portal.transcription.providers.openai import OpenAIProvider
from portal.transcription.providers.deepgram import DeepgramProvider
from portal.transcription.providers.nvidia import NVIDIAProvider
from portal.transcription.providers.elevenlabs import ElevenLabsProvider

logger = logging.getLogger(__name__)

PROVIDERS = {
    'local': LocalProvider(),
    'openai': OpenAIProvider(),
    'deepgram': DeepgramProvider(),
    'nvidia': NVIDIAProvider(),
    'elevenlabs': ElevenLabsProvider(),
}

# --- Worker State ---
active_workers_lock = asyncio.Lock()
MAX_TOTAL_WORKERS = 10
# Store dict containing {"task": asyncio.Task, "provider": str, "stderr_task": asyncio.Task | None}
active_workers: Dict[str, Dict[str, Any]] = {}
active_processes: Dict[str, asyncio.subprocess.Process] = {}

async def transcription_worker(event_slug: str, language_code: str, booth_id: str, broadcast_callback, provider_name: str, model_size: str, config: ProviderConfig):
    logger.info(f"Starting {provider_name} transcription worker for booth {booth_id}")
    rtsp_url = f"rtsp://mediamtx:8554/{event_slug}/{language_code}"
    
    provider = PROVIDERS.get(provider_name, PROVIDERS['local'])
    
    sample_rate = "24000" if provider_name == "openai" else "16000"
    
    ffmpeg_cmd = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vn", "-acodec", "pcm_s16le",
        "-ar", sample_rate,
        "-ac", "1",
        "-f", "s16le", "-"
    ]
    
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    async def log_stderr(stderr_stream, bid):
        try:
            while True:
                line = await stderr_stream.readline()
                if not line:
                    break
                logger.debug(f"[{bid}] ffmpeg: {line.decode().strip()}")
        except Exception:
            pass
            
    stderr_task = asyncio.create_task(log_stderr(process.stderr, booth_id))
    
    async with active_workers_lock:
        active_processes[booth_id] = process
        if booth_id in active_workers:
            active_workers[booth_id]["stderr_task"] = stderr_task
    
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
        async with active_workers_lock:
            proc_to_kill = active_processes.pop(booth_id, None)
            worker_data = active_workers.pop(booth_id, None)
            
        if worker_data:
            if worker_data.get("provider") == 'local':
                from portal.transcription.providers.local import decrement_model_ref
                decrement_model_ref(worker_data.get("model_size"))
                
            if "stderr_task" in worker_data and worker_data["stderr_task"]:
                worker_data["stderr_task"].cancel()
            
        if proc_to_kill and proc_to_kill.returncode is None:
            try:
                proc_to_kill.terminate()
                await proc_to_kill.wait()
            except ProcessLookupError:
                pass
        logger.info(f"[{booth_id}] Transcription worker exited.")

async def start_transcription_worker(event_slug: str, language_code: str, booth_id: str, broadcast_callback, provider: str, model_size: str, config: ProviderConfig):
    async with active_workers_lock:
        if booth_id in active_workers:
            logger.info(f"Transcription worker for {booth_id} is already running.")
            return

        if len(active_workers) >= MAX_TOTAL_WORKERS:
            raise ValueError(f"System at maximum capacity ({MAX_TOTAL_WORKERS} concurrent transcription booths).")

        if provider == 'local':
            from portal.transcription.providers.local import increment_model_ref, start_eviction_loop
            increment_model_ref(model_size)
            start_eviction_loop()

        task = asyncio.create_task(transcription_worker(event_slug, language_code, booth_id, broadcast_callback, provider, model_size, config))
        active_workers[booth_id] = {
            "task": task,
            "provider": provider,
            "model_size": model_size,
            "stderr_task": None
        }

async def stop_transcription_worker(booth_id: str):
    async with active_workers_lock:
        worker_data = active_workers.pop(booth_id, None)
        process = active_processes.pop(booth_id, None)
        
    if process and process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
            
    if worker_data:
        if worker_data.get("provider") == 'local':
            from portal.transcription.providers.local import decrement_model_ref
            decrement_model_ref(worker_data.get("model_size"))
            
        if "stderr_task" in worker_data and worker_data["stderr_task"]:
            worker_data["stderr_task"].cancel()
        task = worker_data["task"]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Task finished with exception: {e}")
