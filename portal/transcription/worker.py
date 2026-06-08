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
# Store dict containing {"task": asyncio.Task, "provider": str}
active_workers: Dict[str, Dict[str, Any]] = {}
active_processes: Dict[str, asyncio.subprocess.Process] = {}

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
    active_workers[booth_id] = {
        "task": task,
        "provider": provider
    }

async def stop_transcription_worker(booth_id: str):
    worker_data = active_workers.get(booth_id)
    if worker_data:
        task = worker_data["task"]
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
