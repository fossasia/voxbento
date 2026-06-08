import asyncio
import logging
import threading
import numpy as np

from portal.transcription.providers.base import TranscriptionProvider, ProviderConfig

logger = logging.getLogger(__name__)

_current_model_size = None
_current_model = None
_model_lock = threading.Lock()

def get_model(model_size: str):
    global _current_model_size, _current_model
    with _model_lock:
        if _current_model_size != model_size:
            logger.info(f"Loading faster-whisper model: {model_size}")
            if _current_model is not None:
                del _current_model
                import gc
                gc.collect()
            from faster_whisper import WhisperModel
            _current_model = WhisperModel(model_size, device="cpu", compute_type="int8")
            _current_model_size = model_size
        return _current_model

class LocalProvider(TranscriptionProvider):
    async def process_chunk(self, chunk: bytes, language_code: str, model_variant: str, config: ProviderConfig) -> str:
        audio_data = np.frombuffer(chunk, np.int16).astype(np.float32) / 32768.0
        return await asyncio.to_thread(self._run_inference, audio_data, language_code, model_variant)
        
    def _run_inference(self, audio_data: np.ndarray, language_code: str, model_size: str) -> str:
        model = get_model(model_size)
        segments, _ = model.transcribe(audio_data, beam_size=5, vad_filter=True, language=language_code)
        text = " ".join(segment.text for segment in segments)
        return text.strip()
