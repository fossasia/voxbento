from __future__ import annotations

import numpy as np
import pytest

from portal.tts.providers.base import TTSProviderEnum, get_tts_provider
from portal.tts.providers.deepgram import DeepgramTTSProvider
from portal.tts.providers.supertonic import SupertonicTTSProvider


async def _achunks(*chunks: str):
    for c in chunks:
        yield c


class TestProviderFactory:
    def test_returns_deepgram_provider(self):
        provider = get_tts_provider("deepgram", deepgram_api_key="secret")
        assert isinstance(provider, DeepgramTTSProvider)
        assert provider.api_key == "secret"

    def test_returns_supertonic_provider(self):
        provider = get_tts_provider("supertonic")
        assert isinstance(provider, SupertonicTTSProvider)

    def test_unknown_provider_falls_back_to_deepgram(self):
        provider = get_tts_provider("does-not-exist")
        assert isinstance(provider, DeepgramTTSProvider)

    def test_enum_values(self):
        assert TTSProviderEnum.DEEPGRAM.value == "deepgram"
        assert TTSProviderEnum.SUPERTONIC.value == "supertonic"


class TestSupertonicResample:
    def test_downsamples_to_expected_length(self):
        samples = np.sin(np.linspace(0, 50, 44100)).astype(np.float32)
        pcm = SupertonicTTSProvider._resample_to_pcm(samples, 44100, 24000)
        assert isinstance(pcm, bytes)
        # 16-bit samples → 2 bytes each, length ≈ 24000.
        assert abs(len(pcm) // 2 - 24000) <= 2

    def test_empty_input_returns_empty_bytes(self):
        assert SupertonicTTSProvider._resample_to_pcm(np.array([], dtype=np.float32), 44100, 24000) == b""

    def test_no_resample_when_rates_equal(self):
        samples = np.zeros(100, dtype=np.float32)
        pcm = SupertonicTTSProvider._resample_to_pcm(samples, 24000, 24000)
        assert len(pcm) // 2 == 100

    def test_clipping_bounds(self):
        samples = np.array([2.0, -2.0, 0.0], dtype=np.float32)
        pcm = SupertonicTTSProvider._resample_to_pcm(samples, 24000, 24000)
        values = np.frombuffer(pcm, dtype=np.int16)
        assert values[0] == 32767
        assert values[1] == -32768
        assert values[2] == 0


class TestSupertonicVoiceResolution:
    def test_explicit_preset_voice_is_kept(self):
        provider = SupertonicTTSProvider()
        assert provider._resolve_voice("F3", "en") == "F3"

    def test_empty_voice_uses_language_map(self):
        provider = SupertonicTTSProvider()
        assert provider._resolve_voice("", "fr") == "F1"
        assert provider._resolve_voice("", "de") == "M2"

    def test_unknown_voice_and_language_uses_default(self):
        provider = SupertonicTTSProvider()
        assert provider._resolve_voice("bogus", "zz") == "M1"


@pytest.mark.anyio
class TestSupertonicStreaming:
    async def test_synthesize_stream_buffers_sentences(self, monkeypatch):
        provider = SupertonicTTSProvider()
        calls: list[tuple[str, str, str]] = []

        def fake_sync(text, language_code, voice):
            calls.append((text, language_code, voice))
            return b"\x00\x01"

        monkeypatch.setattr(provider, "_synthesize_sync", fake_sync)

        emitted: list[bytes] = []

        async def on_audio(chunk: bytes):
            emitted.append(chunk)

        await provider.synthesize_stream(
            text_chunks=_achunks("Hello world. ", "How are ", "you?"),
            language_code="en",
            voice="M1",
            on_audio=on_audio,
        )

        assert [c[0] for c in calls] == ["Hello world.", "How are you?"]
        assert all(c[2] == "M1" for c in calls)
        assert emitted == [b"\x00\x01", b"\x00\x01"]

    async def test_synthesize_stream_swallows_synthesis_errors(self, monkeypatch):
        provider = SupertonicTTSProvider()

        def boom(text, language_code, voice):
            raise RuntimeError("onnx exploded")

        monkeypatch.setattr(provider, "_synthesize_sync", boom)

        emitted: list[bytes] = []

        async def on_audio(chunk: bytes):
            emitted.append(chunk)

        # Should not raise — errors are logged and skipped.
        await provider.synthesize_stream(
            text_chunks=_achunks("One sentence."),
            language_code="en",
            voice="M1",
            on_audio=on_audio,
        )


class _RecordingProvider:
    def __init__(self):
        self.languages: list[str] = []

    async def synthesize_stream(self, *, text_chunks, language_code, voice, on_audio):
        self.languages.append(language_code)
        await on_audio(b"pcm")


@pytest.mark.anyio
async def test_only_ai_voice_languages_are_spoken(monkeypatch):
    from portal.tts import worker

    provider = _RecordingProvider()

    async def config(room_id):
        return {"tts_provider": provider, "voice": "", "voice_languages": {"fr"}}

    monkeypatch.setattr(worker, "_load_config_cached", config)
    assert await worker.synthesize(1, "Bonjour", "fr") == b"pcm"
    # German is set to AI text: translated for reading, never spoken.
    assert await worker.synthesize(1, "Hallo", "de") is None
    assert provider.languages == ["fr"]


# ── ElevenLabs ───────────────────────────────────────────────────────────

import json  # noqa: E402

import httpx  # noqa: E402

from portal.tts.providers import elevenlabs  # noqa: E402
from portal.tts.providers.elevenlabs import ElevenLabsTTSProvider  # noqa: E402


async def _byte_chunks(*chunks: bytes):
    for chunk in chunks:
        yield chunk


@pytest.fixture
def elevenlabs_api(monkeypatch):
    """A fake ElevenLabs API: answers each model with the status and audio chunks given for it."""
    monkeypatch.setattr(elevenlabs, "_refused_models", set())
    requests: list[httpx.Request] = []
    answers: dict[str, tuple[int, tuple[bytes, ...]]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        status, chunks = answers[json.loads(request.content)["model_id"]]
        if status != 200:
            return httpx.Response(status, json={"detail": "nope"})
        return httpx.Response(200, content=_byte_chunks(*chunks))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(elevenlabs, "get_http_client", lambda: client)
    return requests, answers


async def _speak(text: str, language_code: str = "th") -> list[bytes]:
    emitted: list[bytes] = []

    async def on_audio(chunk: bytes):
        emitted.append(chunk)

    await ElevenLabsTTSProvider(api_key="xi-secret").synthesize_stream(
        text_chunks=_achunks(*text.split("|")), language_code=language_code, voice="M1", on_audio=on_audio
    )
    return emitted


def test_factory_returns_elevenlabs_provider():
    provider = get_tts_provider("elevenlabs", elevenlabs_api_key="xi-secret")
    assert isinstance(provider, ElevenLabsTTSProvider)
    assert provider.api_key == "xi-secret"
    assert TTSProviderEnum.ELEVENLABS.value == "elevenlabs"


@pytest.mark.anyio
async def test_elevenlabs_speaks_the_whole_phrase_in_one_request(elevenlabs_api):
    requests, answers = elevenlabs_api
    answers["eleven_v4_turbo"] = (200, (b"\x01\x02\x03", b"\x04\x05\x06"))

    emitted = await _speak("สวัสดีครับ |ยินดีต้อนรับ", language_code="th-TH")

    # Whole 16-bit samples only: the odd byte waits for the next chunk.
    assert emitted == [b"\x01\x02", b"\x03\x04\x05\x06"]
    (request,) = requests
    assert request.url.params["output_format"] == "pcm_24000"
    assert request.headers["xi-api-key"] == "xi-secret"
    body = json.loads(request.content)
    assert body["inputs"] == [{"text": "สวัสดีครับ ยินดีต้อนรับ", "voice_id": elevenlabs.ELEVENLABS_DEFAULT_VOICE_ID}]
    assert body["language_code"] == "th"


@pytest.mark.anyio
async def test_elevenlabs_falls_back_to_v4_and_remembers(elevenlabs_api):
    requests, answers = elevenlabs_api
    answers["eleven_v4_turbo"] = (422, ())
    answers["eleven_v4"] = (200, (b"\x00\x01",))

    assert await _speak("สวัสดี") == [b"\x00\x01"]
    assert await _speak("ครับ") == [b"\x00\x01"]
    # The refused model is tried once, then later phrases go straight to v4.
    assert [json.loads(r.content)["model_id"] for r in requests] == ["eleven_v4_turbo", "eleven_v4", "eleven_v4"]


@pytest.mark.anyio
async def test_elevenlabs_auth_failure_gives_no_audio(elevenlabs_api):
    requests, answers = elevenlabs_api
    answers["eleven_v4_turbo"] = (401, ())

    assert await _speak("สวัสดี") == []
    # A bad key is not a refused model: no second request, and turbo stays in use.
    assert len(requests) == 1 and not elevenlabs._refused_models


@pytest.mark.anyio
async def test_elevenlabs_stream_broken_mid_phrase_is_raised(monkeypatch):
    """Half a phrase must not be played as if it were complete: the caller drops it on the error."""
    monkeypatch.setattr(elevenlabs, "_refused_models", set())

    async def broken_stream():
        yield b"\x00\x01"
        raise httpx.ReadError("connection reset")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=broken_stream()))
    )
    monkeypatch.setattr(elevenlabs, "get_http_client", lambda: client)

    with pytest.raises(httpx.ReadError):
        await _speak("สวัสดี")


@pytest.mark.anyio
async def test_elevenlabs_skips_empty_text(elevenlabs_api):
    requests, _ = elevenlabs_api
    assert await _speak("  ") == []
    assert requests == []
