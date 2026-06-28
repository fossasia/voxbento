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
        assert emitted == []


@pytest.mark.anyio
class TestWorkerProviderRouting:
    @pytest.fixture(autouse=True)
    async def setup_db(self):
        from portal.database import configure, dispose, init_db

        configure("sqlite+aiosqlite://")
        await init_db()
        yield
        await dispose()

    async def _seed_room(self, *, tts_provider: str, voice: str = "M1"):
        from portal.database import create_event, create_room, get_session
        from portal.models import RoomTranslationLanguage

        async with get_session() as s:
            event = await create_event(s, slug="ttscon", display_name="TTS Con")
            room = await create_room(s, event_id=event.id, display_name="Hall")
            room.floor_tts_enabled = True
            room.floor_translation_enabled = True
            # 'local' provider needs no LLM API key, keeping the test self-contained.
            room.floor_translation_provider = "local"
            room.floor_translation_model = "local-model"
            room.floor_tts_provider = tts_provider
            room.floor_tts_voice = voice
            s.add(RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French", enabled=True))
            await s.commit()
            return room.id

    async def test_supertonic_routing_does_not_require_deepgram_key(self, monkeypatch):
        """A Supertonic room must synthesize even with no Deepgram API key set."""
        from portal.tts import worker as worker_mod
        from portal.tts.worker import TTSWorker

        room_id = await self._seed_room(tts_provider="supertonic", voice="F2")

        # Avoid real LLM calls — yield a deterministic translated sentence.
        async def fake_stream_llm(self, provider, model, api_key, text, target_lang_name):
            yield "Bonjour le monde."

        monkeypatch.setattr(TTSWorker, "_stream_llm", fake_stream_llm)

        captured: dict = {}

        class FakeProvider:
            async def synthesize_stream(self, *, text_chunks, language_code, voice, on_audio):
                captured["language_code"] = language_code
                captured["voice"] = voice
                async for _ in text_chunks:
                    pass
                await on_audio(b"\x01\x02")

        def fake_factory(name, *, deepgram_api_key=None):
            captured["provider_name"] = name
            captured["deepgram_api_key"] = deepgram_api_key
            return FakeProvider()

        monkeypatch.setattr(worker_mod, "get_tts_provider", fake_factory)

        broadcasts: list[tuple] = []

        async def broadcast(room_id_, lang, audio):
            broadcasts.append((room_id_, lang, audio))

        await TTSWorker(broadcast).handle_tts(room_id, "Hello world.")

        assert captured["provider_name"] == "supertonic"
        assert captured["deepgram_api_key"] is None
        assert captured["voice"] == "F2"
        assert captured["language_code"] == "fr"
        assert broadcasts == [(room_id, "fr", b"\x01\x02")]

    async def test_deepgram_routing_requires_key_and_aborts_without_it(self, monkeypatch):
        """A Deepgram room with no key must abort before constructing a provider."""
        from portal.tts import worker as worker_mod
        from portal.tts.worker import TTSWorker

        room_id = await self._seed_room(tts_provider="deepgram")

        async def fake_stream_llm(self, provider, model, api_key, text, target_lang_name):
            yield "Bonjour."

        monkeypatch.setattr(TTSWorker, "_stream_llm", fake_stream_llm)

        called = {"factory": False}

        def fake_factory(name, *, deepgram_api_key=None):
            called["factory"] = True
            raise AssertionError("factory should not be called without a Deepgram key")

        monkeypatch.setattr(worker_mod, "get_tts_provider", fake_factory)

        broadcasts: list[tuple] = []

        async def broadcast(room_id_, lang, audio):
            broadcasts.append((room_id_, lang, audio))

        # Should return cleanly without raising or broadcasting.
        await TTSWorker(broadcast).handle_tts(room_id, "Hello world.")

        assert called["factory"] is False
        assert broadcasts == []

