import asyncio
import json
import logging
import re

import httpx
import websockets

from portal.crypto import decrypt_val
from portal.database import get_session
from portal.models import Event, Room
from portal.translations.constants import TranslationProviderEnum
from portal.tts.constants import get_deepgram_voice_for_language

logger = logging.getLogger(__name__)

# Sentence boundary regex
SENTENCE_BOUNDARY = re.compile(r"([^.?!]+[.?!]+)")


class TTSWorker:
    """
    Handles streaming translations and piping them to Deepgram TTS,
    then broadcasting the audio chunks to connected WebSocket clients.
    """

    def __init__(self, broadcast_audio_callback):
        # broadcast_audio_callback(room_id, lang_code, audio_bytes)
        self.broadcast_audio_callback = broadcast_audio_callback

    async def handle_tts(self, room_id: int, text: str):
        """Called when a finalized STT segment is saved (Floor only for now)."""
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        async with get_session() as session:
            room = await session.scalar(
                select(Room).options(selectinload(Room.translation_languages)).where(Room.id == room_id)
            )
            if not room or not room.floor_tts_enabled or not room.floor_translation_enabled:
                return

            event = await session.scalar(select(Event).where(Event.id == room.event_id))
            if not event:
                return

            provider = room.floor_translation_provider
            model = room.floor_translation_model
            enabled_langs = [lang for lang in room.translation_languages if lang.enabled]

            if not provider or not model or not enabled_langs:
                return

            llm_api_key = self._get_translation_api_key(event, provider)
            if not llm_api_key and provider != TranslationProviderEnum.LOCAL.value:
                logger.error(f"[TTS] Translation API key not found for provider {provider}")
                return

            dg_api_key = decrypt_val(event.encrypted_deepgram_api_key) if event.encrypted_deepgram_api_key else None
            if not dg_api_key:
                logger.error(f"[TTS] Deepgram API key not found for Event {event.id}")
                return

            # Execute TTS generation for all target languages concurrently
            tasks = [
                self._stream_llm_to_tts(
                    room_id=room.id,
                    provider=provider,
                    model=model,
                    llm_api_key=llm_api_key,
                    dg_api_key=dg_api_key,
                    lang_code=lang.language_code,
                    lang_name=lang.language_name,
                    text=text,
                )
                for lang in enabled_langs
            ]
            await asyncio.gather(*tasks)

    def _get_translation_api_key(self, event: Event, provider: str) -> str | None:
        key_map = {
            TranslationProviderEnum.OPENAI.value: event.encrypted_translation_openai_api_key,
            TranslationProviderEnum.OPENROUTER.value: event.encrypted_openrouter_api_key,
            TranslationProviderEnum.GEMINI.value: event.encrypted_gemini_api_key,
            TranslationProviderEnum.ANTHROPIC.value: event.encrypted_anthropic_api_key,
            TranslationProviderEnum.GROQ.value: event.encrypted_groq_api_key,
        }
        encrypted = key_map.get(provider)
        return decrypt_val(encrypted) if encrypted else None

    async def _stream_llm_to_tts(
        self,
        room_id: int,
        provider: str,
        model: str,
        llm_api_key: str,
        dg_api_key: str,
        lang_code: str,
        lang_name: str,
        text: str,
    ):
        dg_voice = get_deepgram_voice_for_language(lang_code)
        # Using linear16 at 24000Hz. Can be adjusted based on requirements.
        dg_url = f"wss://api.deepgram.com/v1/speak?model={dg_voice}&encoding=linear16&sample_rate=24000&container=none"

        try:
            # We connect to Deepgram WS first
            async with websockets.connect(
                dg_url, additional_headers={"Authorization": f"Token {dg_api_key}"}
            ) as dg_socket:
                # Start a background task to receive audio chunks and broadcast them
                async def receive_audio():
                    try:
                        async for message in dg_socket:
                            if isinstance(message, bytes):
                                await self.broadcast_audio_callback(room_id, lang_code, message)
                            elif isinstance(message, str):
                                try:
                                    msg_json = json.loads(message)
                                    if msg_json.get("type") == "Error":
                                        logger.error(f"[TTS] Deepgram Error: {msg_json}")
                                except json.JSONDecodeError:
                                    pass
                    except websockets.exceptions.ConnectionClosed:
                        pass
                    except Exception as e:
                        logger.error(f"[TTS] Receive error: {e}")

                receive_task = asyncio.create_task(receive_audio())

                # Now stream from LLM and send Speak requests
                buffer = ""
                async for chunk in self._stream_llm(provider, model, llm_api_key, text, lang_name):
                    buffer += chunk

                    # Send complete sentences to Deepgram
                    while True:
                        match = SENTENCE_BOUNDARY.search(buffer)
                        if not match:
                            break

                        sentence = match.group(1).strip()
                        split_idx = match.end()

                        if sentence:
                            await dg_socket.send(json.dumps({"type": "Speak", "text": sentence}))

                        buffer = buffer[split_idx:].lstrip()

                # Send any remaining text
                remaining = buffer.strip()
                if remaining:
                    await dg_socket.send(json.dumps({"type": "Speak", "text": remaining}))

                # Flush to tell Deepgram we are done generating for this segment
                await dg_socket.send(json.dumps({"type": "Flush"}))

                # Wait a short moment for remaining audio to arrive, then close
                # Deepgram sends a 'Flushed' metadata message when done.
                # The receive_audio task will handle that, but we'll just wait a couple seconds to be safe.
                # Actually, the proper way is to wait for the 'Flushed' event.
                # But to keep it simple, we await the receive_task to finish naturally if WS closes,
                # or timeout after a few seconds.
                try:
                    await asyncio.wait_for(receive_task, timeout=5.0)
                except asyncio.TimeoutError:
                    pass

        except Exception as e:
            logger.error(f"[TTS] Error in TTS stream for room {room_id} lang {lang_code}: {e}")

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

        endpoint = "https://api.openai.com/v1/chat/completions"
        if provider == TranslationProviderEnum.OPENROUTER.value:
            endpoint = "https://openrouter.ai/api/v1/chat/completions"
        elif provider == TranslationProviderEnum.GROQ.value:
            endpoint = "https://api.groq.com/openai/v1/chat/completions"

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
