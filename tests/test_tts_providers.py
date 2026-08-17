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

