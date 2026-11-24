"""A translation model is only saved for languages it can translate (#1186)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from fastapi_app import app
from portal.auth import create_user_token
from portal.crypto import encrypt_val
from portal.database import (
    configure,
    create_booth,
    create_event,
    create_room,
    dispose,
    get_session,
    init_db,
    set_event_membership,
)
from portal.models import (
    BoothTranslationLanguage,
    DBBooth,
    DeveloperAccount,
    Event,
    OAuthClient,
    OAuthToken,
    Room,
    RoomTranslationLanguage,
    User,
)
from portal.roles import EVENT_OWNER
from portal.room_setup import engine_catalog
from portal.routers.oauth import hash_token
from portal.translations.model_languages import (
    fixed_language_model,
    language_support_error,
    supported_languages,
    unsupported_languages,
)
from portal.translations.registry import NLLB_200_DISTILLED_600M

# These tests are not about CSRF; their requests carry the token a real page would send.
pytestmark = pytest.mark.usefixtures("auto_csrf")

NLLB = "nllb-200-distilled-600M"
ORIGIN = {"origin": "http://test"}
ACCESS_TOKEN = "model-languages-access-token"
UNSUPPORTED = "The built-in translator (nllb-200-distilled-600M) doesn't support Azerbaijani."


# --- The check itself ----------------------------------------------------------------------


def test_only_the_built_in_translator_has_a_fixed_language_list():
    """Only the built in translator has a fixed language list."""
    assert fixed_language_model("local", NLLB) is NLLB_200_DISTILLED_600M
    for provider in ("openai", "gemini", "anthropic", "groq", "openrouter", "self_hosted", "", None):
        assert fixed_language_model(provider, "any-model") is None
        assert supported_languages(provider, "any-model") is None
        assert unsupported_languages(provider, "any-model", ["en", "az", "xx"]) == []


def test_a_built_in_model_without_an_entry_uses_the_nllb_languages():
    """A built in model without an entry uses the nllb languages."""
    assert fixed_language_model("local", "/models/my-nllb-build") is NLLB_200_DISTILLED_600M
    assert fixed_language_model("local", None) is NLLB_200_DISTILLED_600M


def test_supported_languages_are_the_registry_entrys_codes():
    """Supported languages are the registry entrys codes."""
    assert supported_languages("local", NLLB) == sorted(NLLB_200_DISTILLED_600M.languages)


def test_unsupported_languages_skip_a_language_that_is_not_set():
    """Unsupported languages skip a language that is not set."""
    assert unsupported_languages("local", NLLB, [None, "en", "", "az", "fr", "no", "az"]) == ["az", "no"]


def test_the_error_names_the_model_and_every_language_it_lacks():
    """The error names the model and every language it lacks."""
    assert language_support_error("local", NLLB, ["en", "fr", "el"]) is None
    assert language_support_error("openai", "gpt-4o-mini", ["en", "az"]) is None
    assert language_support_error("local", NLLB, ["az", "fr", "no"]) == (
        "The built-in translator (nllb-200-distilled-600M) doesn't support Azerbaijani, Norwegian. "
        "Choose another translation service or change the languages."
    )


def test_the_catalog_tells_the_setup_form_which_services_have_a_fixed_list():
    """The catalog tells the setup form which services have a fixed list."""
    event = Event(slug="demo", display_name="Demo", encrypted_translation_openai_api_key=encrypt_val("sk-test"))
    catalog = engine_catalog(event)

    assert {engine.provider for engine in catalog.translation} >= {"openai", "local"}
    assert catalog.translation_support == {"local": sorted(NLLB_200_DISTILLED_600M.languages)}
    assert catalog.translation_unfit(["en", "fr"]) == set()
    assert catalog.translation_unfit(["en", None, "az"]) == {"local"}
    assert catalog.translation_hidden(["en", "az"]) == {"local"}


def test_no_service_is_hidden_when_none_translates_the_languages():
    """With nothing that fits, every service stays on offer so the picker is never empty."""
    catalog = engine_catalog(Event(slug="demo", display_name="Demo"))

    assert catalog.translation_unfit(["en", "az"]) == {engine.provider for engine in catalog.translation}
    assert catalog.translation_hidden(["en", "az"]) == set()


# --- Fixtures ------------------------------------------------------------------------------


@pytest.fixture
async def setup_db():
    """Provide an isolated database for language validation tests."""
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def admin_room(setup_db):
    """Create an authenticated admin and a room with a hosted translation key."""
    async with get_session() as s:
        admin = User(email="lang-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        s.add(admin)
        event = await create_event(s, slug="langcon", display_name="LangCon")
        event.encrypted_translation_openai_api_key = encrypt_val("sk-test")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        room.floor_language_code = "en"
        await s.flush()
        cookie = {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)}
        return cookie, event.id, room.id


@pytest.fixture
async def admin_booth(admin_room):
    """Add a French booth to the authenticated room fixture."""
    cookie, event_id, room_id = admin_room
    async with get_session() as s:
        booth = await create_booth(s, event_id=event_id, room_id=room_id, language_code="fr", language_name="French")
        return cookie, event_id, room_id, booth.id


def _client(cookies: dict[str, str]) -> AsyncClient:
    """Build an ASGI client carrying the given session cookies."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookies)


async def _room(room_id: int) -> Room:
    """Read persisted room settings after a request."""
    async with get_session() as s:
        return await s.get(Room, room_id)


async def _room_targets(room_id: int) -> list[str]:
    """Read the enabled target languages persisted for a room."""
    async with get_session() as s:
        rows = await s.scalars(
            select(RoomTranslationLanguage.language_code).where(
                RoomTranslationLanguage.room_id == room_id, RoomTranslationLanguage.enabled.is_(True)
            )
        )
        return sorted(rows)


async def _set_room(room_id: int, *, provider: str, targets: list[str], source: str = "en") -> None:
    """Put a room in a translation setup directly, as one saved before the rule existed could be."""
    async with get_session() as s:
        room = await s.get(Room, room_id)
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_translation_provider = provider
        room.floor_translation_model = NLLB if provider == "local" else "gpt-4o-mini"
        room.floor_language_code = source
        for code in targets:
            s.add(RoomTranslationLanguage(room_id=room_id, language_code=code, language_name=code, enabled=True))


async def _post_room(cookie, event_id, room_id, **fields):
    """Submit room settings through the admin form."""
    async with _client(cookie) as client:
        return await client.post(f"/admin/events/{event_id}/rooms/{room_id}/edit", data=fields, headers=ORIGIN)


# --- The room page's AI settings -----------------------------------------------------------


@pytest.mark.anyio
async def test_room_ai_settings_refuse_a_model_that_lacks_a_room_language(admin_room):
    """Room ai settings refuse a model that lacks a room language."""
    cookie, event_id, room_id = admin_room
    await _set_room(room_id, provider="openai", targets=["fr", "az"])

    res = await _post_room(cookie, event_id, room_id, form_section="ai", floor_translation_provider="local")

    assert res.status_code == 400
    assert UNSUPPORTED in res.json()["detail"]
    room = await _room(room_id)
    assert (room.floor_translation_provider, room.floor_translation_model) == ("openai", "gpt-4o-mini")


@pytest.mark.anyio
async def test_room_ai_settings_refuse_a_model_that_lacks_the_language_spoken(admin_room):
    """Room ai settings refuse a model that lacks the language spoken."""
    cookie, event_id, room_id = admin_room
    await _set_room(room_id, provider="openai", targets=["fr"], source="az")

    res = await _post_room(cookie, event_id, room_id, form_section="ai", floor_translation_provider="local")

    assert res.status_code == 400
    assert UNSUPPORTED in res.json()["detail"]


@pytest.mark.anyio
async def test_room_ai_settings_accept_a_model_that_translates_every_language(admin_room):
    """Room ai settings accept a model that translates every language."""
    cookie, event_id, room_id = admin_room
    await _set_room(room_id, provider="openai", targets=["fr", "el"])

    res = await _post_room(cookie, event_id, room_id, form_section="ai", floor_translation_provider="local")

    assert res.status_code == 303
    room = await _room(room_id)
    assert (room.floor_translation_provider, room.floor_translation_model) == ("local", NLLB)


@pytest.mark.anyio
async def test_a_room_saved_before_the_rule_can_still_save_its_other_settings(admin_room):
    """A room saved before the rule can still save its other settings."""
    cookie, event_id, room_id = admin_room
    await _set_room(room_id, provider="local", targets=["az"])

    ai = await _post_room(
        cookie,
        event_id,
        room_id,
        form_section="ai",
        floor_translation_provider="local",
        floor_ai_interpreter_persona="A formal interpreter",
    )
    general = await _post_room(cookie, event_id, room_id, form_section="general", display_name="Renamed Hall")

    assert (ai.status_code, general.status_code) == (303, 303)
    room = await _room(room_id)
    assert (room.floor_ai_interpreter_persona, room.display_name) == ("A formal interpreter", "Renamed Hall")


@pytest.mark.anyio
async def test_room_page_offers_only_services_that_translate_its_languages(admin_room):
    """Room page offers only services that translate its languages."""
    cookie, event_id, room_id = admin_room
    await _set_room(room_id, provider="openai", targets=["az"])

    async with _client(cookie) as client:
        res = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")
        page = res.text
    service_picker = page.split('id="floor_translation_provider"')[1].split("</select>")[0]

    assert 'value="openai"' in service_picker
    assert 'value="local"' not in service_picker


@pytest.mark.anyio
async def test_room_page_still_shows_the_saved_service_when_it_no_longer_fits(admin_room):
    """Room page still shows the saved service when it no longer fits."""
    cookie, event_id, room_id = admin_room
    await _set_room(room_id, provider="local", targets=["az"])

    async with _client(cookie) as client:
        page = (await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")).text
    service_picker = page.split('id="floor_translation_provider"')[1].split("</select>")[0]

    assert 'value="local" selected' in service_picker


@pytest.mark.anyio
async def test_room_setup_form_gets_the_language_list_of_the_built_in_translator(admin_room):
    """Room setup form gets the language list of the built in translator."""
    cookie, event_id, room_id = admin_room

    async with _client(cookie) as client:
        page = (await client.get(f"/admin/events/{event_id}/rooms/{room_id}/setup")).text

    support = page.split("data-translation-support='")[1].split("'")[0]
    assert '"local"' in support and '"fr"' in support and '"az"' not in support and "openai" not in support


# --- The older room form -------------------------------------------------------------------


def _translation_form(**fields) -> dict:
    """Build the legacy room translation form fields."""
    return {
        "form_section": "translation",
        "floor_translation_enabled": "on",
        "floor_translation_provider": "local",
        "floor_translation_model": NLLB,
        "floor_translation_languages": ["fr"],
        **fields,
    }


@pytest.mark.anyio
async def test_older_room_form_refuses_a_language_the_model_lacks(admin_room):
    """Older room form refuses a language the model lacks."""
    cookie, event_id, room_id = admin_room

    res = await _post_room(cookie, event_id, room_id, **_translation_form(floor_translation_languages=["fr", "az"]))

    assert res.status_code == 400
    assert UNSUPPORTED in res.json()["detail"]
    room = await _room(room_id)
    assert (room.floor_translation_enabled, room.floor_translation_provider) == (False, None)
    assert await _room_targets(room_id) == []


@pytest.mark.anyio
async def test_older_room_form_saves_languages_the_model_translates(admin_room):
    """Older room form saves languages the model translates."""
    cookie, event_id, room_id = admin_room

    res = await _post_room(cookie, event_id, room_id, **_translation_form(floor_translation_languages=["fr", "de"]))

    assert res.status_code == 303
    assert await _room_targets(room_id) == ["de", "fr"]


@pytest.mark.anyio
async def test_older_room_form_lets_a_hosted_model_translate_any_language(admin_room):
    """Older room form lets a hosted model translate any language."""
    cookie, event_id, room_id = admin_room

    res = await _post_room(
        cookie,
        event_id,
        room_id,
        **_translation_form(
            floor_translation_provider="openai",
            floor_translation_model="gpt-4o-mini",
            floor_translation_languages=["az"],
        ),
    )

    assert res.status_code == 303
    assert await _room_targets(room_id) == ["az"]


@pytest.mark.anyio
async def test_older_room_form_can_always_turn_translation_off(admin_room):
    """Older room form can always turn translation off."""
    cookie, event_id, room_id = admin_room
    await _set_room(room_id, provider="local", targets=["az"])

    form = _translation_form(floor_translation_languages=["az"])
    del form["floor_translation_enabled"]
    res = await _post_room(cookie, event_id, room_id, **form)

    assert res.status_code == 303
    assert (await _room(room_id)).floor_translation_enabled is False


@pytest.mark.anyio
async def test_older_room_form_refuses_a_spoken_language_the_model_lacks(admin_room):
    """Older room form refuses a spoken language the model lacks."""
    cookie, event_id, room_id = admin_room
    await _set_room(room_id, provider="local", targets=["fr"])

    res = await _post_room(
        cookie,
        event_id,
        room_id,
        form_section="transcription",
        floor_transcription_enabled="on",
        floor_language_code="az",
    )

    assert res.status_code == 400
    assert UNSUPPORTED in res.json()["detail"]
    assert (await _room(room_id)).floor_language_code == "en"


# --- Booths --------------------------------------------------------------------------------


async def _save_booth(cookie, event_id, room_id, booth_id, **fields):
    """Submit booth translation settings through the admin form."""
    data = {"translation_provider": "local", "translation_model": NLLB, "translation_languages": ["de"], **fields}
    async with _client(cookie) as client:
        return await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/translation-settings",
            data=data,
            headers=ORIGIN,
        )


async def _booth(booth_id: int) -> DBBooth:
    """Read persisted booth settings after a request."""
    async with get_session() as s:
        return await s.get(DBBooth, booth_id)


@pytest.mark.anyio
async def test_booth_refuses_a_language_the_model_lacks(admin_booth):
    """Booth refuses a language the model lacks."""
    cookie, event_id, room_id, booth_id = admin_booth

    res = await _save_booth(
        cookie, event_id, room_id, booth_id, translation_enabled="true", translation_languages=["de", "az"]
    )

    assert res.status_code == 400
    assert UNSUPPORTED in res.json()["detail"]
    assert (await _booth(booth_id)).translation_enabled is False


@pytest.mark.anyio
async def test_booth_saves_languages_the_model_translates(admin_booth):
    """Booth saves languages the model translates."""
    cookie, event_id, room_id, booth_id = admin_booth

    res = await _save_booth(cookie, event_id, room_id, booth_id, translation_enabled="true")

    assert res.status_code == 303
    booth = await _booth(booth_id)
    assert (booth.translation_enabled, booth.translation_provider, booth.translation_model) == (True, "local", NLLB)


@pytest.mark.anyio
async def test_booth_lets_a_hosted_model_translate_any_language(admin_booth):
    """Booth lets a hosted model translate any language."""
    cookie, event_id, room_id, booth_id = admin_booth

    res = await _save_booth(
        cookie,
        event_id,
        room_id,
        booth_id,
        translation_enabled="true",
        translation_provider="openai",
        translation_model="gpt-4o-mini",
        translation_languages=["az"],
    )

    assert res.status_code == 303


@pytest.mark.anyio
async def test_booth_with_translation_off_is_not_checked(admin_booth):
    """Booth with translation off is not checked."""
    cookie, event_id, room_id, booth_id = admin_booth

    res = await _save_booth(cookie, event_id, room_id, booth_id, translation_languages=["az"])

    assert res.status_code == 303
    assert (await _booth(booth_id)).translation_enabled is False


@pytest.mark.anyio
async def test_booth_language_cannot_change_to_one_its_model_lacks(admin_booth):
    """Booth language cannot change to one its model lacks."""
    cookie, event_id, room_id, booth_id = admin_booth
    async with get_session() as s:
        booth = await s.get(DBBooth, booth_id)
        booth.translation_enabled = True
        booth.translation_provider = "local"
        booth.translation_model = NLLB
        s.add(BoothTranslationLanguage(booth_id=booth_id, language_code="de", language_name="German", enabled=True))

    async with _client(cookie) as client:
        url = f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/edit"
        refused = await client.post(url, data={"language_code": "az"}, headers=ORIGIN)
        saved = await client.post(url, data={"language_code": "es"}, headers=ORIGIN)

    assert refused.status_code == 400
    assert UNSUPPORTED in refused.json()["detail"]
    assert saved.status_code == 303
    assert (await _booth(booth_id)).language_code == "es"


@pytest.mark.anyio
async def test_booth_page_offers_only_services_that_translate_its_languages(admin_booth):
    """Booth page offers only services that translate its languages."""
    cookie, event_id, room_id, booth_id = admin_booth
    async with get_session() as s:
        booth = await s.get(DBBooth, booth_id)
        booth.language_code = "az"
        booth.translation_provider = "openai"
        await s.commit()

    async with _client(cookie) as client:
        res = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/")
        page = res.text
    service_picker = page.split('id="translation_provider"')[1].split("</select>")[0]

    assert 'value="openai"' in service_picker
    assert 'value="local"' not in service_picker


@pytest.mark.anyio
async def test_booth_and_room_service_pickers_are_never_empty(admin_booth):
    """No service translates the language and none is saved: the pickers still offer the services."""
    cookie, event_id, room_id, booth_id = admin_booth
    await _set_room(room_id, provider="local", targets=["az"])
    async with get_session() as s:
        event = await s.get(Event, event_id)
        for attribute in Event.__mapper__.column_attrs.keys():
            if attribute.endswith("_api_key"):
                setattr(event, attribute, None)
        booth = await s.get(DBBooth, booth_id)
        booth.language_code = "az"
        booth.translation_provider = None
        room = await s.get(Room, room_id)
        room.floor_language_code = "az"
        room.floor_translation_provider = None
        await s.commit()

    async with _client(cookie) as client:
        booth_page = (await client.get(f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/")).text
        room_page = (await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")).text

    assert 'value="local"' in booth_page.split('id="translation_provider"')[1].split("</select>")[0]
    assert 'value="local"' in room_page.split('id="floor_translation_provider"')[1].split("</select>")[0]


@pytest.mark.anyio
async def test_booth_page_still_shows_the_saved_service_when_it_no_longer_fits(admin_booth):
    """Booth page still shows the saved service when it no longer fits."""
    cookie, event_id, room_id, booth_id = admin_booth
    async with get_session() as s:
        booth = await s.get(DBBooth, booth_id)
        booth.language_code = "az"
        booth.translation_provider = "local"
        await s.commit()

    async with _client(cookie) as client:
        res = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/")
        page = res.text
    service_picker = page.split('id="translation_provider"')[1].split("</select>")[0]

    assert 'value="local"' in service_picker


# --- The API -------------------------------------------------------------------------------


@pytest.fixture
async def api_event(setup_db):
    """Create an owner and scoped OAuth token for room API tests."""
    async with get_session() as s:
        user = User(email="lang-owner@example.com", display_name="Owner", email_verified=True)
        s.add(user)
        event = await create_event(s, slug="apicon", display_name="APICon")
        await s.flush()
        await set_event_membership(s, user_id=user.id, event_id=event.id, role=EVENT_OWNER)
        account = DeveloperAccount(user_id=user.id, status="approved")
        s.add(account)
        await s.flush()
        oauth_client = OAuthClient(developer_account_id=account.id, client_id="model-languages-client", name="API")
        s.add(oauth_client)
        await s.flush()
        s.add(
            OAuthToken(
                client_id=oauth_client.id,
                user_id=user.id,
                event_id=event.id,
                scopes=["rooms:write"],
                access_token_hash=hash_token(ACCESS_TOKEN),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
    return event


async def _put_room(body: dict):
    """Upsert the test room with its scoped OAuth token."""
    async with _client({}) as client:
        return await client.put(
            "/api/v1/events/apicon/rooms/hall-1", json=body, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
        )


async def _api_room() -> Room | None:
    """Read the room saved by the room API."""
    async with get_session() as s:
        return await s.scalar(select(Room).where(Room.eventyay_room_id == "hall-1"))


LOCAL_ROOM = {
    "name": "Hall 1",
    "source_language": "en",
    "enable_translation": True,
    "translation_provider": "local",
    "translation_model": NLLB,
    "target_languages": ["fr"],
}


@pytest.mark.anyio
async def test_api_does_not_create_a_room_whose_model_lacks_a_language(api_event):
    """API does not create a room whose model lacks a language."""
    res = await _put_room({**LOCAL_ROOM, "target_languages": ["fr", "az"]})

    assert res.status_code == 422
    assert UNSUPPORTED in res.json()["detail"]
    assert await _api_room() is None


@pytest.mark.anyio
async def test_api_creates_a_room_whose_model_translates_its_languages(api_event):
    """API creates a room whose model translates its languages."""
    res = await _put_room(LOCAL_ROOM)

    assert res.status_code == 200
    room = await _api_room()
    assert (room.floor_translation_provider, room.floor_translation_model) == ("local", NLLB)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "update",
    [
        {"source_language": "az"},
        {"target_languages": ["fr", "az"]},
    ],
    ids=["spoken language", "target language"],
)
async def test_api_update_is_checked_against_the_room_as_it_will_be(api_event, update):
    """API update is checked against the room as it will be."""
    assert (await _put_room(LOCAL_ROOM)).status_code == 200

    res = await _put_room(update)

    assert res.status_code == 422
    assert UNSUPPORTED in res.json()["detail"]
    room = await _api_room()
    assert room.floor_language_code == "en"
    assert await _room_targets(room.id) == ["fr"]


@pytest.mark.anyio
async def test_api_switching_to_the_built_in_model_checks_the_languages_already_saved(api_event):
    """API switching to the built in model checks the languages already saved."""
    hosted = {**LOCAL_ROOM, "translation_provider": "openai", "translation_model": "gpt-4o-mini"}
    assert (await _put_room({**hosted, "target_languages": ["az"]})).status_code == 200

    res = await _put_room({"translation_provider": "local", "translation_model": NLLB})

    assert res.status_code == 422
    assert (await _api_room()).floor_translation_provider == "openai"


@pytest.mark.anyio
async def test_api_can_turn_translation_off_and_leaves_unrelated_updates_alone(api_event):
    """API can turn translation off and leaves unrelated updates alone."""
    hosted = {**LOCAL_ROOM, "translation_provider": "openai", "translation_model": "gpt-4o-mini"}
    assert (await _put_room({**hosted, "target_languages": ["az"]})).status_code == 200
    async with get_session() as s:  # a room left on a model that lacks its language
        room = await s.scalar(select(Room).where(Room.eventyay_room_id == "hall-1"))
        room.floor_translation_provider, room.floor_translation_model = "local", NLLB

    renamed = await _put_room({"name": "Renamed"})
    off = await _put_room({"enable_translation": False, "target_languages": ["az"]})

    assert (renamed.status_code, off.status_code) == (200, 200)
    assert (await _api_room()).floor_translation_enabled is False


@pytest.mark.anyio
@pytest.mark.parametrize("prefix", ["/admin", "/workspace"])
@pytest.mark.parametrize("save_path", ["room_ai", "room_source", "booth_translation", "booth_source"])
async def test_browser_translation_errors_return_the_settings_form(admin_booth, prefix, save_path):
    """Browser validation keeps the form and error visible without committing partial changes."""
    cookie, event_id, room_id, booth_id = admin_booth
    await _set_room(room_id, provider="openai", targets=["az"])
    room_url = f"{prefix}/events/{event_id}/rooms/{room_id}"
    booth_url = f"{room_url}/booths/{booth_id}"
    if save_path == "room_ai":
        url = f"{room_url}/edit"
        fields = {
            "form_section": "ai",
            "floor_translation_provider": "local",
            "floor_transcription_provider": "local",
            "floor_transcription_model": "small",
        }
        expected_action = f"{room_url}/edit"
    elif save_path == "room_source":
        await _set_room(room_id, provider="local", targets=[])
        url = f"{room_url}/edit"
        fields = {"form_section": "transcription", "floor_language_code": "az"}
        expected_action = f"{room_url}/edit"
    elif save_path == "booth_translation":
        url = f"{booth_url}/translation-settings"
        fields = {
            "translation_enabled": "true",
            "translation_provider": "local",
            "translation_model": NLLB,
            "translation_languages": ["az"],
        }
        expected_action = url
    else:
        async with get_session() as session:
            booth = await session.get(DBBooth, booth_id)
            booth.translation_enabled = True
            booth.translation_provider = "local"
            booth.translation_model = NLLB
        url = f"{booth_url}/edit"
        fields = {"language_code": "az", "language_name": "Changed name"}
        expected_action = url

    async with _client(cookie) as client:
        response = await client.post(url, data=fields, headers={**ORIGIN, "accept": "text/html"})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert 'role="alert"' in response.text or "role='alert'" in response.text
    assert "doesn&#39;t support Azerbaijani" in response.text
    assert f'action="{expected_action}"' in response.text
    room = await _room(room_id)
    assert room.floor_language_code == "en"
    assert room.floor_transcription_model != "small"
    async with get_session() as session:
        booth = await session.get(DBBooth, booth_id)
        assert (booth.language_code, booth.language_name) == ("fr", "French")
        assert booth.translation_enabled == (save_path == "booth_source")


@pytest.mark.anyio
async def test_api_resending_an_older_rooms_unchanged_setup_still_saves(api_event):
    """A sync that re-sends a room saved before the rule keeps working until the setup changes."""
    hosted = {**LOCAL_ROOM, "translation_provider": "openai", "translation_model": "gpt-4o-mini"}
    assert (await _put_room({**hosted, "target_languages": ["az"]})).status_code == 200
    async with get_session() as s:
        room = await s.scalar(select(Room).where(Room.eventyay_room_id == "hall-1"))
        room.floor_translation_provider, room.floor_translation_model = "local", NLLB
    unchanged = {**LOCAL_ROOM, "target_languages": ["az"]}

    resent = await _put_room({**unchanged, "name": "Renamed"})
    changed = await _put_room({**unchanged, "target_languages": ["az", "fr"]})

    assert resent.status_code == 200
    assert changed.status_code == 422
    room = await _api_room()
    assert room.display_name == "Renamed"
    assert await _room_targets(room.id) == ["az"]
