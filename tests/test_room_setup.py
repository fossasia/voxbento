"""Guided room setup: one form for a room's language, listener languages, captions and AI."""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from starlette.datastructures import FormData

from fastapi_app import app
from portal import room_setup
from portal.auth import create_user_token
from portal.config import settings
from portal.crypto import encrypt_val
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_invite_token,
    create_room,
    dispose,
    get_session,
    init_db,
    list_booths_for_room,
    set_event_membership,
    set_room_membership,
)
from portal.models import Event, InviteToken, Room, RoomTranslationLanguage, User
from portal.roles import EVENT_OWNER, ROOM_COORDINATOR
from portal.room_setup import (
    AI,
    BOTH,
    INTERPRETER,
    MODE_AI,
    MODE_BOTH,
    MODE_INTERPRETERS,
    engine_catalog,
    parse_plan,
)
from portal.routers import admin

ORIGIN = {"origin": "http://test"}


@pytest.fixture(autouse=True)
def installed_optional_engines(monkeypatch):
    monkeypatch.setattr(room_setup, "find_spec", lambda name: object())
    monkeypatch.setattr(settings, "nvidia_function_id", "test-function")


@pytest.fixture
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def admin_cookie(setup_db):
    async with get_session() as s:
        admin = User(email="setup-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        s.add(admin)
        await s.flush()
        admin_id = admin.id
    return {"user_token": create_user_token(user_id=admin_id, email="setup-admin@example.com", is_admin=True)}


@pytest.fixture
async def event_room(setup_db):
    async with get_session() as s:
        event = await create_event(s, slug="setupcon", display_name="SetupCon")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        await create_booth(s, event_id=event.id, room_id=room.id, language_code="fr", language_name="French")
    return event, room


def _form(**fields: object) -> FormData:
    items: list[tuple[str, str]] = []
    for key, value in fields.items():
        for v in value if isinstance(value, list) else [value]:
            items.append((key, str(v)))
    return FormData(items)


def _event(**keys: object) -> Event:
    return Event(slug="x", display_name="X", transcription_api_enabled=False, **keys)


async def _room(room_id: int) -> Room:
    async with get_session() as s:
        return await s.get(Room, room_id)


async def _ai_codes(room_id: int) -> set[str]:
    async with get_session() as s:
        rows = await s.scalars(
            select(RoomTranslationLanguage.language_code).where(
                RoomTranslationLanguage.room_id == room_id, RoomTranslationLanguage.enabled.is_(True)
            )
        )
        return set(rows)


async def _booth_codes(room_id: int) -> list[str]:
    async with get_session() as s:
        return [b.language_code for b in await list_booths_for_room(s, room_id)]


# ── Engines ────────────────────────────────────────────────────────────────


def test_without_keys_only_self_hosted_engines_are_offered():
    catalog = engine_catalog(_event())
    assert [e.provider for e in catalog.speech] == ["local"]
    assert [e.provider for e in catalog.translation] == ["local"]
    assert [e.provider for e in catalog.voice] == ["supertonic"]
    assert catalog.locked == (
        "Deepgram",
        "ElevenLabs",
        "OpenAI",
        "NVIDIA",
        "Anthropic",
        "Google Gemini",
        "Groq",
        "OpenRouter",
    )


def test_engines_with_keys_are_offered_first():
    key = encrypt_val("sk-test")
    event = _event(encrypted_deepgram_api_key=key, encrypted_translation_openai_api_key=key)
    event.transcription_api_enabled = True
    catalog = engine_catalog(event)
    assert catalog.speech[0].provider == "deepgram"
    assert catalog.translation[0].provider == "openai"
    assert catalog.voice[0].provider == "deepgram"
    # OpenAI still appears: the captions key is separate from the translation key.
    assert "Deepgram" not in catalog.locked and "OpenAI" in catalog.locked


def test_cloud_captions_need_the_event_transcription_switch():
    catalog = engine_catalog(_event(encrypted_deepgram_api_key=encrypt_val("k")))
    assert [e.provider for e in catalog.speech] == ["local"]
    # The voice reads the same key directly, so it is available.
    assert catalog.voice[0].provider == "deepgram"


def test_missing_optional_dependencies_are_not_offered(monkeypatch):
    monkeypatch.setattr(room_setup, "find_spec", lambda name: None)
    event = _event(encrypted_nvidia_api_key=encrypt_val("k"))
    event.transcription_api_enabled = True
    catalog = engine_catalog(event)
    assert "nvidia" not in {engine.provider for engine in catalog.speech}
    assert catalog.voice == ()
    assert not room_setup.blank_plan(catalog).ai_voice
    assert any("supertonic extra" in reason for reason in catalog.unavailable)


def test_nvidia_requires_its_function_id(monkeypatch):
    monkeypatch.setattr(settings, "nvidia_function_id", "")
    event = _event(encrypted_nvidia_api_key=encrypt_val("k"))
    event.transcription_api_enabled = True
    assert "nvidia" not in {engine.provider for engine in engine_catalog(event).speech}


# ── Reading the form ───────────────────────────────────────────────────────


def _parse(**fields: object):
    base: dict[str, object] = {
        "display_name": "Main Hall",
        "spoken_language": "en",
        "speech_engine": "local",
        "translation_engine": "local",
        "voice_engine": "supertonic",
    }
    base.update(fields)
    return parse_plan(_form(**base), engine_catalog(_event()))


def test_mode_applies_to_every_language_unless_both():
    plan, errors = _parse(mode=MODE_INTERPRETERS, languages=["es", "fr"], delivery_es=AI)
    assert not errors
    assert plan.languages == (("es", INTERPRETER), ("fr", INTERPRETER))

    plan, _ = _parse(mode=MODE_AI, languages=["es"])
    assert plan.languages == (("es", AI),)

    plan, _ = _parse(mode=MODE_BOTH, languages=["es", "fr", "de"], delivery_es=INTERPRETER, delivery_fr=AI)
    assert plan.languages == (("es", INTERPRETER), ("fr", AI), ("de", BOTH))


def test_the_rooms_own_language_and_duplicates_are_dropped():
    plan, errors = _parse(mode=MODE_INTERPRETERS, languages=["en", "es", "es", ""])
    assert not errors
    assert plan.languages == (("es", INTERPRETER),)


def test_ai_turns_captions_on():
    plan, errors = _parse(mode=MODE_AI, languages=["es"])
    assert not errors
    assert plan.captions is True


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"display_name": "", "mode": MODE_INTERPRETERS, "languages": ["es"]}, "Give the room a name."),
        ({"mode": "", "languages": ["es"]}, "Choose how the room will be interpreted"),
        ({"mode": MODE_INTERPRETERS, "languages": ["xx"]}, "isn't recognised"),
        ({"mode": MODE_INTERPRETERS}, "Choose at least one listener language, or turn on captions."),
        ({"mode": MODE_AI, "languages": ["es"], "translation_engine": "openai"}, "Choose a translation service"),
        ({"spoken_language": "zz", "mode": MODE_INTERPRETERS, "languages": ["es"]}, "language spoken in the room"),
    ],
)
def test_problems_are_explained(fields, message):
    _, errors = _parse(**fields)
    assert any(message in e for e in errors), errors


def test_captions_only_room_is_allowed():
    plan, errors = _parse(mode=MODE_INTERPRETERS, captions="on")
    assert not errors
    assert plan.languages == () and plan.captions


def test_unsupported_local_translation_is_explained():
    _, errors = _parse(mode=MODE_AI, languages=["az"], ai_output="captions")
    assert any("Local translation doesn't support Azerbaijani" in error for error in errors)


def test_unsupported_ai_voice_can_use_captions_instead():
    _, errors = _parse(mode=MODE_AI, languages=["th"], ai_output="voice")
    assert any("AI voice doesn't support Thai" in error for error in errors)
    _, errors = _parse(mode=MODE_AI, languages=["th"], ai_output="captions")
    assert not errors


@pytest.mark.anyio
async def test_saving_legacy_spoken_language_booth_keeps_its_invites(admin_cookie, event_room):
    event, room = event_room
    async with get_session() as session:
        booth = await create_booth(
            session, event_id=event.id, room_id=room.id, language_code="en", language_name="English"
        )
        invite = await create_invite_token(session, booth_id=booth.id, role=INTERPRETER)
        token = invite.token
    async with _client() as client:
        response = await client.post(
            f"/admin/events/{event.id}/rooms/{room.id}/setup",
            data={
                "display_name": "Renamed",
                "spoken_language": "en",
                "mode": MODE_INTERPRETERS,
                "languages": ["en", "fr"],
            },
            headers=ORIGIN,
            cookies=admin_cookie,
        )
    assert response.status_code == 303
    assert await _booth_codes(room.id) == ["en", "fr"]
    async with get_session() as session:
        assert await session.get(InviteToken, token) is not None


@pytest.mark.anyio
async def test_captions_only_setup_can_be_saved_again(admin_cookie, event_room):
    event, room = event_room
    async with get_session() as session:
        db_room = await session.get(Room, room.id)
        db_room.floor_transcription_enabled = True
        for booth in await list_booths_for_room(session, room.id):
            await session.delete(booth)
        snapshot = await room_setup.load_snapshot(session, db_room, engine_catalog(event))
    assert snapshot.plan.mode == MODE_INTERPRETERS


@pytest.mark.anyio
async def test_workspace_setup_is_owner_only(event_room):
    event, room = event_room
    async with get_session() as session:
        owner = User(email="owner@example.com", display_name="Owner", email_verified=True)
        coordinator = User(email="coordinator@example.com", display_name="Coordinator", email_verified=True)
        session.add_all([owner, coordinator])
        await session.flush()
        await set_event_membership(session, user_id=owner.id, event_id=event.id, role=EVENT_OWNER)
        await set_room_membership(session, user_id=coordinator.id, room_id=room.id, role=ROOM_COORDINATOR)
        owner_cookie = {"user_token": create_user_token(user_id=owner.id, email=owner.email)}
        coordinator_cookie = {"user_token": create_user_token(user_id=coordinator.id, email=coordinator.email)}
    data = {"display_name": "Owner's room", "spoken_language": "en", "mode": MODE_INTERPRETERS, "languages": ["es"]}
    async with _client() as client:
        for path in (f"/workspace/events/{event.id}/rooms/new", f"/workspace/events/{event.id}/rooms/{room.id}/setup"):
            assert (await client.get(path, cookies=coordinator_cookie)).status_code == 403
            assert (await client.post(path, cookies=coordinator_cookie, data=data, headers=ORIGIN)).status_code == 403
            assert (await client.get(path, cookies=owner_cookie)).status_code == 200
            assert (
                await client.post(
                    path, cookies=owner_cookie, data=data, headers={"origin": "https://elsewhere.example"}
                )
            ).status_code == 403
            response = await client.post(path, cookies=owner_cookie, data=data, headers=ORIGIN)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/workspace/")


# ── Pages and saving ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_add_room_page_asks_four_questions_without_inline_code(admin_cookie, event_room):
    event, _ = event_room
    async with _client() as c:
        resp = await c.get(f"/admin/events/{event.id}/rooms/new", cookies=admin_cookie)
    assert resp.status_code == 200
    html = resp.text
    for heading in ("The room", "Languages for listeners", "Captions &amp; AI", "Review"):
        assert heading in html
    assert "How will this room be interpreted?" in html
    assert 'value="interpreters"' in html and 'value="ai"' in html and 'value="both"' in html
    assert "Whisper, self-hosted" in html and "OpenAI" not in re.sub(r"also use[^<]*", "", html)
    assert not re.search(r"\sstyle\s*=", html, re.IGNORECASE)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)


@pytest.mark.anyio
async def test_creating_a_mixed_room(admin_cookie, event_room):
    event, _ = event_room
    data = {
        "display_name": "Track B",
        "spoken_language": "de",
        "mode": MODE_BOTH,
        "languages": ["es", "fr", "ja"],
        "delivery_es": INTERPRETER,
        "delivery_fr": BOTH,
        "delivery_ja": AI,
        "ai_output": "voice",
        "speech_engine": "local",
        "translation_engine": "local",
        "voice_engine": "supertonic",
    }
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{event.id}/rooms/new",
            data=data,
            headers=ORIGIN,
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        match = re.search(r"/rooms/(\d+)/\?created=1$", resp.headers["location"])
        assert match, resp.headers["location"]
        room_id = int(match.group(1))
        page = await c.get(resp.headers["location"], cookies=admin_cookie)

    room = await _room(room_id)
    assert room.display_name == "Track B"
    assert room.floor_language_code == "de"
    assert room.jitsi_url and "Voxbento-setupcon-TrackB" in room.jitsi_url
    assert await _booth_codes(room_id) == ["es", "fr"]
    assert await _ai_codes(room_id) == {"fr", "ja"}
    assert room.floor_transcription_enabled and room.floor_transcription_provider == "local"
    assert room.floor_translation_enabled and room.floor_translation_provider == "local"
    assert room.floor_translation_model == "nllb-200-distilled-600M"
    assert room.floor_tts_enabled and room.floor_tts_provider == "supertonic"

    assert "Here&#39;s what&#39;s next" in page.text or "Here's what's next" in page.text
    assert "Interpreter + AI" in page.text and "Change languages &amp; captions" in page.text


@pytest.mark.anyio
async def test_interpreters_only_room_without_captions(admin_cookie, event_room):
    event, _ = event_room
    data = {"display_name": "Hall C", "spoken_language": "en", "mode": MODE_INTERPRETERS, "languages": ["es"]}
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{event.id}/rooms/new",
            data=data,
            headers=ORIGIN,
            cookies=admin_cookie,
            follow_redirects=False,
        )
    room_id = int(re.search(r"/rooms/(\d+)/", resp.headers["location"]).group(1))
    room = await _room(room_id)
    assert await _booth_codes(room_id) == ["es"]
    assert not room.floor_transcription_enabled
    assert not room.floor_translation_enabled
    assert not room.floor_tts_enabled


@pytest.mark.anyio
async def test_local_translation_stores_names_the_provider_recognizes(admin_cookie, event_room):
    event, _ = event_room
    async with _client() as client:
        response = await client.post(
            f"/admin/events/{event.id}/rooms/new",
            data={
                "display_name": "Local languages",
                "spoken_language": "en",
                "mode": MODE_AI,
                "languages": ["bn", "oc"],
                "ai_output": "captions",
                "speech_engine": "local",
                "translation_engine": "local",
            },
            headers=ORIGIN,
            cookies=admin_cookie,
        )
    assert response.status_code == 303
    room_id = int(re.search(r"/rooms/(\d+)/", response.headers["location"]).group(1))
    async with get_session() as session:
        rows = await session.scalars(select(RoomTranslationLanguage).where(RoomTranslationLanguage.room_id == room_id))
        assert {row.language_code: row.language_name for row in rows} == {
            "bn": "Bengali",
            "oc": "Occitan (post 1500)",
        }


@pytest.mark.anyio
async def test_rejected_form_is_shown_again_with_answers(admin_cookie, event_room):
    event, _ = event_room
    data = {"display_name": "Hall D", "spoken_language": "en", "languages": ["es"]}
    async with _client() as c:
        resp = await c.post(f"/admin/events/{event.id}/rooms/new", data=data, headers=ORIGIN, cookies=admin_cookie)
    assert resp.status_code == 400
    assert "Choose how the room will be interpreted" in resp.text
    assert 'value="Hall D"' in resp.text
    assert re.search(r'value="es"\s+checked', resp.text)


@pytest.mark.anyio
async def test_creating_requires_same_origin(admin_cookie, event_room):
    event, _ = event_room
    data = {"display_name": "Hall E", "spoken_language": "en", "mode": MODE_INTERPRETERS, "languages": ["es"]}
    async with _client() as c:
        resp = await c.post(f"/admin/events/{event.id}/rooms/new", data=data, cookies=admin_cookie)
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_wizard_returns_to_the_rooms_step(admin_cookie, event_room):
    event, _ = event_room
    data = {
        "from": "setup",
        "display_name": "Hall F",
        "spoken_language": "en",
        "mode": MODE_INTERPRETERS,
        "languages": ["es"],
    }
    async with _client() as c:
        form = await c.get(f"/admin/events/{event.id}/rooms/new?from=setup", cookies=admin_cookie)
        resp = await c.post(
            f"/admin/events/{event.id}/rooms/new",
            data=data,
            headers=ORIGIN,
            cookies=admin_cookie,
            follow_redirects=False,
        )
        rooms_step = await c.get(f"/admin/events/{event.id}/setup/rooms", cookies=admin_cookie)
    assert "Rooms &amp; languages" in form.text and 'name="from" value="setup"' in form.text
    assert resp.headers["location"].endswith(f"/events/{event.id}/setup/rooms")
    assert "Hall F" in rooms_step.text and "Spanish" in rooms_step.text


@pytest.mark.anyio
async def test_old_booths_step_lands_on_rooms(admin_cookie, event_room):
    event, _ = event_room
    async with _client() as c:
        resp = await c.get(f"/admin/events/{event.id}/setup/booths", cookies=admin_cookie, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"/events/{event.id}/setup/rooms")


@pytest.mark.anyio
async def test_legacy_add_room_form_still_saves(admin_cookie, event_room):
    event, _ = event_room
    async with _client() as client:
        response = await client.post(
            f"/admin/events/{event.id}/setup/rooms",
            data={"display_name": "Legacy form room"},
            cookies=admin_cookie,
        )
        assert response.status_code == 303
        page = await client.get(response.headers["location"], cookies=admin_cookie)
    assert "Legacy form room" in page.text


@pytest.mark.anyio
async def test_editing_opens_with_the_rooms_current_setup(admin_cookie, event_room):
    event, room = event_room
    async with _client() as c:
        resp = await c.get(f"/admin/events/{event.id}/rooms/{room.id}/setup", cookies=admin_cookie)
    assert resp.status_code == 200
    assert 'value="Main Hall"' in resp.text
    assert re.search(r'value="fr"\s+checked', resp.text)
    assert re.search(r'value="interpreters" required\s+checked', resp.text)
    assert "Save changes" in resp.text


@pytest.mark.anyio
async def test_editing_changes_booths_but_keeps_live_ones(admin_cookie, event_room, monkeypatch):
    event, room = event_room
    async with get_session() as s:
        await create_booth(s, event_id=event.id, room_id=room.id, language_code="de", language_name="German")
    monkeypatch.setattr(admin, "_live_booth_codes", lambda slug, room_id, codes: frozenset({"de"}))
    data = {"display_name": "Main Hall", "spoken_language": "en", "mode": MODE_INTERPRETERS, "languages": ["es"]}
    async with _client() as c:
        resp = await c.post(
            f"/admin/events/{event.id}/rooms/{room.id}/setup",
            data=data,
            headers=ORIGIN,
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "updated=1" in resp.headers["location"] and "kept_live=de" in resp.headers["location"]
        page = await c.get(resp.headers["location"], cookies=admin_cookie)
    assert await _booth_codes(room.id) == ["de", "es"]
    assert "German booth is live right now" in page.text


@pytest.mark.anyio
async def test_editing_keeps_a_fine_tuned_model_for_the_same_provider(admin_cookie, event_room):
    event, room = event_room
    async with get_session() as s:
        db_room = await s.get(Room, room.id)
        db_room.floor_translation_provider = "local"
        db_room.floor_translation_model = "custom-local-model"
    data = {
        "display_name": "Main Hall",
        "spoken_language": "en",
        "mode": MODE_AI,
        "languages": ["es"],
        "ai_output": "captions",
        "speech_engine": "local",
        "translation_engine": "local",
    }
    async with _client() as c:
        await c.post(f"/admin/events/{event.id}/rooms/{room.id}/setup", data=data, headers=ORIGIN, cookies=admin_cookie)
    db_room = await _room(room.id)
    assert db_room.floor_translation_model == "custom-local-model"
    assert db_room.floor_translation_enabled and not db_room.floor_tts_enabled
    assert await _booth_codes(room.id) == []
    assert await _ai_codes(room.id) == {"es"}


@pytest.mark.anyio
async def test_cannot_set_up_another_events_room(admin_cookie, event_room):
    event, _ = event_room
    async with get_session() as s:
        other = await create_event(s, slug="othercon", display_name="Other")
        other_room = await create_room(s, event_id=other.id, display_name="Elsewhere")
    data = {"display_name": "Taken", "spoken_language": "en", "mode": MODE_INTERPRETERS, "languages": ["es"]}
    async with _client() as c:
        page = await c.get(f"/admin/events/{event.id}/rooms/{other_room.id}/setup", cookies=admin_cookie)
        resp = await c.post(
            f"/admin/events/{event.id}/rooms/{other_room.id}/setup", data=data, headers=ORIGIN, cookies=admin_cookie
        )
    assert page.status_code == 404 and resp.status_code == 404
    assert await _booth_codes(other_room.id) == []
    assert (await _room(other_room.id)).display_name == "Elsewhere"


@pytest.mark.anyio
async def test_room_lists_describe_each_rooms_languages(admin_cookie, event_room):
    event, _ = event_room
    async with _client() as c:
        rooms = await c.get(f"/admin/events/{event.id}/rooms/", cookies=admin_cookie)
        detail = await c.get(f"/admin/events/{event.id}/", cookies=admin_cookie)
    for resp in (rooms, detail):
        assert resp.status_code == 200
        assert "French" in resp.text and "delivery-tag--interpreter" in resp.text
        assert f"/events/{event.id}/rooms/new" in resp.text


@pytest.mark.anyio
async def test_load_snapshot_ai_voice_requires_catalog_voice(event_room):
    event, room = event_room
    empty_voice_catalog = room_setup.EngineCatalog(
        speech=(),
        translation=(),
        voice=(),
        locked=(),
    )
    async with get_session() as s:
        db_room = await s.get(Room, room.id)
        snapshot = await room_setup.load_snapshot(s, db_room, empty_voice_catalog)
    assert not snapshot.plan.ai_voice


@pytest.mark.anyio
async def test_editing_with_from_setup_returns_to_wizard_rooms(admin_cookie, event_room):
    event, room = event_room
    async with _client() as c:
        page = await c.get(f"/admin/events/{event.id}/rooms/{room.id}/setup?from=setup", cookies=admin_cookie)
        assert page.status_code == 200
        assert 'href="/admin/events/' f"{event.id}" '/setup/rooms" class="btn">Cancel</a>' in page.text
        assert 'name="from" value="setup"' in page.text

        resp = await c.post(
            f"/admin/events/{event.id}/rooms/{room.id}/setup",
            data={
                "display_name": "Main Hall",
                "spoken_language": "en",
                "mode": MODE_INTERPRETERS,
                "languages": ["es"],
                "from": "setup",
            },
            headers=ORIGIN,
            cookies=admin_cookie,
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"].endswith(f"/events/{event.id}/setup/rooms")

