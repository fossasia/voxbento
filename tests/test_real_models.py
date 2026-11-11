"""Real inference through the three local models: Supertonic (TTS), faster-whisper (STT), NLLB (MT).

VOXBENTO_REAL_MODELS=1 uv run --extra supertonic pytest tests/test_real_models.py -v

Nothing is mocked: the providers download and load the real weights (about 1.2 GB on first
run, cached under HF_HOME and SUPERTONIC_CACHE_DIR afterwards) and run on the CPU. The
models check each other, so no audio fixtures are stored: Supertonic speaks a known
sentence and Whisper has to hear it back. Model output is not bit-stable across library
versions, so every assertion is a keyword or word-overlap threshold, never an exact string.

Opt-in locally; the ``real-models`` workflow sets VOXBENTO_REAL_MODELS so a model that
fails to load fails the job instead of skipping it. REAL_MODELS_WHISPER_SIZES (comma
separated, default ``tiny``) picks the Whisper sizes to run.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import AsyncIterator

import numpy as np
import pytest

from portal.transcription.providers.base import BoothTranscriptionState, ProviderConfig
from portal.transcription.providers.local import LocalProvider as WhisperProvider
from portal.translations.constants import TRANSLATION_MODELS, TranslationProviderEnum
from portal.translations.providers.local import LocalProvider as NllbProvider
from portal.translations.providers.local import get_download_progress
from portal.translations.vocabulary import VocabularyEntryInput
from portal.tts.providers.base import TTSProviderEnum, get_tts_provider

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not os.environ.get("VOXBENTO_REAL_MODELS"),
        reason="Real model inference is opt-in locally (VOXBENTO_REAL_MODELS=1); required in CI",
    ),
]

WHISPER_SIZES = [s.strip() for s in os.environ.get("REAL_MODELS_WHISPER_SIZES", "tiny").split(",") if s.strip()]
NLLB_MODEL = TRANSLATION_MODELS[TranslationProviderEnum.LOCAL.value][0]

TTS_RATE = 24000  # what every TTS provider must emit
STT_RATE = 16000  # what ffmpeg hands the transcription providers
STT_CHUNK_BYTES = STT_RATE * 2 * 3  # the 3 s chunks TranscriptionProvider.run_stream reads

ENGLISH = "The conference starts at nine o'clock tomorrow morning. Please bring your laptop and your badge."
ENGLISH_KEYWORDS = ("conference", "starts", "tomorrow", "morning", "laptop", "badge")

_speech_cache: dict[tuple[str, str], bytes] = {}


def _normalize(text: str) -> str:
    """Lower-case, accent-free text so 'Mañana' and 'manana' compare equal."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _hits(text: str, keywords: tuple[str, ...]) -> list[str]:
    haystack = _normalize(text)
    return [k for k in keywords if _normalize(k) in haystack]


def _content_words(text: str) -> set[str]:
    words = ("".join(c for c in w if c.isalnum()) for w in _normalize(text).split())
    return {w for w in words if len(w) > 3}


async def _speak(text: str, language_code: str) -> bytes:
    """Run text through the real Supertonic provider, fed word by word like an LLM token stream."""
    key = (text, language_code)
    if key not in _speech_cache:
        chunks: list[bytes] = []

        async def on_audio(pcm: bytes) -> None:
            chunks.append(pcm)

        async def tokens() -> AsyncIterator[str]:
            for word in text.split(" "):
                yield word + " "

        provider = get_tts_provider(TTSProviderEnum.SUPERTONIC.value)
        await provider.synthesize_stream(text_chunks=tokens(), language_code=language_code, voice="", on_audio=on_audio)
        _speech_cache[key] = b"".join(chunks)
    return _speech_cache[key]


def _to_stt_input(pcm_24k: bytes) -> bytes:
    """24 kHz TTS output -> 16 kHz PCM with one second of leading silence.

    The local provider drops words that end inside the first second of each window (the
    overlap it carries between chunks), so speech must not start at sample zero.
    """
    samples = np.frombuffer(pcm_24k, np.int16).astype(np.float32)
    length = int(round(samples.size * STT_RATE / TTS_RATE))
    resampled = np.interp(np.linspace(0, samples.size - 1, length), np.arange(samples.size), samples)
    return b"\x00" * (STT_RATE * 2) + resampled.astype(np.int16).tobytes()


async def _transcribe(pcm_16k: bytes, language_code: str, model: str, *, chunked: bool) -> str:
    provider = WhisperProvider()
    config = ProviderConfig(api_key=None)
    if not chunked:
        return await provider.process_chunk(pcm_16k, language_code, model, config)
    # Same shape as TranscriptionProvider.run_stream: 3 s chunks sharing one booth state.
    state = BoothTranscriptionState(booth_id="real-models-en")
    parts = [
        await provider.process_chunk(pcm_16k[i : i + STT_CHUNK_BYTES], language_code, model, config, booth_state=state)
        for i in range(0, len(pcm_16k), STT_CHUNK_BYTES)
    ]
    return " ".join(p for p in parts if p)


async def _translate(text: str, target_name: str, target_code: str, **kwargs) -> str | None:
    return await NllbProvider().translate(
        provider_name=TranslationProviderEnum.LOCAL.value,
        text=text,
        target_lang_name=target_name,
        target_lang_code=target_code,
        source_lang_name="English",
        model=NLLB_MODEL,
        api_key=None,
        **kwargs,
    )


async def test_supertonic_emits_audible_24khz_pcm():
    pcm = await _speak(ENGLISH, "en")

    assert len(pcm) % 2 == 0
    samples = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    seconds = samples.size / TTS_RATE
    # Two short sentences: a wrong sample rate or a truncated synthesis lands outside this.
    assert 3.0 < seconds < 15.0, f"unexpected duration {seconds:.1f}s"
    rms = float(np.sqrt(np.mean(samples**2)))
    assert rms > 0.01, f"audio is near-silent (rms {rms:.4f})"
    assert float(np.max(np.abs(samples))) > 0.1


@pytest.mark.parametrize("model", WHISPER_SIZES)
async def test_whisper_hears_supertonic_speech_through_the_chunked_path(model):
    pcm = _to_stt_input(await _speak(ENGLISH, "en"))

    text = await _transcribe(pcm, "en", model, chunked=True)

    found = _hits(text, ENGLISH_KEYWORDS)
    assert len(found) >= 5, f"whisper-{model} heard {text!r}; matched only {found}"


@pytest.mark.parametrize(
    ("target_name", "target_code", "keywords"),
    [
        ("French", "fr", ("conférence", "demain")),
        ("Spanish", "es", ("conferencia", "mañana")),
        ("German", "de", ("konferenz", "morgen")),
    ],
)
async def test_nllb_translates_english(target_name, target_code, keywords):
    source = "The conference starts tomorrow morning."

    translated = await _translate(source, target_name, target_code)

    assert translated, "NLLB returned nothing"
    assert _normalize(translated) != _normalize(source)
    assert _hits(translated, keywords) == list(keywords), f"{target_code}: {translated!r}"


async def test_nllb_accepts_iso_codes_as_language_names():
    # The translation worker can pass ISO 639-1 codes where language names are expected.
    translated = await NllbProvider().translate(
        provider_name=TranslationProviderEnum.LOCAL.value,
        text="The conference starts tomorrow morning.",
        target_lang_name="es",
        target_lang_code="es",
        source_lang_name="en",
        model=NLLB_MODEL,
        api_key=None,
    )

    assert translated and _hits(translated, ("conferencia", "mañana")), translated


async def test_nllb_keeps_glossary_terms():
    glossary = [VocabularyEntryInput(source_term="keynote", target_language="fr", target_term="discours inaugural")]

    translated = await _translate(
        "The keynote is about Eventyay and open source.", "French", "fr", vocabulary_entries=glossary
    )

    assert translated and "discours inaugural" in translated.lower(), translated
    assert "Eventyay" in translated


async def test_nllb_reports_the_downloaded_model_as_cached():
    await _translate("Hello.", "French", "fr")

    assert get_download_progress(NLLB_MODEL).get("status") == "completed"


async def test_speech_to_translated_speech_chain():
    """Floor speech -> Whisper -> NLLB -> Supertonic in Spanish, checked by Whisper again."""
    model = WHISPER_SIZES[0]
    sentence = "Please bring your laptop and your badge."

    heard = await _transcribe(_to_stt_input(await _speak(sentence, "en")), "en", model, chunked=False)
    assert len(_hits(heard, ("bring", "laptop", "badge"))) >= 2, f"whisper-{model} heard {heard!r}"

    spanish = await _translate(heard, "Spanish", "es")
    assert spanish and _hits(spanish, ("favor",)), f"NLLB produced {spanish!r} from {heard!r}"

    heard_spanish = await _transcribe(_to_stt_input(await _speak(spanish, "es")), "es", model, chunked=False)

    expected = _content_words(spanish)
    overlap = len(expected & _content_words(heard_spanish)) / len(expected)
    assert overlap >= 0.5, f"spoke {spanish!r}, whisper-{model} heard {heard_spanish!r} (overlap {overlap:.0%})"
