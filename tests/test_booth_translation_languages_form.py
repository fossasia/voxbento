"""The booth translation form saves each target language once, as a known lowercase code."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.auth import create_user_token
from portal.crypto import encrypt_val
from portal.database import configure, create_booth, create_event, create_room, dispose, get_session, init_db
from portal.models import BoothTranslationLanguage, User

pytestmark = pytest.mark.usefixtures("auto_csrf")

ORIGIN = {"origin": "http://test"}
UNRECOGNISED = "One of the chosen translation languages isn't recognised."


@pytest.fixture
async def admin_booth():
    """An Admin cookie and a French booth in a room with a hosted translation key."""
    configure("sqlite+aiosqlite://")
    await init_db()
    async with get_session() as s:
        admin = User(email="booth-langs@example.com", display_name="Admin", is_admin=True, email_verified=True)
        s.add(admin)
        event = await create_event(s, slug="langcon", display_name="LangCon")
        event.encrypted_translation_openai_api_key = encrypt_val("sk-test")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        booth = await create_booth(s, event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
        await s.flush()
        cookie = {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)}
        ids = (event.id, room.id, booth.id)
    yield cookie, *ids
    await dispose()


async def _save(cookie, event_id, room_id, booth_id, languages, *, accept="application/json", enabled=True):
    data = {
        "translation_enabled": "true" if enabled else "false",
        "translation_provider": "openai",
        "translation_model": "gpt-4o-mini",
        "translation_languages": languages,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookie) as client:
        return await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/translation-settings",
            data=data,
            headers={**ORIGIN, "accept": accept},
        )


async def _languages(booth_id) -> list[tuple[str, str, bool]]:
    async with get_session() as s:
        rows = await s.scalars(
            select(BoothTranslationLanguage).where(BoothTranslationLanguage.booth_id == booth_id)
        )
        return sorted((row.language_code, row.language_name, row.enabled) for row in rows)


@pytest.mark.anyio
@pytest.mark.parametrize("languages", [["es", "es"], ["es", "ES"], ["ES", " es "]])
async def test_a_language_sent_twice_is_saved_once(admin_booth, languages):
    cookie, event_id, room_id, booth_id = admin_booth

    res = await _save(cookie, event_id, room_id, booth_id, languages)

    assert res.status_code == 303
    assert await _languages(booth_id) == [("es", "Spanish", True)]


@pytest.mark.anyio
async def test_a_language_saved_again_in_another_case_reuses_its_row(admin_booth):
    cookie, event_id, room_id, booth_id = admin_booth
    await _save(cookie, event_id, room_id, booth_id, ["es", "de"])

    res = await _save(cookie, event_id, room_id, booth_id, ["DE"])

    assert res.status_code == 303
    assert await _languages(booth_id) == [("de", "German", True), ("es", "Spanish", False)]


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["zz", "english", "pt-BR", "x" * 30])
async def test_an_unknown_language_is_refused_and_nothing_is_saved(admin_booth, bad):
    cookie, event_id, room_id, booth_id = admin_booth

    res = await _save(cookie, event_id, room_id, booth_id, ["es", bad])

    assert res.status_code == 400
    assert res.json()["detail"] == UNRECOGNISED
    assert await _languages(booth_id) == []


@pytest.mark.anyio
async def test_the_browser_form_shows_the_error(admin_booth):
    cookie, event_id, room_id, booth_id = admin_booth

    res = await _save(cookie, event_id, room_id, booth_id, ["zz"], accept="text/html")

    assert res.status_code == 400
    assert res.headers["content-type"].startswith("text/html")
    assert "isn&#39;t recognised" in res.text or "isn't recognised" in res.text


@pytest.mark.anyio
async def test_empty_values_are_skipped(admin_booth):
    cookie, event_id, room_id, booth_id = admin_booth

    res = await _save(cookie, event_id, room_id, booth_id, ["", "es", "  "])

    assert res.status_code == 303
    assert await _languages(booth_id) == [("es", "Spanish", True)]


async def _add_saved(booth_id, code):
    """Store a target language the way an earlier version of the form could."""
    async with get_session() as s:
        s.add(BoothTranslationLanguage(booth_id=booth_id, language_code=code, language_name=code, enabled=True))


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [True, False])
async def test_a_code_the_booth_already_has_is_kept_as_saved(admin_booth, enabled):
    """The picker sends back saved codes it does not list; the booth must still save."""
    cookie, event_id, room_id, booth_id = admin_booth
    await _add_saved(booth_id, "pt-BR")
    await _add_saved(booth_id, "ES")

    res = await _save(cookie, event_id, room_id, booth_id, ["pt-BR", "ES", "de"], enabled=enabled)

    assert res.status_code == 303
    assert await _languages(booth_id) == [("ES", "ES", True), ("de", "German", True), ("pt-BR", "pt-BR", True)]


@pytest.mark.anyio
async def test_a_saved_code_can_be_removed_and_a_new_unknown_one_is_still_refused(admin_booth):
    cookie, event_id, room_id, booth_id = admin_booth
    await _add_saved(booth_id, "pt-BR")

    refused = await _save(cookie, event_id, room_id, booth_id, ["pt-BR", "zh-TW"])
    removed = await _save(cookie, event_id, room_id, booth_id, ["de"])

    assert refused.status_code == 400
    assert removed.status_code == 303
    assert await _languages(booth_id) == [("de", "German", True), ("pt-BR", "pt-BR", False)]
