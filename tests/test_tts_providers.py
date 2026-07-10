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
        from portal.tts import worker as worker_mod

        worker_mod._config_cache.clear()
        configure("sqlite+aiosqlite://")
        await init_db()
        yield
        await dispose()
        worker_mod._config_cache.clear()

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


@pytest.mark.anyio
class TestConfigCache:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        from portal.tts import worker as worker_mod

        worker_mod._config_cache.clear()
        yield
        worker_mod._config_cache.clear()

    async def test_cache_hit_avoids_second_load(self, monkeypatch):
        from portal.tts.worker import TTSWorker

        calls = {"n": 0}
        sentinel = {"room_id": 7, "tts_provider_name": "supertonic"}

        async def fake_load(self, room_id):
            calls["n"] += 1
            return sentinel

        monkeypatch.setattr(TTSWorker, "_load_config", fake_load)
        worker = TTSWorker(None)

        first = await worker._load_config_cached(7)
        second = await worker._load_config_cached(7)

        assert first is sentinel
        assert second is sentinel
        assert calls["n"] == 1

    async def test_ttl_expiry_forces_reload(self, monkeypatch):
        from portal.tts import worker as worker_mod
        from portal.tts.worker import TTSWorker

        calls = {"n": 0}

        async def fake_load(self, room_id):
            calls["n"] += 1
            return {"room_id": room_id}

        monkeypatch.setattr(TTSWorker, "_load_config", fake_load)
        worker = TTSWorker(None)

        await worker._load_config_cached(7)
        # Force the cached entry to look expired without sleeping.
        _, cfg = worker_mod._config_cache[7]
        worker_mod._config_cache[7] = (0.0, cfg)
        await worker._load_config_cached(7)

        assert calls["n"] == 2

    async def test_invalidate_clears_entry(self, monkeypatch):
        from portal.tts import worker as worker_mod
        from portal.tts.worker import TTSWorker

        calls = {"n": 0}

        async def fake_load(self, room_id):
            calls["n"] += 1
            return {"room_id": room_id}

        monkeypatch.setattr(TTSWorker, "_load_config", fake_load)
        worker = TTSWorker(None)

        await worker._load_config_cached(7)
        assert 7 in worker_mod._config_cache
        worker_mod.invalidate_room_config(7)
        assert 7 not in worker_mod._config_cache
        await worker._load_config_cached(7)
        assert calls["n"] == 2

    async def test_disabled_room_none_is_cached(self, monkeypatch):
        from portal.tts.worker import TTSWorker

        calls = {"n": 0}

        async def fake_load(self, room_id):
            calls["n"] += 1
            return None

        monkeypatch.setattr(TTSWorker, "_load_config", fake_load)
        worker = TTSWorker(None)

        assert await worker._load_config_cached(7) is None
        assert await worker._load_config_cached(7) is None
        assert calls["n"] == 1


@pytest.mark.anyio
class TestPipelineCleanup:
    @pytest.fixture(autouse=True)
    def _clear_pipelines(self):
        from portal.tts import worker as worker_mod

        worker_mod._room_pipelines.clear()
        worker_mod._pipeline_shutdowns.clear()
        yield
        worker_mod._room_pipelines.clear()
        worker_mod._pipeline_shutdowns.clear()

    async def test_remove_room_pipeline_cancels_consumer(self):
        import asyncio

        from portal.tts import worker as worker_mod
        from portal.tts.worker import _RoomTTSPipeline

        pipeline = _RoomTTSPipeline(room_id=7)
        worker_mod._room_pipelines[7] = pipeline
        consumer = pipeline._consumer

        worker_mod.remove_room_pipeline(7)

        assert 7 not in worker_mod._room_pipelines
        # Drain the scheduled shutdown task(s) so cancellation completes.
        await asyncio.gather(*list(worker_mod._pipeline_shutdowns))
        assert consumer.cancelled()

    async def test_remove_missing_room_is_noop(self):
        from portal.tts import worker as worker_mod

        # No pipeline registered for this room — must not raise.
        worker_mod.remove_room_pipeline(999)

        assert 999 not in worker_mod._room_pipelines
        assert worker_mod._pipeline_shutdowns == set()
