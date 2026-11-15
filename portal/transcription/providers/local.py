from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faster_whisper import WhisperModel

import numpy as np

from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig, TranscriptionProvider

logger = logging.getLogger(__name__)

#: 16 kHz, 16-bit mono PCM.
BYTES_PER_SECOND = 32000
#: Audio from the end of one chunk heard again at the start of the next.
OVERLAP_BYTES = BYTES_PER_SECOND


@dataclass
class ModelEntry:
    model: WhisperModel
    last_used: float


_loaded_models = {}
_active_booths_per_model = {}
_model_lock = threading.Lock()
_load_lock = threading.Lock()


def increment_model_ref(model_size: str):
    with _model_lock:
        _active_booths_per_model[model_size] = _active_booths_per_model.get(model_size, 0) + 1


def decrement_model_ref(model_size: str):
    with _model_lock:
        if model_size in _active_booths_per_model:
            _active_booths_per_model[model_size] = max(0, _active_booths_per_model[model_size] - 1)


def is_model_loaded(model_size: str) -> bool:
    """Whether ``model_size`` is in memory now (idle models are evicted after an hour)."""
    with _model_lock:
        return model_size in _loaded_models


def get_model(model_size: str):
    with _model_lock:
        if model_size in _loaded_models:
            _loaded_models[model_size].last_used = time.time()
            return _loaded_models[model_size].model

    with _load_lock:
        # Double-check inside the load lock
        with _model_lock:
            if model_size in _loaded_models:
                _loaded_models[model_size].last_used = time.time()
                return _loaded_models[model_size].model

        logger.info(f"Loading faster-whisper model: {model_size}")
        from faster_whisper import WhisperModel

        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        with _model_lock:
            _loaded_models[model_size] = ModelEntry(model=model, last_used=time.time())
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
        *,
        prompt: str | None = None,
    ) -> str:
        """Transcribe one chunk. ``prompt`` is text Whisper reads as preceding speech, e.g.
        glossary terms so it spells them as written; None (the default) gives no prompt."""
        if booth_state:
            # Prepend the overlap (up to the last 1.0 s heard) to the current chunk, so a word cut
            # at the previous chunk's end is heard whole; words inside it are dropped below.
            overlap_seconds = len(booth_state.overlap_buffer) / BYTES_PER_SECOND
            overlap_audio = booth_state.overlap_buffer + chunk
            # Keep the last 1.0 s of everything heard, not only of this chunk: a chunk shorter
            # than a second must not shorten the next overlap.
            booth_state.overlap_buffer = overlap_audio[-OVERLAP_BYTES:]
        else:
            overlap_seconds = 0.0
            overlap_audio = chunk

        audio_data = np.frombuffer(overlap_audio, np.int16).astype(np.float32) / 32768.0
        return await asyncio.to_thread(
            self._run_inference, audio_data, language_code, model_variant, booth_state, prompt, overlap_seconds
        )

    def _run_inference(
        self,
        audio_data: np.ndarray,
        language_code: str,
        model_size: str,
        booth_state: BoothTranscriptionState | None,
        prompt: str | None = None,
        overlap_seconds: float = 1.0,
    ) -> str:
        model = get_model(model_size)
        segments, _ = model.transcribe(
            audio_data,
            beam_size=5,
            vad_filter=True,
            language=language_code if language_code else None,
            word_timestamps=True,
            compression_ratio_threshold=2.4,
            no_speech_threshold=0.6,
            log_prob_threshold=-1.0,
            condition_on_previous_text=False,
            initial_prompt=prompt,
        )

        valid_words = []
        for segment in segments:
            if not getattr(segment, "words", None):
                # Fallback if words aren't available
                valid_words.append(segment.text.strip())
                continue

            for word in segment.words:
                # Skip words inside the overlap, already captioned from the previous chunk. Its real
                # length is used: none for the first chunk, less than 1.0 s after a short one.
                if word.end > overlap_seconds:
                    valid_words.append(word.word.strip())

        return " ".join(valid_words).strip()
