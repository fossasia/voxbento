import asyncio
import gc
import logging
import threading
import time
from dataclasses import dataclass

import numpy as np

from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig, TranscriptionProvider

logger = logging.getLogger(__name__)


@dataclass
class ModelEntry:
    model: any
    last_used: float


_loaded_models = {}
_active_booths_per_model = {}
_model_lock = threading.Lock()


def increment_model_ref(model_size: str):
    with _model_lock:
        _active_booths_per_model[model_size] = _active_booths_per_model.get(model_size, 0) + 1


def decrement_model_ref(model_size: str):
    with _model_lock:
        if model_size in _active_booths_per_model:
            _active_booths_per_model[model_size] = max(0, _active_booths_per_model[model_size] - 1)


def get_model(model_size: str):
    with _model_lock:
        if model_size not in _loaded_models:
            logger.info(f"Loading faster-whisper model: {model_size}")
            from faster_whisper import WhisperModel

            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            _loaded_models[model_size] = ModelEntry(model=model, last_used=time.time())
        else:
            _loaded_models[model_size].last_used = time.time()
        return _loaded_models[model_size].model


async def eviction_loop():
    while True:
        await asyncio.sleep(60 * 15)  # Check every 15 minutes
        now = time.time()
        to_delete = []
        with _model_lock:
            for size, entry in _loaded_models.items():
                refs = _active_booths_per_model.get(size, 0)
                if refs == 0 and (now - entry.last_used) > 3600:
                    to_delete.append(size)
            for size in to_delete:
                logger.info(f"Evicting idle model: {size}")
                del _loaded_models[size]
        if to_delete:
            gc.collect()


_eviction_task = None


def start_eviction_loop():
    global _eviction_task
    if _eviction_task is None:
        try:
            loop = asyncio.get_running_loop()
            _eviction_task = loop.create_task(eviction_loop())
        except RuntimeError:
            pass


class LocalProvider(TranscriptionProvider):
    async def process_chunk(
        self,
        chunk: bytes,
        language_code: str,
        model_variant: str,
        config: ProviderConfig,
        booth_state: BoothTranscriptionState | None = None,
    ) -> str:
        if booth_state:
            # Append overlap buffer (last 1.0s) to current 3.0s chunk -> 4.0s total
            overlap_audio = booth_state.overlap_buffer + chunk

            # Save the last 1.0s of the *current* chunk for the next iteration
            # 16000 hz * 2 bytes/sample * 1 channel * 1.0 seconds = 32000 bytes
            overlap_bytes = 32000
            if len(chunk) >= overlap_bytes:
                booth_state.overlap_buffer = chunk[-overlap_bytes:]
            else:
                booth_state.overlap_buffer = chunk
        else:
            overlap_audio = chunk

        audio_data = np.frombuffer(overlap_audio, np.int16).astype(np.float32) / 32768.0
        return await asyncio.to_thread(self._run_inference, audio_data, language_code, model_variant, booth_state)

    def _run_inference(
        self, audio_data: np.ndarray, language_code: str, model_size: str, booth_state: BoothTranscriptionState | None
    ) -> str:
        model = get_model(model_size)
        segments, _ = model.transcribe(
            audio_data, beam_size=5, vad_filter=True, language=language_code, word_timestamps=True
        )

        valid_words = []
        for segment in segments:
            if not getattr(segment, "words", None):
                # Fallback if words aren't available
                valid_words.append(segment.text.strip())
                continue

            for word in segment.words:
                if word.end > 1.0:  # Skip words completely inside the 1.0s overlap period
                    valid_words.append(word.word.strip())

        return " ".join(valid_words).strip()
