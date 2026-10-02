"""Ray Serve deployment for Whisper audio transcription."""

import base64
import logging

import numpy as np
from ray import serve
from starlette.requests import Request

logger = logging.getLogger(__name__)


@serve.multiplexed(max_num_models_per_replica=2)
async def get_whisper_model(model_size: str):
    import ray
    from faster_whisper import WhisperModel

    logger.info(f"Loading faster-whisper model dynamically: {model_size}")
    has_gpu = len(ray.get_gpu_ids()) > 0
    device = "cuda" if has_gpu else "cpu"
    compute_type = "float16" if has_gpu else "int8"

    return WhisperModel(model_size, device=device, compute_type=compute_type)


@serve.deployment
class FasterWhisperTranscriber:
    """Ray Serve deployment class for the Faster Whisper transcription model."""

    def __init__(self):
        """
        Initialize the Whisper deployment. Models are loaded dynamically via multiplexing.
        """
        pass

    def transcribe_with_model(self, model, audio_data: np.ndarray, language_code: str) -> str:
        """
        Run the audio data through the transcription model.

        Args:
            model: The dynamically loaded WhisperModel.
            audio_data: Numpy array of the float32 audio samples.
            language_code: Optional ISO language code to force the model into.

        Returns:
            The transcribed text string.
        """
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

    async def __call__(self, request: Request):
        """
        Handle incoming HTTP requests to the deployment.

        Args:
            request: The Starlette HTTP request containing the JSON payload.

        Returns:
            A dictionary containing the transcribed text or an error.
        """
        try:
            payload = await request.json()
        except ValueError:
            return {"error": "Invalid JSON payload."}

        if not isinstance(payload, dict):
            return {"error": "Expected JSON dictionary payload."}

        audio_b64 = payload.get("audio_b64")
        if not audio_b64:
            return {"error": "Missing audio_b64 in payload."}

        # Convert back from base64 to bytes, then to numpy float32
        audio_bytes = base64.b64decode(audio_b64)
        audio_data = np.frombuffer(audio_bytes, dtype=np.float32)

        language_code = payload.get("language_code", "")

        model_size = serve.get_multiplexed_model_id() or "tiny"
        model = await get_whisper_model(model_size)

        import asyncio

        loop = asyncio.get_running_loop()
        # Run inference in background thread so we don't block the Ray event loop
        transcribed_text = await loop.run_in_executor(
            None, lambda: self.transcribe_with_model(model, audio_data, language_code)
        )

        return {"transcribed_text": transcribed_text}


transcriber_app = FasterWhisperTranscriber.bind()
