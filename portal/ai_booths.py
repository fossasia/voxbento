"""AI (TTS) interpretation booths: which languages a room offers and how listeners reach them."""

from __future__ import annotations

from portal.booth_identity import make_ai_booth_id
from portal.models import Room, RoomTranslationLanguage
from portal.utils import public_ws_url


def excluded_ai_languages(room: Room, human_language_codes: set[str]) -> set[str]:
    """Languages *room* can't offer as AI audio.

    A human booth for a language takes precedence over AI audio, and the floor
    language is never synthesized back into itself.
    """
    excluded = set(human_language_codes)
    if room.floor_language_code:
        excluded.add(room.floor_language_code)
    return excluded


def ai_booth_languages(room: Room, human_language_codes: set[str]) -> list[RoomTranslationLanguage]:
    """Floor translation languages that get an AI booth in *room*.

    Floor translation and TTS must be on for the room, and translation and TTS
    must be on for the language. See :func:`excluded_ai_languages` for the rest.
    """
    if not (room.floor_translation_enabled and room.floor_tts_enabled):
        return []
    excluded = excluded_ai_languages(room, human_language_codes)
    return [
        lang
        for lang in room.translation_languages
        if lang.enabled and lang.tts_enabled and lang.language_code not in excluded
    ]


def ai_booth_entry(event_slug: str, room: Room, lang: RoomTranslationLanguage) -> dict:
    """Booth-list entry for one AI booth, shared by the listener page and the booths API."""
    booth_id = make_ai_booth_id(event_slug, room.id, lang.language_code)
    return {
        "id": booth_id,
        "room_id": room.id,
        "eventyay_room_id": room.eventyay_room_id,
        "language_code": lang.language_code,
        "language_name": lang.language_name,
        "type": "ai",
        "label": f"{lang.language_name} (AI)",
        "is_ai": True,
        "whip_url": None,
        "whep_url": None,
        "tts_ws_url": public_ws_url(f"/ws/tts/{booth_id}"),
        "audio_delay_ms": room.audio_delay_ms,
    }
