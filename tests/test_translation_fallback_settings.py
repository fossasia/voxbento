"""Fallback configuration uses the existing protected room form and encrypted event keys."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.auth import create_user_token
from portal.crypto import encrypt_val
from portal.database import configure, dispose, get_session, init_db
from portal.models import Event, Room, RoomTranslationLanguage, User
from portal.routers.api_v1 import _check_translation_languages

NLLB = "nllb-200-distilled-600M"
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("auto_csrf")]


@pytest.fixture
async def room_form():
    configure("sqlite+aiosqlite://")
    await init_db()
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
    yield cookie, event.id, room.id
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
