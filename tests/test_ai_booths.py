"""AI (TTS) interpretation booths: configuration, listing, and the room sync contract."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from portal.auth import create_admin_token
from portal.config import settings
from portal.models import DBBooth, Room, RoomTranslationLanguage

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
def admin_cookie():
    return {"admin_token": create_admin_token()}


@pytest.fixture
async def ai_room():
    """An event whose room translates English floor audio into de/fr/es with TTS, and has a human fr booth."""
    from portal.database import create_booth, create_event, create_room, get_session

    async with get_session() as s:
        event = await create_event(s, slug="aicon", display_name="AI Con")
        event.listener_join_code = "LISTEN"
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_language_code = "en"
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_tts_enabled = True
        for code, name in [("de", "German"), ("fr", "French"), ("en", "English")]:
            s.add(RoomTranslationLanguage(room_id=room.id, language_code=code, language_name=name, tts_enabled=True))
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="es", language_name="Spanish", enabled=False))
        await create_booth(s, event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
    return event, room


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _room_languages(room_id: int) -> dict[str, RoomTranslationLanguage]:
    from portal.database import get_session

    async with get_session() as s:
        rows = await s.scalars(select(RoomTranslationLanguage).where(RoomTranslationLanguage.room_id == room_id))
        return {row.language_code: row for row in rows}


# ── Helpers ──────────────────────────────────────────────────────────────────


async def test_public_ws_url_follows_base_url_scheme(monkeypatch):
    from portal.utils import public_ws_url

    monkeypatch.setattr(settings, "public_base_url", "https://voxbento.example")
    assert public_ws_url("/ws/tts/aicon-1-ai-de") == "wss://voxbento.example/ws/tts/aicon-1-ai-de"
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8100/")
    assert public_ws_url("/ws/tts/aicon-1-ai-de") == "ws://localhost:8100/ws/tts/aicon-1-ai-de"


async def test_make_ai_booth_id():
    from portal.booth_identity import make_ai_booth_id

    assert make_ai_booth_id("pycon2026", 14, "de") == "pycon2026-14-ai-de"


async def test_normalize_tts_voice_keeps_only_provider_voices():
    from portal.tts.constants import normalize_tts_voice

    assert normalize_tts_voice("supertonic", "F2") == "F2"
    assert normalize_tts_voice("supertonic", "aura-2-julius-de") == ""
    assert normalize_tts_voice("deepgram", "aura-2-julius-de") == "aura-2-julius-de"
    assert normalize_tts_voice("deepgram", "M1") == ""
    assert normalize_tts_voice("deepgram", "aura-invalid") == ""
    assert normalize_tts_voice("deepgram", "") == ""


async def test_deepgram_voice_only_used_for_its_language():
    from portal.tts.constants import get_deepgram_voice_for_language

    assert get_deepgram_voice_for_language("de", "aura-2-julius-de") == "aura-2-julius-de"
    # A German voice must not read French text; French gets its own voice.
    assert get_deepgram_voice_for_language("fr", "aura-2-julius-de") == "aura-2-agathe-fr"
    assert get_deepgram_voice_for_language("fr", "M1") == "aura-2-agathe-fr"
    assert get_deepgram_voice_for_language("en", "aura-asteria-en") == "aura-asteria-en"


# ── Listing AI booths ────────────────────────────────────────────────────────


async def test_booths_api_lists_ai_booths_with_human_precedence(ai_room, monkeypatch):
    event, room = ai_room
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8100")
    async with _client() as c:
        resp = await c.get(f"/api/events/{event.slug}/booths")
    assert resp.status_code == 200, resp.text
    ai = [b for b in resp.json()["booths"] if b.get("is_ai")]

    # fr has a human booth, en is the floor language, es is not translated: only de is AI.
    assert [b["language_code"] for b in ai] == ["de"]
    booth = ai[0]
    assert booth["id"] == f"aicon-{room.id}-ai-de"
    assert booth["type"] == "ai"
    assert booth["label"] == "German (AI)"
    assert booth["whep_url"] is None
    assert booth["tts_ws_url"] == f"ws://localhost:8100/ws/tts/aicon-{room.id}-ai-de"


async def test_no_ai_booths_without_room_tts(ai_room):
    from portal.database import get_session

    event, room = ai_room
    async with get_session() as s:
        (await s.get(Room, room.id)).floor_tts_enabled = False
    async with _client() as c:
        resp = await c.get(f"/api/events/{event.slug}/booths")
    assert not [b for b in resp.json()["booths"] if b.get("is_ai")]


async def test_listener_page_offers_ai_booths_and_a_listener_token(ai_room):
    from portal.auth import decode_token

    event, room = ai_room
    async with _client() as c:
        resp = await c.get(f"/listener/{event.slug}?code=LISTEN")
    assert resp.status_code == 200
    assert f'"aicon-{room.id}-ai-de"' in resp.text
    assert f'"aicon-{room.id}-ai-fr"' not in resp.text

    marker = '"listenerToken": "'
    token = resp.text.split(marker, 1)[1].split('"', 1)[0]
    payload = decode_token(token)
    assert payload["role"] == "listener"
    assert payload["event_slug"] == event.slug


# ── Admin TTS settings ───────────────────────────────────────────────────────


async def test_admin_tts_save_ignores_human_and_floor_languages(ai_room, admin_cookie):
    event, room = ai_room
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{event.id}/rooms/{room.id}/edit",
            data={
                "form_section": "tts",
                "floor_tts_enabled": "on",
                "floor_tts_provider": "supertonic",
                "floor_tts_voice": "aura-2-julius-de",  # not a Supertonic voice
                # fr has a human booth, en is the floor language, xx is not a language.
                "floor_tts_languages": ["es", "fr", "en", "it", "xx"],
            },
            cookies=admin_cookie,
            follow_redirects=False,
        )
    assert resp.status_code == 303

    langs = await _room_languages(room.id)
    assert {code for code, lang in langs.items() if lang.tts_enabled} == {"es", "it"}
    # AI audio needs the translation: a disabled row is re-enabled and a new one is created enabled.
    assert langs["es"].enabled
    assert langs["it"].enabled and langs["it"].language_name == "Italian"
    assert "xx" not in langs

    from portal.database import get_session

    async with get_session() as s:
        saved = await s.get(Room, room.id)
        assert saved.floor_tts_provider == "supertonic"
        assert saved.floor_tts_voice == ""  # the provider picks a voice per language


async def test_removing_a_translation_language_turns_its_tts_off(ai_room, admin_cookie):
    event, room = ai_room
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{event.id}/rooms/{room.id}/edit",
            data={
                "form_section": "translation",
                "floor_translation_enabled": "on",
                "floor_translation_provider": "local",
                "floor_translation_model": "nllb-200-distilled-600M",
                "floor_translation_languages": ["fr", "en"],
            },
            cookies=admin_cookie,
            follow_redirects=False,
        )
    assert resp.status_code == 303
    langs = await _room_languages(room.id)
    assert not langs["de"].enabled
    assert not langs["de"].tts_enabled


# ── Room sync (Eventyay → VoxBento) ──────────────────────────────────────────


async def _upsert(target_languages, ai_languages, eventyay_room_id="eventyay-7"):
    from portal.database import get_session
    from portal.routers.api_v1 import RoomUpsert, upsert_room

    payload = RoomUpsert(name="Stage", target_languages=target_languages, ai_languages=ai_languages)
    with patch("portal.routers.api_v1._verify_token_rbac", new=AsyncMock()):
        async with get_session() as db:
            return await upsert_room(
                "aicon", eventyay_room_id, payload, db=db, token=SimpleNamespace(id=None, client_id=None)
            )


async def test_room_sync_creates_ai_languages_without_human_booths(ai_room, monkeypatch):
    from portal.database import get_session

    monkeypatch.setattr(settings, "public_base_url", "https://voxbento.example")
    result = await _upsert(["es"], ["de", "fr", "es"])
    room_id = result["room_id"]

    # es is a human booth, so it is not an AI language even though it was sent in both lists.
    assert [b["language"] for b in result["booths"]] == ["es"]
    assert result["ai_booths"] == [
        {
            "language": "de",
            "booth_id": f"aicon-{room_id}-ai-de",
            "tts_ws_url": f"wss://voxbento.example/ws/tts/aicon-{room_id}-ai-de",
        },
        {
            "language": "fr",
            "booth_id": f"aicon-{room_id}-ai-fr",
            "tts_ws_url": f"wss://voxbento.example/ws/tts/aicon-{room_id}-ai-fr",
        },
    ]
    assert result["tts_ready"] is False  # floor translation and TTS are not set up yet

    langs = await _room_languages(room_id)
    assert {code: (lang.enabled, lang.tts_enabled) for code, lang in langs.items()} == {
        "es": (True, False),
        "de": (True, True),
        "fr": (True, True),
    }
    assert langs["de"].language_name == "German"
    async with get_session() as s:
        booth_codes = set(await s.scalars(select(DBBooth.language_code).where(DBBooth.room_id == room_id)))
    assert booth_codes == {"es"}


async def test_room_sync_moves_languages_between_human_and_ai(ai_room):
    from portal.database import get_session

    room_id = (await _upsert(["es"], ["de", "fr"]))["room_id"]

    # de becomes a human booth, fr stays AI.
    result = await _upsert(["es", "de"], ["fr"])
    assert [b["language"] for b in result["ai_booths"]] == ["fr"]
    langs = await _room_languages(room_id)
    assert not langs["de"].tts_enabled
    assert langs["fr"].tts_enabled
    async with get_session() as s:
        booth_codes = set(await s.scalars(select(DBBooth.language_code).where(DBBooth.room_id == room_id)))
    assert booth_codes == {"es", "de"}

    # Dropping fr from the sync removes it.
    result = await _upsert(["es", "de"], [])
    assert result["ai_booths"] == []
    assert set(await _room_languages(room_id)) == {"es", "de"}


async def test_room_sync_without_ai_languages_keeps_old_behaviour(ai_room):
    """Older plugin versions don't send ai_languages; every language is a human booth."""
    result = await _upsert(["es", "de"], [])
    assert sorted(b["language"] for b in result["booths"]) == ["de", "es"]
    assert result["ai_booths"] == []
    langs = await _room_languages(result["room_id"])
    assert not any(lang.tts_enabled for lang in langs.values())


async def test_room_sync_partial_updates_keep_the_list_they_omit(ai_room):
    """Room sync is a partial update: a language list left out of the payload is kept."""
    from portal.database import get_session
    from portal.routers.api_v1 import RoomUpsert, upsert_room

    async def _partial(**fields):
        with patch("portal.routers.api_v1._verify_token_rbac", new=AsyncMock()):
            async with get_session() as db:
                return await upsert_room(
                    "aicon", "eventyay-7", RoomUpsert(**fields), db=db, token=SimpleNamespace(id=None, client_id=None)
                )

    async def _booth_codes(room_id):
        async with get_session() as s:
            return set(await s.scalars(select(DBBooth.language_code).where(DBBooth.room_id == room_id)))

    room_id = (await _upsert(["es"], ["de", "fr"]))["room_id"]

    # Omitting ai_languages keeps the AI languages while the human booths change.
    result = await _partial(target_languages=["es", "it"])
    assert [b["language"] for b in result["ai_booths"]] == ["de", "fr"]
    assert await _booth_codes(room_id) == {"es", "it"}

    # Sending only ai_languages leaves the human booths alone; a dropped AI
    # language keeps its translation row but loses its TTS flag.
    result = await _partial(ai_languages=["de"])
    assert [b["language"] for b in result["ai_booths"]] == ["de"]
    assert await _booth_codes(room_id) == {"es", "it"}
    langs = await _room_languages(room_id)
    assert langs["de"].tts_enabled
    assert not langs["fr"].tts_enabled

    # A payload with neither list touches no languages at all.
    result = await _partial(name="Main Stage")
    assert [b["language"] for b in result["ai_booths"]] == ["de"]
    assert await _booth_codes(room_id) == {"es", "it"}


async def test_room_sync_repairs_code_only_language_names(ai_room):
    """Earlier syncs stored "de" as the name, which made the AI booth label "de (AI)"."""
    from portal.database import get_session

    room_id = (await _upsert(["es"], ["de"]))["room_id"]
    async with get_session() as s:
        row = await s.scalar(
            select(RoomTranslationLanguage).where(
                RoomTranslationLanguage.room_id == room_id, RoomTranslationLanguage.language_code == "de"
            )
        )
        row.language_name = "de"

    await _upsert(["es"], ["de"])
    assert (await _room_languages(room_id))["de"].language_name == "German"


async def test_room_sync_normalizes_and_validates_language_codes(ai_room):
    from pydantic import ValidationError

    from portal.routers.api_v1 import RoomUpsert

    payload = RoomUpsert(name="Stage", target_languages=[" ES "], ai_languages=["DE", "de", "Es"])
    assert payload.target_languages == ["es"]
    assert payload.ai_languages == ["de", "es"]

    # Upper case can't sneak an AI booth past the human "es" booth.
    result = await _upsert([" ES "], ["DE", "Es"])
    assert [b["language"] for b in result["ai_booths"]] == ["de"]

    for bad in (["floor"], ["de/x"], ["xx"], ["deu"]):
        with pytest.raises(ValidationError):
            RoomUpsert(name="Stage", ai_languages=bad)


async def test_tts_ready_needs_a_translation_setup_and_ai_languages(ai_room):
    from portal.database import get_session

    event, room = ai_room
    async with get_session() as s:
        r = await s.get(Room, room.id)
        r.eventyay_room_id = "eventyay-7"
        r.floor_translation_provider = "local"
        r.floor_translation_model = "nllb-200-distilled-600M"

    assert (await _upsert(["es"], ["de"]))["tts_ready"] is True
    assert (await _upsert(["es"], []))["tts_ready"] is False  # no AI language to speak

    async with get_session() as s:
        (await s.get(Room, room.id)).floor_translation_model = None
    assert (await _upsert(["es"], ["de"]))["tts_ready"] is False
