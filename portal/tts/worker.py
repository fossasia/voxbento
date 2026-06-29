from __future__ import annotations

import asyncio
import json
import logging

import httpx

from portal.crypto import decrypt_val
from portal.database import get_session
from portal.models import Event, Room
from portal.translations.constants import OPENAI_COMPATIBLE_ENDPOINTS, TranslationProviderEnum
from portal.translations.keys import get_translation_api_key
from portal.tts.providers.base import TTSProviderEnum, get_tts_provider

logger = logging.getLogger(__name__)

# Per-room TTS routing.
#
# Deepgram streams translated sentences to its WebSocket Speak API as soon as
# each sentence is translated, so Deepgram rooms run fire-and-forget per segment
# to preserve that low-latency streaming behaviour.
#
# Supertonic synthesizes a full clip per sentence on CPU (slower than realtime
# when several segments overlap), so Supertonic rooms are funnelled through a
# per-room ordered pipeline that translates eagerly but synthesizes in strict
# arrival order, preventing audio from reordering.
#
# Routing is decided per room from its configured provider, so different rooms
# can use Deepgram and Supertonic simultaneously.
_room_pipelines: dict[int, "_RoomTTSPipeline"] = {}

# Strong references to in-flight fire-and-forget tasks (routing + Deepgram
# streaming), preventing them from being garbage collected before they finish.
_inflight: set[asyncio.Task] = set()


def enqueue_tts(room_id: int, text: str) -> None:
    """Queue a finalized segment for TTS synthesis.

    Must be called from a running event loop. The room's configured TTS provider
    decides the delivery strategy: Deepgram streams concurrently, Supertonic is
    serialized per room to preserve order.
    """
    text = (text or "").strip()
    if not text:
        return
    task = asyncio.create_task(_route(room_id, text))
    _inflight.add(task)

    def _done(t: asyncio.Task) -> None:
        _inflight.discard(t)
        try:
            t.result()
        except Exception:
            logger.exception("[TTS] enqueue_tts task failed (room_id=%s)", room_id)

    task.add_done_callback(_done)


async def _route(room_id: int, text: str) -> None:
    """Load the room config once and dispatch to the right provider strategy."""
    from portal.websockets.manager import tts_manager

    worker = TTSWorker(tts_manager.broadcast_audio)
    cfg = await worker._load_config(room_id)
    if not cfg:
        return

    if cfg["tts_provider_name"] == TTSProviderEnum.SUPERTONIC.value:
        pipeline = _room_pipelines.get(room_id)
        if pipeline is None:
            pipeline = _RoomTTSPipeline(room_id)
            _room_pipelines[room_id] = pipeline
        pipeline.submit(cfg, text)
    else:
        # Deepgram (and any other streaming provider): stream live, concurrently.
        await worker._stream_live(cfg, text)


class _RoomTTSPipeline:
    """Eager-translate, ordered-synthesize pipeline for one Supertonic room."""

    def __init__(self, room_id: int):
        from portal.websockets.manager import tts_manager

        self.room_id = room_id
        self.worker = TTSWorker(tts_manager.broadcast_audio)
        self.queue: "asyncio.Queue[tuple[dict, asyncio.Task]]" = asyncio.Queue()
        # Strong reference prevents the consumer task from being garbage collected.
        self._consumer = asyncio.create_task(self._consume())

    def submit(self, cfg: dict, text: str) -> None:
        # Translate eagerly so LLM latency overlaps across segments; synthesis is
        # still drained in arrival order by the consumer below.
        prep = asyncio.create_task(self.worker._translate_all(cfg, text))
        self.queue.put_nowait((cfg, prep))

    async def _consume(self) -> None:
        while True:
            cfg, prep = await self.queue.get()
            try:
                items = await prep
                if items:
                    await self.worker._synthesize_buffered(cfg, items)
            except Exception:
                logger.exception("[TTS] Pipeline error for room %s", self.room_id)
            finally:
                self.queue.task_done()


class TTSWorker:
    """
    Handles streaming translations and piping them to a TTS provider
    (Deepgram Aura or self-hosted Supertonic), then broadcasting the audio
    chunks to connected WebSocket clients.
    """

    def __init__(self, broadcast_audio_callback):
        # broadcast_audio_callback(room_id, lang_code, audio_bytes)
        self.broadcast_audio_callback = broadcast_audio_callback

    async def handle_tts(self, room_id: int, text: str):
        """Synthesize and broadcast TTS for one finalized segment (Floor only).

        Deepgram streams the live LLM translation straight to its Speak API;
        Supertonic translates fully, then synthesizes each sentence in order.
        """
        cfg = await self._load_config(room_id)
        if not cfg:
            return
        if cfg["tts_provider_name"] == TTSProviderEnum.SUPERTONIC.value:
            items = await self._translate_all(cfg, text)
            if items:
                await self._synthesize_buffered(cfg, items)
        else:
            await self._stream_live(cfg, text)

    async def _load_config(self, room_id: int) -> dict | None:
        """Load room/event TTS config and build the provider, or None if disabled."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        async with get_session() as session:
            room = await session.scalar(
                select(Room).options(selectinload(Room.translation_languages)).where(Room.id == room_id)
            )
            if not room or not room.floor_tts_enabled or not room.floor_translation_enabled:
                return None

            event = await session.scalar(select(Event).where(Event.id == room.event_id))
            if not event:
                return None

            provider = room.floor_translation_provider
            model = room.floor_translation_model
            langs = [(lang.language_code, lang.language_name) for lang in room.translation_languages if lang.enabled]

            if not provider or not model or not langs:
                return None

            llm_api_key = self._get_translation_api_key(event, provider)
            if not llm_api_key and provider != TranslationProviderEnum.LOCAL.value:
                logger.error("[TTS] Translation API key not found for provider %s", provider)
                return None

            tts_provider_name = room.floor_tts_provider or TTSProviderEnum.DEEPGRAM.value
            tts_voice = room.floor_tts_voice or ""

            dg_api_key = None
            if tts_provider_name == TTSProviderEnum.DEEPGRAM.value:
                dg_api_key = (
                    decrypt_val(event.encrypted_deepgram_api_key) if event.encrypted_deepgram_api_key else None
                )
                if not dg_api_key:
                    logger.error("[TTS] Deepgram API key not found for Event %s", event.id)
                    return None

            try:
                tts_provider = get_tts_provider(tts_provider_name, deepgram_api_key=dg_api_key)
            except Exception as e:
                logger.error(f"[TTS] Failed to initialise TTS provider '{tts_provider_name}': {e}")
                return None

        return {
            "room_id": room_id,
            "provider": provider,
            "model": model,
            "llm_api_key": llm_api_key,
            "langs": langs,
            "tts_provider": tts_provider,
            "tts_provider_name": tts_provider_name,
            "voice": tts_voice,
        }

    async def _stream_live(self, cfg: dict, text: str) -> None:
        """Stream the live LLM translation straight into the TTS provider.

        Used for Deepgram so audio starts as soon as the first sentence is
        translated. Languages run concurrently; segments are not serialized.
        """
        room_id = cfg["room_id"]
        tts_provider = cfg["tts_provider"]
        voice = cfg["voice"]

        async def _one(lang_code: str, lang_name: str) -> None:
            async def on_audio(audio_bytes: bytes, lc=lang_code) -> None:
                await self.broadcast_audio_callback(room_id, lc, audio_bytes)

            try:
                await tts_provider.synthesize_stream(
                    text_chunks=self._stream_llm(cfg["provider"], cfg["model"], cfg["llm_api_key"], text, lang_name),
                    language_code=lang_code,
                    voice=voice,
                    on_audio=on_audio,
                )
            except Exception:
                logger.error("TTS stream failed for room (details omitted for security analyzer)")

        await asyncio.gather(*[_one(code, name) for code, name in cfg["langs"]])

    async def _translate_all(self, cfg: dict, text: str) -> list[tuple[str, str]]:
        """Translate the segment into every enabled language, in parallel."""

        async def _tr(lang_code: str, lang_name: str) -> tuple[str, str]:
            return lang_code, await self._translate_full(
                cfg["provider"], cfg["model"], cfg["llm_api_key"], text, lang_name
            )

        results = await asyncio.gather(*[_tr(code, name) for code, name in cfg["langs"]])
        return [(code, translated) for code, translated in results if translated]

    async def _synthesize_buffered(self, cfg: dict, items: list[tuple[str, str]]) -> None:
        """Synthesize already-translated text per language (used for Supertonic)."""
        room_id = cfg["room_id"]
        tts_provider = cfg["tts_provider"]
        voice = cfg["voice"]

        async def _one(lang_code: str, translated: str) -> None:
            async def _chunks(payload=translated):
                yield payload

            async def on_audio(audio_bytes: bytes, lc=lang_code) -> None:
                await self.broadcast_audio_callback(room_id, lc, audio_bytes)

            try:
                await tts_provider.synthesize_stream(
                    text_chunks=_chunks(),
                    language_code=lang_code,
                    voice=voice,
                    on_audio=on_audio,
                )
            except Exception:
                logger.error("TTS synthesis failed for room (details omitted for security analyzer)")

        await asyncio.gather(*[_one(code, translated) for code, translated in items])

    def _get_translation_api_key(self, event: Event, provider: str) -> str | None:
        return get_translation_api_key(event, provider)

    async def _translate_full(
        self, provider: str, model: str, api_key: str, text: str, lang_name: str
    ) -> str:
        """Collect the streaming LLM translation into a single string."""
        parts: list[str] = []
        try:
            async for chunk in self._stream_llm(provider, model, api_key, text, lang_name):
                parts.append(chunk)
        except Exception as e:
            logger.error(f"[TTS] Translation failed for {lang_name}: {e}")
            return ""
        return "".join(parts).strip()

    async def _stream_llm(self, provider: str, model: str, api_key: str, text: str, target_lang_name: str):
        """Yields text chunks as they arrive from the LLM."""
        system_prompt = f"You are a professional interpreter. Translate the following text into {target_lang_name}. Output ONLY the translated text, nothing else."

        # Currently optimized for OpenAI-compatible streaming endpoints (Groq, OpenRouter, OpenAI)
        if provider not in [
            TranslationProviderEnum.OPENAI.value,
            TranslationProviderEnum.OPENROUTER.value,
            TranslationProviderEnum.GROQ.value,
        ]:
            # For non-streaming supported providers, fallback to fetching all at once and yielding it
            # This is a fallback to avoid breaking Anthropic/Gemini users immediately.
            from portal.translations.worker import TranslationWorker

            temp_worker = TranslationWorker(None)
            full_text = await temp_worker._call_llm(provider, model, api_key, text, target_lang_name)
            if full_text:
                yield full_text
            return

        endpoint = OPENAI_COMPATIBLE_ENDPOINTS[provider]

        async with httpx.AsyncClient(timeout=10.0) as client:
            async with client.stream(
                "POST",
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass
