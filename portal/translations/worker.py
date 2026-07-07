import asyncio
import logging

from portal.database import get_session
from portal.models import DBBooth, Event, Room, TranscriptTranslation
from portal.translations.constants import OPENAI_COMPATIBLE_ENDPOINTS, TranslationProviderEnum
from portal.translations.keys import get_translation_api_key

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

            if not provider or not model or not enabled_langs or not room:
                return

            event = await session.scalar(select(Event).where(Event.id == room.event_id))
            if not event:
                return

            api_key = self._get_translation_api_key(event, provider)
            if not api_key and provider != TranslationProviderEnum.LOCAL.value:
                logger.error(f"[{booth_id_str}] Translation API key not found for provider {provider}")
                return

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
    ):
        try:
            translated_text = await self._call_llm(provider, model, api_key, text, lang_name)
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

    async def _call_llm(self, provider: str, model: str, api_key: str, text: str, target_lang_name: str) -> str | None:
        import httpx

        system_prompt = f"You are a professional interpreter. Translate the following text into {target_lang_name}. Output ONLY the translated text, nothing else."

        timeout = httpx.Timeout(10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider in OPENAI_COMPATIBLE_ENDPOINTS:
                res = await client.post(
                    OPENAI_COMPATIBLE_ENDPOINTS[provider],
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}],
                    },
                )
                res.raise_for_status()
                return res.json()["choices"][0]["message"]["content"].strip()

            elif provider == TranslationProviderEnum.GEMINI.value:
                res = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": api_key},
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
