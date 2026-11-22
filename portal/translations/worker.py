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
from portal.translations.providers import local as local_provider
from portal.translations.providers.anthropic import AnthropicProvider
from portal.translations.providers.base import ProviderEngine
from portal.translations.providers.gemini import GeminiProvider
from portal.translations.providers.local import LocalProvider
from portal.translations.providers.openai import OpenAIProvider
from portal.translations.scheduler import TranslationJob, TranslationScheduler
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

# Providers that run without a per-event API key.
KEYLESS_PROVIDERS = {TranslationProviderEnum.LOCAL.value, TranslationProviderEnum.SELF_HOSTED.value}

#: Schedulers made on first use, by what each one limits (#1188); see ``scheduler_for``.
SCHEDULERS: dict[tuple[str, str], TranslationScheduler] = {}


def reset_worker_state(prefix: str | None = None) -> None:
    """Clear cached translation schedulers.

    If ``prefix`` is given, only schedulers whose provider (or key string) starts with ``prefix`` are removed.
    Otherwise, all schedulers are cleared.
    """
    if prefix is None:
        SCHEDULERS.clear()
    else:
        for key in [
            k
            for k in SCHEDULERS
            if (k[0].startswith(prefix) if isinstance(k, tuple) else str(k).startswith(prefix))
        ]:
            del SCHEDULERS[key]


def _scheduler_key(provider: str, model: str, event_id: int | None) -> tuple[str, str]:
    """What one scheduler covers: whatever runs out first when it is overloaded.

    - Local: one model. Each loaded model has its own CTranslate2 threads, so rooms on
      different models never wait for each other, and loading one model blocks only its rooms.
    - Self-hosted: the operator's one server, shared by every room.
    - Hosted: one event. A hosted provider's key always comes from the event, and each key has
      its own quota, so an event whose key is slow, rate-limited or failing only holds up its
      own rooms. Keyed by the event rather than the key, so no secret is handled here.
    """
    if provider == TranslationProviderEnum.LOCAL.value:
        return provider, model
    if provider in KEYLESS_PROVIDERS or event_id is None:
        return provider, ""
    return provider, f"event-{event_id}"


def scheduler_for(provider: str, model: str = "", event_id: int | None = None) -> TranslationScheduler:
    """The scheduler shared by every room translating with ``provider`` (and its model or event).

    When the engine's limit changes (its setting did), the same scheduler takes the new limit,
    so calls already running still count against it.
    """
    engine = ENGINES.get(provider)
    capacity = engine.max_concurrency if engine is not None else TranslationEngine.max_concurrency
    key = _scheduler_key(provider, model, event_id)
    scheduler = SCHEDULERS.get(key)
    if scheduler is None:
        # Drop the ones with nothing in them, so keys and models no longer used do not pile up.
        for idle in [k for k, s in SCHEDULERS.items() if s.idle()]:
            del SCHEDULERS[idle]
        scheduler = SCHEDULERS[key] = TranslationScheduler(capacity)
    elif scheduler.capacity != capacity:
        scheduler.set_capacity(capacity)
    return scheduler


def _inference_timeout(provider: str, model: str) -> float | None:
    """Seconds an engine call may take, or None while the local model is still downloading."""
    if provider != TranslationProviderEnum.LOCAL.value:
        return 12.0
    # If the local CPU is pegged, NLLB can take 30+ seconds.
    if local_provider.is_model_loaded(model):
        # In memory already, so it translates even while an Admin downloads it again; and the
        # usual case skips the cache lookup on disk below.
        return 60.0
    try:
        prog = local_provider.get_download_progress(model)
        if prog and prog.get("status") == "downloading":
            return None
    except Exception as exc:
        # Download status is advisory; inference keeps its normal timeout.
        logger.warning(
            "Local model download status unavailable (%s); using standard inference timeout",
            type(exc).__name__,
        )
    return 60.0


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
        job: _ResolvedTranslation,
        segment_id: int,
        text: str,
        booth_id_str: str,
        uuid_segment_id: str = "",
        seq: int = 0,
    ) -> list:
        """Run the translation tasks of a resolved job: one per language with listeners."""
        tasks = self._build_language_tasks(job, segment_id, text, booth_id_str, uuid_segment_id, seq)
        if tasks:
            logger.debug("[%s] Spawning %d translation tasks for active listeners", booth_id_str, len(tasks))
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
                logger.error("[%s] handle_translation abort: segment %s not found", booth_id_str, segment_id)
                return

            logger.debug(
                "[%s] handle_translation triggered for segment %s (len=%s)", booth_id_str, segment_id, len(text)
            )

            job = await self._resolve_translation_job(session, room_id, segment, booth_id_str)

        # The connection goes back to the pool before translating: a segment can wait a while for
        # the engine, and each translation needs a connection of its own to save its result.
        # Holding this one meanwhile let busy rooms empty the pool. The job's rows stay readable
        # (sessions do not expire them).
        if job is None:
            return
        sentence = self._accumulate_sentence(text)
        await self._process_completed_sentence(job, segment_id, sentence, booth_id_str, uuid_segment_id, seq)

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
        job = scheduler_for(provider, model, event.id).admit(room.id, queue_key)

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

            # The slot covers the engine call only; saving and broadcasting happen after it is free.
            translated_text = None
            async with job:
                # Enforce a strict timeout on inference (12s for cloud, 60s for local). Checked once
                # the segment has its turn, so a download that started while it waited still counts.
                # A download runs in its own thread and holds no slot, so nothing queues behind it.
                timeout_val = _inference_timeout(provider, model)
                downloading = timeout_val is None
                if not downloading:
                    translated_text = await self._infer_holding_slot(
                        job,
                        timeout_val,
                        booth_id_str,
                        provider,
                        model,
                        api_key,
                        text,
                        lang_code,
                        lang_name,
                        source_lang_code,
                        source_lang_name,
                        room,
                        vocabulary_entries,
                    )

            if downloading:
                # Told after the slot is free, so the next segment does not wait for this broadcast.
                await self._drop_while_downloading(room.id, lang_code, booth_id_str, model, uuid_segment_id, seq, text)
                return

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

            # Saved and delivered: the segment leaves its language's queue, so slow speech
            # synthesis below does not hold back new segments.
            job.close()

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
            # A job that stopped before its result was delivered (dropped, failed or cancelled) leaves its
            # channel here; for the others this does nothing.
            job.close()

    async def _infer_holding_slot(
        self,
        job: TranslationJob,
        timeout_val: float,
        booth_id_str: str,
        provider: str,
        model: str,
        api_key: str | None,
        text: str,
        lang_code: str,
        lang_name: str,
        source_lang_code: str | None,
        source_lang_name: str,
        room: Room,
        vocabulary_entries: list,
    ) -> str | None:
        """Run the engine call within the job's slot and its timeout; None when it timed out."""
        call = asyncio.ensure_future(
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
            )
        )
        try:
            return await asyncio.wait_for(asyncio.shield(call), timeout=timeout_val)
        except asyncio.TimeoutError:
            logger.error(f"[{booth_id_str}] Translation LLM timed out after {timeout_val}s for {lang_code}.")
            return None
        finally:
            if not call.done():
                # Stop the call, but keep its slot until it has ended: a local model runs in a
                # thread that cannot be stopped, and the next call must not start beside it.
                call.cancel()
                job.keep_slot_until(call)

    async def _drop_while_downloading(
        self, room_id: int, lang_code: str, booth_id_str: str, model: str, uuid_segment_id: str, seq: int, text: str
    ) -> None:
        """Drop a segment because its local model is still downloading, and tell listeners why."""
        from portal.websockets.manager import tts_manager

        logger.info(
            "[%s] Local model %s is downloading. Dropping segment for %s.", booth_id_str, model, lang_code
        )
        await tts_manager.broadcast_bundle(
            room_id, lang_code, booth_id_str, b"", uuid_segment_id, seq, text, "", "model_downloading"
        )

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
