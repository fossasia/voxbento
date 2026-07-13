from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from portal.tts.constants import (
    SUPERTONIC_DEFAULT_VOICE,
    SUPERTONIC_PRESET_VOICES,
    SUPERTONIC_SUPPORTED_LANGS,
    SUPERTONIC_VOICE_BY_LANG,
)
from portal.tts.providers.base import AudioCallback, TTSProvider

logger = logging.getLogger(__name__)

# Sentence boundary regex — split LLM output into sentences for synthesis.
SENTENCE_BOUNDARY = re.compile(r"([^.?!]+[.?!]+)")

# Supertonic emits 44.1kHz float32 audio; listeners expect 24kHz PCM s16le.
SUPERTONIC_SAMPLE_RATE = 44100
TARGET_SAMPLE_RATE = 24000


class SupertonicTTSProvider(TTSProvider):
    """In-process Supertonic (ONNX) TTS provider.

    A single ``TTS`` engine is shared across all rooms/languages. ONNX Runtime
    sessions are not thread-safe, so synthesis is serialised with an inference
    lock and offloaded to a worker thread via ``asyncio.to_thread`` to keep the
    event loop responsive.
    """

    _tts: Any | None = None
    _init_lock = threading.Lock()  # guards engine construction (double-checked)
    _inference_lock = threading.Lock()  # ONNX session is not thread-safe
    _voice_style_cache: dict[str, Any] = {}

    @classmethod
    def _get_engine(cls) -> Any:
        if cls._tts is None:
            with cls._init_lock:
                if cls._tts is None:
                    from supertonic import TTS

                    from portal.config import settings

                    threads = settings.supertonic_intra_op_threads or None
                    logger.info("[TTS] Initialising Supertonic engine (first use; may download model)...")
                    cls._tts = TTS(auto_download=True, intra_op_num_threads=threads)
                    logger.info("[TTS] Supertonic engine ready.")
        return cls._tts

    def _resolve_voice(self, voice: str, language_code: str) -> str:
        if voice in SUPERTONIC_PRESET_VOICES:
            return voice
        primary_lang = language_code.split("-")[0].lower()
        return SUPERTONIC_VOICE_BY_LANG.get(primary_lang, SUPERTONIC_DEFAULT_VOICE)

    async def synthesize_stream(
        self,
        *,
        text_chunks: AsyncIterator[str],
        language_code: str,
        voice: str,
        on_audio: AudioCallback,
    ) -> None:
        resolved_voice = self._resolve_voice(voice, language_code)

        async def synth_and_broadcast(sentence: str) -> None:
            if not sentence:
                return
            try:
                audio = await asyncio.to_thread(self._synthesize_sync, sentence, language_code, resolved_voice)
                if audio:
                    await on_audio(audio)
            except Exception:
                logger.exception("[TTS] Supertonic synthesis error for lang %s", language_code)

        buffer = ""
        try:
            async for chunk in text_chunks:
                buffer += chunk
                while True:
                    match = SENTENCE_BOUNDARY.search(buffer)
                    if not match:
                        break
                    sentence = match.group(1).strip()
                    buffer = buffer[match.end() :].lstrip()
                    await synth_and_broadcast(sentence)

            remaining = buffer.strip()
            if remaining:
                await synth_and_broadcast(remaining)
        except Exception:
            logger.exception("[TTS] Error in Supertonic TTS stream for lang %s", language_code)

    def _synthesize_sync(self, text: str, language_code: str, voice: str) -> bytes:
        engine = self._get_engine()

        primary_lang = language_code.split("-")[0].lower()
        lang_tag = primary_lang if primary_lang in SUPERTONIC_SUPPORTED_LANGS else "na"

        from portal.config import settings

        # ONNX session and the shared style cache are not thread-safe; serialise
        # cache population and inference under one lock.
        with self._inference_lock:
            style = self._voice_style_cache.get(voice)
            if style is None:
                style = engine.get_voice_style(voice_name=voice)
                self._voice_style_cache[voice] = style

            wav, _ = engine.synthesize(
                text=text,
                lang=lang_tag,
                voice_style=style,
                total_steps=settings.supertonic_total_steps,
                speed=1.0,
            )

        # wav is float32 numpy at 44100 Hz, shape (1, N) — convert to 24kHz PCM.
        return self._resample_to_pcm(np.asarray(wav).squeeze(), SUPERTONIC_SAMPLE_RATE, TARGET_SAMPLE_RATE)

    @staticmethod
    def _resample_to_pcm(samples: np.ndarray, src_rate: int, dst_rate: int) -> bytes:
        """Resample float32 [-1, 1] audio and convert to 16-bit PCM bytes."""
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if samples.size == 0:
            return b""
        if src_rate != dst_rate:
            new_len = max(1, int(round(samples.size * dst_rate / src_rate)))
            indices = np.linspace(0, samples.size - 1, new_len)
            samples = np.interp(indices, np.arange(samples.size), samples)
        pcm = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
        return pcm.tobytes()
