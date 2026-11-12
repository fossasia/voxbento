from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from portal import youtube_captions
from portal.config import settings
from portal.database import get_session
from portal.models import DBBooth, Event, Room, TranscriptTranslation
from portal.transcription.timing import segment_timing, timing_metadata
from portal.translations.constants import OPENAI_COMPATIBLE_ENDPOINTS, TranslationProviderEnum
from portal.translations.engine import EngineKind, Language, TranslationContext, TranslationEngine
from portal.translations.keys import get_translation_api_key
from portal.translations.providers.anthropic import AnthropicProvider
from portal.translations.providers.base import ProviderEngine
from portal.translations.providers.gemini import GeminiProvider
from portal.translations.providers.local import LocalProvider
from portal.translations.providers.openai import OpenAIProvider
from portal.translations.scheduler import TranslationScheduler
from portal.translations.vocabulary import resolve_vocabulary_entries

logger = logging.getLogger(__name__)

#: The glossary is optional context, so its lookup must never eat into the inference budget.
VOCABULARY_LOOKUP_TIMEOUT = 2.0

openai_provider = OpenAIProvider()


def _api_concurrency() -> int:
    return settings.translation_api_concurrency


def _self_hosted_concurrency() -> int:
    return settings.self_hosted_llm_concurrency


#: The engine behind each translation provider a room or booth can select.
ENGINES: dict[str, TranslationEngine] = {
    TranslationProviderEnum.GEMINI.value: ProviderEngine(
        TranslationProviderEnum.GEMINI.value, GeminiProvider(), EngineKind.LLM_API, max_concurrency=_api_concurrency
    ),
    TranslationProviderEnum.ANTHROPIC.value: ProviderEngine(
        TranslationProviderEnum.ANTHROPIC.value,
        AnthropicProvider(),
        EngineKind.LLM_API,
        max_concurrency=_api_concurrency,
    ),
    TranslationProviderEnum.LOCAL.value: LocalProvider(),
}
for p in OPENAI_COMPATIBLE_ENDPOINTS.keys():
    ENGINES[p] = ProviderEngine(p, openai_provider, EngineKind.OPENAI_COMPATIBLE_LLM, max_concurrency=_api_concurrency)
# The operator's self-hosted server speaks the same protocol; the provider picks its endpoint by name.
ENGINES[TranslationProviderEnum.SELF_HOSTED.value] = ProviderEngine(
    TranslationProviderEnum.SELF_HOSTED.value,
    openai_provider,
    EngineKind.OPENAI_COMPATIBLE_LLM,
    max_concurrency=_self_hosted_concurrency,
)

#: One scheduler per provider, made on first use (#1188).
SCHEDULERS: dict[str, TranslationScheduler] = {}


def scheduler_for(provider: str) -> TranslationScheduler:
    """The scheduler every room shares for ``provider``'s engine.

    Remade when the engine's limit changes (its setting did), so the new limit applies to
    new segments; jobs already admitted finish under the old one.
    """
    engine = ENGINES.get(provider)
    capacity = engine.max_concurrency if engine is not None else TranslationEngine.max_concurrency
    scheduler = SCHEDULERS.get(provider)
    if scheduler is None or scheduler.capacity != capacity:
        scheduler = SCHEDULERS[provider] = TranslationScheduler(capacity)
    return scheduler


def _inference_timeout(provider: str, model: str) -> float | None:
    """Seconds an engine call may take, or None while the local model is still downloading."""
    if provider != TranslationProviderEnum.LOCAL.value:
        return 12.0
    # If the local CPU is pegged, NLLB can take 30+ seconds.
    try:
        from portal.translations.providers.local import get_download_progress

        prog = get_download_progress(model)
        if prog and prog.get("status") == "downloading":
            return None
    except Exception as exc:
        # Download status is advisory; inference keeps its normal timeout.
        logger.warning(
            "Local model download status unavailable (%s); using standard inference timeout",
            type(exc).__name__,
        )
    return 60.0


# Providers that run without a per-event API key.
KEYLESS_PROVIDERS = {TranslationProviderEnum.LOCAL.value, TranslationProviderEnum.SELF_HOSTED.value}


@dataclass
class _ResolvedTranslation:
    """Resolved provider/model/room/language state for a single translation segment."""

    event: Event
    room: Room
    provider: str
    model: str
    api_key: str | None
    enabled_langs: list
    source_lang_code: str | None
    source_lang_name: str
    source_booth_id: int | None = None


@dataclass
class _JobContext:
    """Resolved room, provider, model, enabled languages and source language code."""

    room: Room
    provider: str
    model: str
    enabled_langs: list
    source_lang_code: str | None


def _enabled_translation_langs(owner) -> list:
    """Enabled translation languages for a room or booth.

    Centralised so the floor and booth paths cannot drift apart.
    """
    return [lang for lang in owner.translation_languages if lang.enabled]


def _resolve_source_lang_name(source_lang_code: str | None) -> str:
    """Human-readable source language name, falling back to the code (or English)."""
    import pycountry

    source_lang_obj = pycountry.languages.get(alpha_2=source_lang_code) if source_lang_code else None
    return source_lang_obj.name if source_lang_obj else (source_lang_code or "English")


async def _broadcast_source_language(
    room_id: int,
    language_code: str,
    booth_id_str: str,
    uuid_segment_id: str,
    seq: int,
    text: str,
    target_booth_id: str,
) -> None:
    """Broadcast the source-language bundle instantly (translation == original text)."""
    from portal.transcription.timing import timing_metadata
    from portal.websockets.manager import listener_manager, tts_manager

    await tts_manager.broadcast_bundle(
        room_id, language_code, booth_id_str, b"", uuid_segment_id, seq, text, text, None, audio_pending=False
    )
    if target_booth_id:
        await listener_manager.broadcast(
            target_booth_id,
            {
                "type": "translated_caption",
                "status": "final",
                "text": text,
                "segment_id": uuid_segment_id,
                "seq": seq,
                **timing_metadata(),
            },
        )
    if booth_id_str.endswith("-floor") and youtube_captions.wants(room_id, language_code):
        # The floor's own text is offered as "floor"; a YouTube track set to this
        # language code needs it offered under that code too.
        timing = segment_timing.get()
        youtube_captions.offer(room_id, language_code, text, timing.source_received_at_ms if timing else None)


class TranslationWorker:
    """
    Handles fetching translations asynchronously for a given canonical segment.
    """

    def __init__(self, broadcast_callback):
        self.broadcast_callback = broadcast_callback

    def _accumulate_sentence(self, current: str | None, chunk: str = "") -> str:
        """Accumulate sentence fragments or normalize text for completed-sentence processing."""
        if not current:
            return chunk.strip() if chunk else ""
        if not chunk:
            return current.strip()
        c = current.strip()
        k = chunk.strip()
        if k in {".", ",", "!", "?", ";", ":", "..."}:
            return f"{c}{k}"
        return f"{c} {k}"

    async def _process_completed_sentence(
        self,
        session,
        room_id: int,
        segment,
        segment_id: int,
        text: str,
        booth_id_str: str,
        uuid_segment_id: str = "",
        seq: int = 0,
    ) -> list | None:
        """Process a completed sentence segment by resolving the job and executing translation tasks."""
        job = await self._resolve_translation_job(session, room_id, segment, booth_id_str)
        if job is None:
            return None

        tasks = self._build_language_tasks(job, segment_id, text, booth_id_str, uuid_segment_id, seq)
        if tasks:
            logger.error(f"[{booth_id_str}] Spawning {len(tasks)} translation tasks for active listeners")
            await asyncio.gather(*tasks)
        return tasks

    async def handle_translation(
        self, room_id: int, segment_id: int, text: str, booth_id_str: str, uuid_segment_id: str = "", seq: int = 0
    ):
        """Called when a finalized STT segment is saved. Fires off LLM requests for enabled target languages."""
        from sqlalchemy import select

        from portal.models import TranscriptSegment

        async with get_session() as session:
            segment = await session.scalar(select(TranscriptSegment).where(TranscriptSegment.id == segment_id))
            if not segment:
                logger.error(f"[{booth_id_str}] handle_translation abort: segment {segment_id} not found")
                return

            logger.debug(
                "[%s] handle_translation triggered for segment %s (len=%s)", booth_id_str, segment_id, len(text)
            )

            sentence = self._accumulate_sentence(text)
            await self._process_completed_sentence(
                session, room_id, segment, segment_id, sentence, booth_id_str, uuid_segment_id, seq
            )

    async def _resolve_translation_job(
        self, session, room_id: int, segment, booth_id_str: str
    ) -> _ResolvedTranslation | None:
        """Resolve the room, provider, model, enabled languages and source language for a segment.

        Returns ``None`` (after logging, where applicable) when the segment cannot be translated.
        """
        from sqlalchemy import select

        context = await self._resolve_job_context(session, room_id, segment, booth_id_str)
        if not context:
            return None

        event = await session.scalar(select(Event).where(Event.id == context.room.event_id))
        if not event:
            return None

        api_key = self._get_translation_api_key(event, context.provider)
        if not api_key and context.provider not in KEYLESS_PROVIDERS:
            logger.error(f"[{booth_id_str}] Translation API key not found for provider {context.provider}")
            return None

        return _ResolvedTranslation(
            event=event,
            room=context.room,
            provider=context.provider,
            model=context.model,
            api_key=api_key,
            enabled_langs=context.enabled_langs,
            source_lang_code=context.source_lang_code,
            source_lang_name=_resolve_source_lang_name(context.source_lang_code),
            source_booth_id=segment.booth_id,
        )

    async def _resolve_job_context(self, session, room_id: int, segment, booth_id_str: str) -> _JobContext | None:
        """Fetch the room/booth config and validate that all required settings exist."""
        if segment.booth_id is None:
            context = await self._resolve_floor_context(session, room_id, booth_id_str)
        else:
            context = await self._resolve_booth_context(session, room_id, segment)

        if not context:
            return None

        if not context.provider or not context.model or not context.enabled_langs:
            logger.error(
                f"[{booth_id_str}] handle_translation abort: missing config. "
                f"provider={context.provider}, model={context.model}, langs={context.enabled_langs}"
            )
            return None

        return context

    async def _resolve_floor_context(self, session, room_id: int, booth_id_str: str) -> _JobContext | None:
        """Floor translation configuration from the room."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        room = await session.scalar(
            select(Room).options(selectinload(Room.translation_languages)).where(Room.id == room_id)
        )
        if not room or not room.floor_translation_enabled:
            logger.error(
                f"[{booth_id_str}] handle_translation abort: floor_translation_enabled is false for room {room_id}"
            )
            return None

        return _JobContext(
            room=room,
            provider=room.floor_translation_provider,
            model=room.floor_translation_model,
            enabled_langs=_enabled_translation_langs(room),
            source_lang_code=room.floor_language_code,
        )

    async def _resolve_booth_context(self, session, room_id: int, segment) -> _JobContext | None:
        """Booth translation configuration from the booth."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        booth = await session.scalar(
            select(DBBooth).options(selectinload(DBBooth.translation_languages)).where(DBBooth.id == segment.booth_id)
        )
        if not booth or not booth.translation_enabled:
            return None

        room = await session.scalar(select(Room).where(Room.id == room_id))
        if not room:
            return None

        return _JobContext(
            room=room,
            provider=booth.translation_provider,
            model=booth.translation_model,
            enabled_langs=_enabled_translation_langs(booth),
            source_lang_code=booth.language_code,
        )

    def _build_language_tasks(
        self,
        job: _ResolvedTranslation,
        segment_id: int,
        text: str,
        booth_id_str: str,
        uuid_segment_id: str,
        seq: int,
    ) -> list:
        """Construct the async translation and broadcast tasks for a resolved job."""
        from portal.websockets.manager import listener_manager, tts_manager

        tasks = []
        for lang in job.enabled_langs:
            target_booth_id = f"{job.event.slug}-{job.room.id}-{lang.language_code}"

            if lang.language_code == job.source_lang_code:
                tasks.append(
                    _broadcast_source_language(
                        job.room.id,
                        lang.language_code,
                        booth_id_str,
                        uuid_segment_id,
                        seq,
                        text,
                        target_booth_id,
                    )
                )
            else:
                has_tts = tts_manager.has_listeners(job.room.id, lang.language_code, booth_id_str)
                has_text = listener_manager.has_listeners(target_booth_id)
                # A language posted to YouTube's caption track is translated even with no listeners. Only the
                # floor's translations are posted there, so a booth's segments keep the listener rule.
                youtube_only = not has_tts and not has_text
                if youtube_only and not (
                    booth_id_str.endswith("-floor") and youtube_captions.wants(job.room.id, lang.language_code)
                ):
                    continue

                tasks.append(
                    self._translate_and_broadcast(
                        job.event,
                        job.room,
                        job.provider,
                        job.model,
                        job.api_key,
                        lang.language_code,
                        lang.language_name,
                        job.source_lang_code,
                        job.source_lang_name,
                        segment_id,
                        text,
                        booth_id_str,
                        uuid_segment_id,
                        seq,
                        target_booth_id,
                        job.source_booth_id,
                        synthesize_audio=not youtube_only,
                    )
                )

        return tasks

    def _get_translation_api_key(self, event: Event, provider: str) -> str | None:
        """Resolve the API key for the given event and translation provider."""
        return get_translation_api_key(event, provider)

    async def _translate_and_broadcast(
        self,
        event: Event,
        room: Room,
        provider: str,
        model: str,
        api_key: str,
        lang_code: str,
        lang_name: str,
        source_lang_code: str | None,
        source_lang_name: str,
        segment_id: int,
        text: str,
        booth_id_str: str,
        uuid_segment_id: str,
        seq: int,
        target_booth_id: str | None = None,
        source_booth_id: int | None = None,
        synthesize_audio: bool = True,
    ):
        """Translate and deliver a segment, keeping advisory status failures outside inference.

        ``synthesize_audio`` is False when nobody listens to this language (it is only translated for the
        YouTube caption track), so no speech is generated for it.
        """
        from portal.websockets.manager import tts_manager

        queue_key = f"{booth_id_str}:{lang_code}"
        # Rooms take turns for the engine, and so do the languages within a room.
        job = scheduler_for(provider).admit(room.id, queue_key)

        if job is None:
            logger.warning(f"[{booth_id_str}] Queue full for {lang_code}. Dropping segment {seq}.")
            await tts_manager.broadcast_bundle(
                room.id, lang_code, booth_id_str, b"", uuid_segment_id, seq, text, "", "pipeline_failed"
            )
            return

        try:
            # Resolved before waiting for an engine slot: the glossary is optional context, and a
            # slow lookup must not hold a slot while other rooms wait.
            vocabulary_entries = []
            if room.floor_ai_vocabulary_enabled:
                try:
                    async with asyncio.timeout(VOCABULARY_LOOKUP_TIMEOUT):
                        async with get_session() as vocabulary_session:
                            vocabulary_entries = await resolve_vocabulary_entries(
                                vocabulary_session,
                                event_id=event.id,
                                room_id=room.id,
                                booth_id=source_booth_id,
                                target_language=lang_code,
                                transcript_text=text,
                            )
                except (Exception, asyncio.TimeoutError) as e:
                    logger.warning(
                        f"[{booth_id_str}] Vocabulary lookup failed for {lang_code}: {e!r}. "
                        "Translating without glossary entries."
                    )
                    vocabulary_entries = []

            # Enforce a strict timeout on inference (12s for cloud, 60s for local).
            timeout_val = _inference_timeout(provider, model)
            if timeout_val is None:
                # Checked before waiting for a slot, so a download never queues segments behind it.
                logger.info(f"[{booth_id_str}] Local model {model} is downloading. Dropping segment for {lang_code}.")
                await tts_manager.broadcast_bundle(
                    room.id, lang_code, booth_id_str, b"", uuid_segment_id, seq, text, "", "model_downloading"
                )
                return

            # The slot covers the engine call only; saving and broadcasting happen after it is free.
            async with job:
                try:
                    translated_text = await asyncio.wait_for(
                        self._call_llm(
                            provider,
                            model,
                            api_key,
                            text,
                            lang_name,
                            source_lang_name,
                            target_lang_code=lang_code,
                            source_language_code=source_lang_code,
                            persona=room.floor_ai_interpreter_persona,
                            style=room.floor_ai_interpretation_style,
                            vocabulary_entries=vocabulary_entries,
                        ),
                        timeout=timeout_val,
                    )
                except asyncio.TimeoutError:
                    logger.error(f"[{booth_id_str}] Translation LLM timed out after {timeout_val}s for {lang_code}.")
                    translated_text = None

            if translated_text is None:
                await tts_manager.broadcast_bundle(
                    room.id, lang_code, booth_id_str, b"", uuid_segment_id, seq, text, "", "pipeline_failed"
                )
                return

            if not translated_text.strip():
                # The engine ran and had nothing to say for this segment, which is not a failure.
                # Close the segment (no error, no audio to wait for) so listeners move on without
                # a message; there is nothing to store or speak.
                await tts_manager.broadcast_bundle(
                    room.id, lang_code, booth_id_str, b"", uuid_segment_id, seq, text, "", None, audio_pending=False
                )
                return

            # Save to DB using an independent session to avoid concurrent transaction crashes
            async with get_session() as local_session:
                translation = TranscriptTranslation(segment_id=segment_id, language_code=lang_code, text=translated_text)
                local_session.add(translation)
                await local_session.flush()

            # Broadcast Stage 1 (Text Ready) immediately with empty audio
            await tts_manager.broadcast_bundle(
                room.id, lang_code, booth_id_str, b"", uuid_segment_id, seq, text, translated_text, None
            )
            if target_booth_id:
                from portal.websockets.manager import listener_manager

                await listener_manager.broadcast(
                    target_booth_id,
                    {
                        "type": "translated_caption",
                        "status": "final",
                        "text": translated_text,
                        "segment_id": uuid_segment_id,
                        "seq": seq,
                        **timing_metadata(),
                    },
                )
            if booth_id_str.endswith("-floor"):
                # Non-blocking: queues the translation for the room's YouTube caption track, if it asks for this language.
                timing = segment_timing.get()
                youtube_captions.offer(
                    room.id, lang_code, translated_text, timing.source_received_at_ms if timing else None
                )

            if not synthesize_audio:
                return

            from portal.tts.worker import synthesize

            timeout_s = max(2.0, 0.3 * len(translated_text))
            error = None
            audio_bytes = b""

            try:
                synth_bytes = await asyncio.wait_for(synthesize(room.id, translated_text, lang_code), timeout=timeout_s)
                if synth_bytes:
                    audio_bytes = synth_bytes
                else:
                    error = "tts_unavailable"
            except asyncio.TimeoutError:
                logger.warning(f"[{booth_id_str}] TTS timeout for {lang_code} after {timeout_s}s.")
                error = "tts_timeout"
            except Exception as e:
                logger.error(f"[{booth_id_str}] TTS error for {lang_code}: {e}")
                error = "tts_error"

            # Broadcast Stage 2 (Audio Ready)
            await tts_manager.broadcast_bundle(
                room.id, lang_code, booth_id_str, audio_bytes, uuid_segment_id, seq, text, translated_text, error
            )

        except Exception as e:
            logger.error(
                f"[{booth_id_str}] Translation failed for {lang_code}: {e} (api_key is None? {api_key is None})"
            )
            await tts_manager.broadcast_bundle(
                room.id, lang_code, booth_id_str, b"", uuid_segment_id, seq, text, "", "pipeline_failed"
            )
        finally:
            # A job that never reached the engine (dropped, failed or cancelled) leaves its channel here.
            job.close()

    async def _call_llm(
        self,
        provider: str,
        model: str,
        api_key: str | None,
        text: str,
        target_lang_name: str,
        source_lang_name: str = "English",
        *,
        target_lang_code: str = "",
        source_language_code: str | None = None,
        persona: str | None = None,
        style: str | None = None,
        vocabulary_entries: list | None = None,
    ) -> str | None:
        engine = ENGINES.get(provider)
        if not engine:
            logger.error(f"Translation provider {provider} not supported.")
            return None

        translated = await engine.translate_text(
            Language(code=source_language_code or "", name=source_lang_name),
            Language(code=target_lang_code, name=target_lang_name),
            text,
            TranslationContext(
                persona=persona,
                style=style,
                vocabulary={target_lang_code: vocabulary_entries} if vocabulary_entries else {},
            ),
            model=model,
            api_key=api_key,
        )
        if not translated:
            logger.warning(f"Provider {provider} returned empty translation for text: '{text}'")
        return translated
