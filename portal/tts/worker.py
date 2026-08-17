from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator

from portal.crypto import decrypt_val
from portal.database import get_session
from portal.models import Event, Room
from portal.tts.providers.base import TTSProviderEnum, get_tts_provider

logger = logging.getLogger(__name__)

_config_cache: dict[int, tuple[float, dict | None]] = {}
_CONFIG_TTL_SECONDS = 300.0

def invalidate_room_config(room_id: int) -> None:
    _config_cache.pop(room_id, None)

async def _load_config_cached(room_id: int) -> dict | None:
    now = time.monotonic()
    entry = _config_cache.get(room_id)
    if entry is not None and entry[0] > now:
        return entry[1]
    cfg = await _load_config(room_id)
    _config_cache[room_id] = (now + _CONFIG_TTL_SECONDS, cfg)
    return cfg

async def _load_config(room_id: int) -> dict | None:
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

        tts_provider_name = room.floor_tts_provider or TTSProviderEnum.DEEPGRAM.value
        tts_voice = room.floor_tts_voice or ""

        dg_api_key = None
        if tts_provider_name == TTSProviderEnum.DEEPGRAM.value:
            dg_api_key = decrypt_val(event.encrypted_deepgram_api_key) if event.encrypted_deepgram_api_key else None
            if not dg_api_key:
                logger.error("[TTS] Deepgram API key not found for Event %s", event.id)
                return None

        try:
            tts_provider = get_tts_provider(tts_provider_name, deepgram_api_key=dg_api_key)
        except Exception as e:
            logger.error(f"[TTS] Failed to initialise TTS provider '{tts_provider_name}': {e}")
            return None

    return {
        "tts_provider": tts_provider,
        "voice": tts_voice,
    }

async def synthesize(room_id: int, text: str, language_code: str) -> bytes | None:
    """Atomic TTS synthesis for the Server-Side Sync pipeline."""
    cfg = await _load_config_cached(room_id)
    if not cfg:
        return None

    tts_provider = cfg["tts_provider"]
    voice = cfg["voice"]

    chunks = []

    async def _on_audio(chunk: bytes) -> None:
        chunks.append(chunk)

    async def _text_iterator() -> AsyncIterator[str]:
        yield text

    await tts_provider.synthesize_stream(
        text_chunks=_text_iterator(),
        language_code=language_code,
        voice=voice,
        on_audio=_on_audio
    )

    return b"".join(chunks)
