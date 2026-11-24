"""Fallback configuration uses the existing protected room form and encrypted event keys."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.auth import create_user_token
from portal.crypto import encrypt_val
from portal.database import configure, dispose, get_session, init_db
from portal.models import BoothTranslationLanguage, DBBooth, Event, Room, RoomTranslationLanguage, User
from portal.routers import admin as admin_routes
from portal.routers.api_v1 import _check_translation_languages, _lock_event_for_request

NLLB = "nllb-200-distilled-600M"
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("auto_csrf")]


@pytest.fixture(autouse=True)
def operator_catalog(monkeypatch):
    # These cases check unavailable services, independently of a developer's local server.
    monkeypatch.setattr(admin_routes.settings, "self_hosted_llm_base_url", "")
    monkeypatch.setattr(admin_routes.settings, "self_hosted_llm_models", "")


async def seed_room_form():
    async with get_session() as session:
        admin = User(email="fallback-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        event = Event(
            slug="fallback-settings",
            display_name="Fallback",
            encrypted_translation_openai_api_key=encrypt_val("private-key"),
        )
        session.add_all([admin, event])
        await session.flush()
        room = Room(
            event_id=event.id,
            display_name="Main Hall",
            floor_language_code="en",
            floor_transcription_enabled=True,
            floor_translation_enabled=True,
            floor_translation_provider="openai",
            floor_translation_model="gpt-4o-mini",
        )
        session.add(room)
        await session.flush()
        session.add(RoomTranslationLanguage(room_id=room.id, language_code="fr", language_name="French", enabled=True))
        cookie = {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)}
    return cookie, event.id, room.id


@pytest.fixture
async def room_form():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield await seed_room_form()
    await dispose()


@pytest.fixture
async def file_room_form(tmp_path):
    configure(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    await init_db()
    yield await seed_room_form()
    await dispose()


async def post(data, room_form, *, cookies=None):
    cookie, event_id, room_id = room_form
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", cookies=cookie if cookies is None else cookies
    ) as client:
        return await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/edit",
            data={"form_section": "ai", **data},
            headers={"origin": "http://test"},
        )


async def read_room(room_id):
    async with get_session() as session:
        return await session.get(Room, room_id)


async def test_room_form_saves_renders_and_disables_fallback(room_form):
    response = await post({"translation_fallback_provider": "local", "translation_fallback_model": NLLB}, room_form)
    assert response.status_code == 303
    _, event_id, room_id = room_form
    room = await read_room(room_id)
    assert (room.translation_fallback_provider, room.translation_fallback_model) == ("local", NLLB)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=room_form[0]) as client:
        page = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")
    assert page.status_code == 200
    assert 'id="translation_fallback_provider"' in page.text
    assert f'data-initial-fallback-model="{NLLB}"' in page.text
    assert "private-key" not in page.text
    response = await post({"translation_fallback_provider": ""}, room_form)
    assert response.status_code == 303
    room = await read_room(room_id)
    assert room.translation_fallback_provider is room.translation_fallback_model is None


@pytest.mark.parametrize(
    "fields",
    [
        {"translation_fallback_provider": "unknown"},
        {"translation_fallback_provider": "gemini"},  # No event key.
        {"translation_fallback_provider": "self_hosted"},  # No server configuration.
        {"translation_fallback_provider": "local", "translation_fallback_model": "unlisted"},
        {"translation_fallback_provider": "local", "translation_fallback_model": "x" * 101},
        {"translation_fallback_provider": "openai", "translation_fallback_model": "gpt-4o-mini"},
    ],
)
async def test_invalid_fallback_does_not_persist_partial_primary_change(room_form, fields):
    response = await post({"floor_ai_interpreter_persona": "should roll back", **fields}, room_form)
    assert response.status_code == 400
    room = await read_room(room_form[2])
    assert room.translation_fallback_provider is None and room.floor_ai_interpreter_persona is None


async def test_unsupported_room_language_is_rejected_on_fallback_save(room_form):
    async with get_session() as session:
        session.add(
            RoomTranslationLanguage(room_id=room_form[2], language_code="az", language_name="Azerbaijani", enabled=True)
        )
    response = await post({"translation_fallback_provider": "local"}, room_form)
    assert response.status_code == 400 and "Azerbaijani" in response.text
    assert (await read_room(room_form[2])).translation_fallback_provider is None


async def test_omitted_fallback_fields_preserve_previous_choice(room_form):
    await post({"translation_fallback_provider": "local"}, room_form)
    response = await post({"floor_ai_interpreter_persona": "An open source conference"}, room_form)
    assert response.status_code == 303
    assert (await read_room(room_form[2])).translation_fallback_provider == "local"


async def test_anonymous_request_cannot_change_fallback(room_form):
    response = await post({"translation_fallback_provider": "local"}, room_form, cookies={})
    assert response.status_code == 403
    assert (await read_room(room_form[2])).translation_fallback_provider is None


async def test_api_language_update_preserves_fallback_compatibility(room_form):
    await post({"translation_fallback_provider": "local"}, room_form)
    async with get_session() as session:
        room = await session.get(Room, room_form[2])
        with pytest.raises(HTTPException) as error:
            await _check_translation_languages(session, {"target_languages": ["fr", "az"]}, room)
        assert error.value.status_code == 422 and "Azerbaijani" in error.value.detail
        with pytest.raises(HTTPException):
            await _check_translation_languages(
                session, {"translation_provider": "local", "translation_model": NLLB}, room
            )
    assert (await read_room(room_form[2])).floor_translation_provider == "openai"


async def test_fallback_can_be_disabled_after_translation_and_captions_are_off(room_form):
    await post({"translation_fallback_provider": "local"}, room_form)
    async with get_session() as session:
        room = await session.get(Room, room_form[2])
        room.floor_translation_enabled = False
        room.floor_transcription_enabled = False
    cookie, event_id, room_id = room_form
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookie) as client:
        response = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")
    assert response.status_code == 200 and 'id="translation_fallback_provider"' in response.text
    assert (await post({"translation_fallback_provider": ""}, room_form)).status_code == 303
    assert (await read_room(room_id)).translation_fallback_provider is None


async def test_saved_custom_fallback_model_is_selected_without_javascript(room_form):
    response = await post(
        {"translation_fallback_provider": "openai", "translation_fallback_model": "my-custom-backup"}, room_form
    )
    assert response.status_code == 303
    cookie, event_id, room_id = room_form
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookie) as client:
        page = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")
    assert '<option value="my-custom-backup" selected>my-custom-backup</option>' in page.text


async def add_booth(room_form, *, source="en", target="fr", enabled=True, provider="openai", model="gpt-4o-mini"):
    async with get_session() as session:
        booth = DBBooth(
            event_id=room_form[1],
            room_id=room_form[2],
            language_code=source,
            language_name=source,
            translation_enabled=enabled,
            translation_provider=provider,
            translation_model=model,
        )
        session.add(booth)
        await session.flush()
        session.add(
            BoothTranslationLanguage(booth_id=booth.id, language_code=target, language_name=target, enabled=True)
        )
        return booth.id


@pytest.mark.parametrize("booth_source,status", [("en", 303), ("az", 400)])
async def test_disabled_floor_does_not_block_valid_booth_fallback_settings(room_form, booth_source, status):
    async with get_session() as session:
        room = await session.get(Room, room_form[2])
        room.floor_translation_enabled = False
        room.floor_language_code = "az"  # Unsupported by NLLB, but unused while the floor is disabled.
    await add_booth(room_form, source=booth_source)
    response = await post({"translation_fallback_provider": "local", "translation_fallback_model": NLLB}, room_form)
    assert response.status_code == status
    room = await read_room(room_form[2])
    assert room.translation_fallback_provider == ("local" if status == 303 else None)


@pytest.mark.parametrize("source,target", [("az", "fr"), ("en", "az")])
async def test_fallback_save_validates_enabled_booths(room_form, source, target):
    await add_booth(room_form, source=source, target=target)
    response = await post({"translation_fallback_provider": "local"}, room_form)
    assert response.status_code == 400 and "Azerbaijani" in response.text
    assert (await read_room(room_form[2])).translation_fallback_provider is None


async def test_fallback_save_rejects_matching_booth_translator(room_form):
    await add_booth(room_form, provider="local", model=NLLB)
    response = await post({"translation_fallback_provider": "local"}, room_form)
    assert response.status_code == 400 and "must be different" in response.text


async def test_booth_translation_update_preserves_fallback_compatibility(room_form):
    booth_id = await add_booth(room_form, source="az", enabled=False)
    assert (await post({"translation_fallback_provider": "local"}, room_form)).status_code == 303
    cookie, event_id, room_id = room_form
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookie) as client:
        response = await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/translation-settings",
            data={
                "translation_enabled": "on",
                "translation_provider": "openai",
                "translation_model": "gpt-4o-mini",
                "translation_languages": "fr",
            },
            headers={"origin": "http://test"},
        )
    assert response.status_code == 400
    async with get_session() as session:
        assert not (await session.get(DBBooth, booth_id)).translation_enabled


async def test_booth_source_update_rolls_back_if_fallback_cannot_translate_it(room_form):
    booth_id = await add_booth(room_form)
    await post({"translation_fallback_provider": "local"}, room_form)
    cookie, event_id, room_id = room_form
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookie) as client:
        response = await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/edit",
            data={"language_code": "az"},
            headers={"origin": "http://test"},
        )
    assert response.status_code == 400
    async with get_session() as session:
        assert (await session.get(DBBooth, booth_id)).language_code == "en"


async def test_disabled_api_update_cannot_store_a_new_identical_primary(room_form):
    await post({"translation_fallback_provider": "local"}, room_form)
    async with get_session() as session:
        room = await session.get(Room, room_form[2])
        with pytest.raises(HTTPException) as error:
            await _check_translation_languages(
                session, {"enable_translation": False, "translation_provider": "local", "translation_model": NLLB}, room
            )
        assert error.value.status_code == 422
        await _check_translation_languages(session, {"enable_translation": False}, room)


async def test_rejected_setup_save_rolls_back_primary_and_room_name(room_form):
    await post({"translation_fallback_provider": "local"}, room_form)
    cookie, event_id, room_id = room_form
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookie) as client:
        response = await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/setup",
            data={
                "display_name": "Should roll back",
                "spoken_language": "en",
                "languages": "fr",
                "delivery_fr": "ai_text",
                "speech_engine": "local",
                "translation_engine": "local",
                "voice_engine": "",
            },
            headers={"origin": "http://test"},
        )
    assert response.status_code == 400
    room = await read_room(room_id)
    assert room.display_name == "Main Hall"
    assert (room.floor_translation_provider, room.floor_translation_model) == ("openai", "gpt-4o-mini")


@pytest.mark.parametrize("writer", ["room form", "API"])
async def test_concurrent_settings_writers_validate_after_the_fallback_save(file_room_form, monkeypatch, writer):
    form = file_room_form
    acquired, waiting, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = admin_routes.lock_event_translation_settings
    calls = 0

    async def guard(session, event_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            await original(session, event_id)
            acquired.set()
            await release.wait()
        else:
            waiting.set()
            await original(session, event_id)

    monkeypatch.setattr(admin_routes, "lock_event_translation_settings", guard)
    first = asyncio.create_task(post({"translation_fallback_provider": "local"}, form))
    await asyncio.wait_for(acquired.wait(), 2)

    async def api_update():
        try:
            async with get_session() as session:
                event = await session.get(Event, form[1])
                waiting.set()
                await _lock_event_for_request(session, event, write_lock=True)
                room = await session.get(Room, form[2])
                await _check_translation_languages(
                    session, {"translation_provider": "local", "translation_model": NLLB}, room
                )
                room.floor_translation_provider = "local"
                room.floor_translation_model = NLLB
            return 200
        except HTTPException as error:
            return error.status_code

    second = asyncio.create_task(
        api_update()
        if writer == "API"
        else post({"floor_translation_provider": "local", "floor_translation_model": NLLB}, form)
    )
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        await asyncio.sleep(0.05)
        assert not second.done()
    finally:
        release.set()
        first_result, second_result = await asyncio.gather(first, second)
    assert first_result.status_code == 303
    assert (second_result if writer == "API" else second_result.status_code) == (422 if writer == "API" else 400)
    room = await read_room(form[2])
    assert (room.floor_translation_provider, room.floor_translation_model) == ("openai", "gpt-4o-mini")
    assert (room.translation_fallback_provider, room.translation_fallback_model) == ("local", NLLB)
