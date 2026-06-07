import asyncio
import logging
import numpy as np
from faster_whisper import WhisperModel
import io

from portal.config import settings

logger = logging.getLogger(__name__)

import threading

# Lazy load _current_model_size: str | None = None
_current_model_size = None
_current_model = None
_model_lock = threading.Lock()

def get_model(model_size: str):
    global _current_model_size, _current_model
    
    # Fast path: if it's already the right model, return it immediately without locking
    if _current_model_size == model_size and _current_model is not None:
        return _current_model
        
    with _model_lock:
        # Double-check inside the lock in case another thread just finished loading it
        if _current_model_size != model_size:
            logger.info(f"Loading faster-whisper model: {model_size} (this may take a moment)")
            if _current_model is not None:
                del _current_model
                import gc
                gc.collect()
            _current_model = WhisperModel(model_size, device="cpu", compute_type="int8")
            _current_model_size = model_size
            logger.info(f"Model {model_size} loaded successfully")
            
    return _current_model

# Track active tasks so we can cancel them
active_workers: dict[str, asyncio.Task] = {}
# Track ffmpeg processes so we can terminate them
active_processes: dict[str, asyncio.subprocess.Process] = {}

async def transcription_worker(event_slug: str, language_code: str, booth_id: str, broadcast_callback, model_size: str):
    """
    Connects to the RTSP stream using ffmpeg, decodes audio, and runs faster-whisper.
    """
    logger.info(f"Starting transcription worker for booth {booth_id}")
    rtsp_url = f"rtsp://mediamtx:8554/{event_slug}/{language_code}"
    
    # 16000 Hz, mono, 16-bit PCM
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
        chunk_size_bytes = 16000 * 2 * 3 # 3 seconds of audio (16kHz * 2 bytes/sample)
        
        while True:
            # Read exactly chunk_size_bytes
            chunk = await process.stdout.readexactly(chunk_size_bytes)
            if not chunk:
                break
                
            # Convert raw PCM to float32 numpy array
            audio_data = np.frombuffer(chunk, np.int16).astype(np.float32) / 32768.0
            
            # Run inference in a threadpool to not block the async event loop
            text = await asyncio.to_thread(_run_inference, audio_data, language_code, model_size)
            
            if text:
                logger.debug(f"[{booth_id}] Transcribed: {text}")
                await broadcast_callback(booth_id, text)
                
    except asyncio.IncompleteReadError:
        logger.warning(f"[{booth_id}] ffmpeg stream ended abruptly.")
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

def _run_inference(audio_data: np.ndarray, language_code: str, model_size: str) -> str:
    model = get_model(model_size)
    # Provide the language hint
    segments, info = model.transcribe(audio_data, beam_size=5, vad_filter=True, language=language_code)
    
    text = ""
    for segment in segments:
        text += segment.text + " "
    
    return text.strip()

async def start_transcription_worker(event_slug: str, language_code: str, booth_id: str, broadcast_callback, model_size: str):
    """Start the background task if not already running."""
    if booth_id in active_workers:
        logger.warning(f"Transcription worker for {booth_id} is already running.")
        return
        
    task = asyncio.create_task(transcription_worker(event_slug, language_code, booth_id, broadcast_callback, model_size))
    active_workers[booth_id] = task

async def stop_transcription_worker(booth_id: str):
    """Stop the background task for the booth."""
    task = active_workers.get(booth_id)
    if task:
        task.cancel()
        try:
            await task
        except Exception as e:
            logger.debug(f"Task finished with exception: {e}")
    
    # Also kill the ffmpeg process immediately if still lingering
    process = active_processes.get(booth_id)
    if process and process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
