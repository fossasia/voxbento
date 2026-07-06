from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from portal.database import get_session
from portal.models import DBBooth, Event, Room, TranscriptTranslation
from portal.translations.constants import OPENAI_COMPATIBLE_ENDPOINTS, TranslationProviderEnum
from portal.translations.keys import get_translation_api_key
from portal.translations.prompts import build_interpretation_messages

if TYPE_CHECKING:
    from portal.models import AIVocabularyEntry

logger = logging.getLogger(__name__)


class TranslationWorker:
    """
    Handles fetching translations asynchronously for a given canonical segment.
    """

    def __init__(self, broadcast_callback):
        self.broadcast_callback = broadcast_callback

    async def handle_translation(self, room_id: int, segment_id: int, text: str, booth_id_str: str):
        """Called when a finalized STT segment is saved. Fires off LLM requests for enabled target languages."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from portal.models import TranscriptSegment

        async with get_session() as session:
            segment = await session.scalar(select(TranscriptSegment).where(TranscriptSegment.id == segment_id))
            if not segment:
                return

            provider = None
            model = None
            enabled_langs = []
            room = None
            persona = None
            style = None
            source_language_code = None
            vocabulary_enabled = True
            booth_db_id = None

            if segment.booth_id is None:
                # Floor translation
                room = await session.scalar(
                    select(Room).options(selectinload(Room.translation_languages)).where(Room.id == room_id)
                )
                if not room or not room.floor_translation_enabled:
                    return
                provider = room.floor_translation_provider
                model = room.floor_translation_model
                enabled_langs = [lang for lang in room.translation_languages if lang.enabled]
                persona = room.floor_ai_interpreter_persona
                style = room.floor_ai_interpretation_style
                source_language_code = room.floor_source_language_code
                vocabulary_enabled = room.floor_ai_vocabulary_enabled
            else:
                # Booth translation
                booth = await session.scalar(
                    select(DBBooth)
                    .options(selectinload(DBBooth.translation_languages))
                    .where(DBBooth.id == segment.booth_id)
                )
                if not booth or not booth.translation_enabled:
                    return
                provider = booth.translation_provider
                model = booth.translation_model
                enabled_langs = [lang for lang in booth.translation_languages if lang.enabled]
                room = await session.scalar(select(Room).where(Room.id == room_id))
                booth_db_id = booth.id
                # Booth-level persona/style override room-level
                persona = booth.ai_interpreter_persona or (room.floor_ai_interpreter_persona if room else None)
                style = booth.ai_interpretation_style or (room.floor_ai_interpretation_style if room else None)
                source_language_code = room.floor_source_language_code if room else None
                vocabulary_enabled = booth.ai_vocabulary_enabled

            if not provider or not model or not enabled_langs or not room:
                return

            event = await session.scalar(select(Event).where(Event.id == room.event_id))
            if not event:
                return

            api_key = self._get_translation_api_key(event, provider)
            if not api_key and provider != TranslationProviderEnum.LOCAL.value:
                logger.error(f"[{booth_id_str}] Translation API key not found for provider {provider}")
                return

            # Resolve vocabulary entries once for all target languages
            vocab_entries: list[AIVocabularyEntry] = []
            if vocabulary_enabled:
                from portal.translations.vocabulary import resolve_vocabulary_entries

                # We resolve vocabulary per-language inside the task below
                pass

            # Execute translation for all target languages concurrently
            tasks = [
                self._translate_and_broadcast(
                    event,
                    room,
                    provider,
                    model,
                    api_key,
                    lang.language_code,
                    lang.language_name,
                    segment_id,
                    text,
                    booth_id_str,
                    source_language_code=source_language_code,
                    persona=persona,
                    style=style,
                    vocabulary_enabled=vocabulary_enabled,
                    event_id=event.id,
                    room_db_id=room.id,
                    booth_db_id=booth_db_id,
                )
                for lang in enabled_langs
            ]
            await asyncio.gather(*tasks)

    def _get_translation_api_key(self, event: Event, provider: str) -> str | None:
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
        segment_id: int,
        text: str,
        booth_id_str: str,
        *,
        source_language_code: str | None = None,
        persona: str | None = None,
        style: str | None = None,
        vocabulary_enabled: bool = True,
        event_id: int | None = None,
        room_db_id: int | None = None,
        booth_db_id: int | None = None,
    ):
        try:
            # Resolve vocabulary entries for this specific target language
            vocab_entries: list[AIVocabularyEntry] = []
            if vocabulary_enabled and event_id is not None:
                from portal.translations.vocabulary import resolve_vocabulary_entries

                async with get_session() as vocab_session:
                    vocab_entries = await resolve_vocabulary_entries(
                        vocab_session,
                        event_id=event_id,
                        room_id=room_db_id,
                        booth_id=booth_db_id,
                        target_language=lang_code,
                        transcript_text=text,
                    )

            translated_text = await self._call_llm(
                provider,
                model,
                api_key,
                text,
                lang_name,
                source_language_code=source_language_code,
                persona=persona,
                style=style,
                vocabulary_entries=vocab_entries or None,
            )
            if not translated_text:
                return

            # Save to DB using an independent session to avoid concurrent transaction crashes
            async with get_session() as local_session:
                translation = TranscriptTranslation(
                    segment_id=segment_id, language_code=lang_code, text=translated_text
                )
                local_session.add(translation)
                await local_session.commit()

            # Broadcast to WebSocket
            await self.broadcast_callback(
                booth_id_str, {"type": "translation", "language_code": lang_code, "text": translated_text}
            )

        except Exception as e:
            logger.error(f"[{booth_id_str}] Translation failed for {lang_code}: {e}")

    async def _call_llm(
        self,
        provider: str,
        model: str,
        api_key: str,
        text: str,
        target_lang_name: str,
        *,
        source_language_code: str | None = None,
        persona: str | None = None,
        style: str | None = None,
        vocabulary_entries: list[AIVocabularyEntry] | None = None,
    ) -> str | None:
        import httpx

        messages = build_interpretation_messages(
            target_language_name=target_lang_name,
            text=text,
            source_language_code=source_language_code,
            persona=persona,
            style=style,
            vocabulary_entries=vocabulary_entries,
        )
        system_prompt = messages[0]["content"]

        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider in OPENAI_COMPATIBLE_ENDPOINTS:
                res = await client.post(
                    OPENAI_COMPATIBLE_ENDPOINTS[provider],
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": messages,
                    },
                )
                res.raise_for_status()
                return res.json()["choices"][0]["message"]["content"].strip()

            elif provider == TranslationProviderEnum.GEMINI.value:
                res = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
                    json={
                        "systemInstruction": {"parts": [{"text": system_prompt}]},
                        "contents": [{"parts": [{"text": text}]}],
                    },
                )
                res.raise_for_status()
                return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()

            elif provider == TranslationProviderEnum.ANTHROPIC.value:
                res = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                    json={
                        "model": model,
                        "max_tokens": 1024,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": text}],
                    },
                )
                res.raise_for_status()
                return res.json()["content"][0]["text"].strip()

            elif provider == TranslationProviderEnum.LOCAL.value:
                # Placeholder for local model inference.
                # In a real environment, you'd route this to an internal LLM endpoint like Ollama/vLLM.
                logger.warning("Local translation not fully implemented. Echoing text.")
                return f"[{target_lang_name}] {text}"

        return None
