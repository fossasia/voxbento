"""Built-in translation models: only the ones the operator offers, downloaded only by Admins (#1191)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from huggingface_hub.utils import LocalEntryNotFoundError
from pydantic import ValidationError
from sqlalchemy import select

import portal.translations.providers.local as local
from fastapi_app import app
from portal.auth import create_user_token
from portal.config import Settings, settings
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
from portal.models import DBBooth, DeveloperAccount, Event, OAuthClient, OAuthToken, Room, User
from portal.roles import EVENT_OWNER
from portal.room_setup import blank_plan, engine_catalog
from portal.routers.oauth import hash_token
from portal.translations.providers.local import LocalProvider, get_download_progress, trigger_download

NLLB = "nllb-200-distilled-600M"
OWN_BUILD = "someone/their-nllb-build"
ORIGIN = {"origin": "http://test"}
ACCESS_TOKEN = "local-models-access-token"


@pytest.fixture
def offered(monkeypatch):
    """Offer NLLB and an operator's own build, in that order."""
    monkeypatch.setattr(settings, "local_translation_models", f"{NLLB},{OWN_BUILD}")


@pytest.fixture
def none_offered(monkeypatch):
    monkeypatch.setattr(settings, "local_translation_models", "")


@pytest.fixture
def no_downloads():
    """Leave the real download state untouched and record what would start."""
    with (
        patch("portal.routers.translation_models.trigger_download") as trigger,
        patch("portal.routers.translation_models.get_download_progress", return_value={}),
    ):
        yield trigger


# ── Settings ───────────────────────────────────────────────────────────────


def test_existing_servers_keep_offering_nllb():
    assert Settings(_env_file=None).local_translation_model_list == (NLLB,)


def test_model_list_keeps_order_and_drops_blanks_and_duplicates():
    s = Settings(_env_file=None, local_translation_models=f" {OWN_BUILD}, ,{NLLB},{OWN_BUILD} ")
    assert s.local_translation_model_list == (OWN_BUILD, NLLB)
    assert Settings(_env_file=None, local_translation_models=" , ").local_translation_model_list == ()


@pytest.mark.parametrize(
    ("models", "model", "allowed", "chosen"),
    [
        (f"{NLLB},{OWN_BUILD}", OWN_BUILD, True, OWN_BUILD),
        (f"{NLLB},{OWN_BUILD}", "evil/any-repo", False, NLLB),
        (f"{NLLB},{OWN_BUILD}", None, False, NLLB),
        ("", NLLB, False, None),
    ],
)
def test_allow_list_and_default(models, model, allowed, chosen):
    s = Settings(_env_file=None, local_translation_models=models)
    assert s.local_translation_allows(model) is allowed
    assert s.local_translation_model_or_default(model) == chosen


def test_a_model_name_longer_than_the_room_column_is_rejected():
    """Rooms and booths store the model in a 100-character column, so a longer name would be cut."""
    assert Settings(_env_file=None, local_translation_models="a" * 100).local_translation_model_list == ("a" * 100,)
    with pytest.raises(ValidationError, match="100 characters or fewer"):
        Settings(_env_file=None, local_translation_models=f"{NLLB},{'a' * 101}")


# ── Room setup catalog ─────────────────────────────────────────────────────


def _event() -> Event:
    return Event(slug="x", display_name="X", transcription_api_enabled=False)


def test_new_rooms_get_the_first_offered_model(monkeypatch):
    monkeypatch.setattr(settings, "local_translation_models", f"{OWN_BUILD},{NLLB}")
    catalog = engine_catalog(_event())
    engine = catalog.find("translation", "local")
    assert engine.model == OWN_BUILD
    # Rooms never download it themselves, so the note must not promise that.
    assert engine.note == "free, an Admin downloads it first"
    assert blank_plan(catalog).translation_engine == "local"


def test_not_offered_when_the_list_is_empty(none_offered):
    catalog = engine_catalog(_event())
    assert catalog.find("translation", "local") is None
    assert "NLLB (built in) needs LOCAL_TRANSLATION_MODELS" in catalog.unavailable
    assert blank_plan(catalog).translation_engine != "local"


# ── Runtime ────────────────────────────────────────────────────────────────


async def _translate(model: str) -> str | None:
    return await LocalProvider().translate(
        provider_name="local",
        text="Hello world",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="English",
        source_lang_code="en",
        model=model,
        api_key=None,
    )


@pytest.mark.anyio
async def test_a_model_that_is_not_offered_is_never_loaded_or_downloaded(offered, caplog):
    with (
        patch.object(local, "get_model_and_tokenizer") as load,
        patch("huggingface_hub.snapshot_download") as download,
        caplog.at_level(logging.ERROR),
    ):
        assert await _translate("evil/any-repo") is None
    load.assert_not_called()
    download.assert_not_called()
    assert "'evil/any-repo' is not offered on this server" in caplog.text


@pytest.mark.anyio
async def test_an_offered_model_still_loads(offered):
    with patch.object(local, "get_model_and_tokenizer", side_effect=RuntimeError("load attempted")) as load:
        assert await _translate(OWN_BUILD) is None
    load.assert_called_once_with(OWN_BUILD)


@pytest.mark.anyio
async def test_the_live_demo_model_translates_when_rooms_are_not_offered_it(none_offered, monkeypatch):
    """LIVE_DEMO_TRANSLATION_MODEL is the operator's own setting, so turning off built-in rooms keeps the demo."""
    monkeypatch.setattr(settings, "live_demo_translation_model", OWN_BUILD)
    with patch.object(local, "get_model_and_tokenizer", side_effect=RuntimeError("load attempted")) as load:
        assert await _translate(OWN_BUILD) is None
    load.assert_called_once_with(OWN_BUILD)


@pytest.mark.anyio
async def test_an_offered_model_that_is_not_downloaded_is_never_downloaded_by_a_room(offered, caplog):
    def cache_only(**kwargs):
        if kwargs.get("local_files_only"):
            raise LocalEntryNotFoundError("not in the cache")
        return "/downloaded"

    with (
        patch("huggingface_hub.snapshot_download", side_effect=cache_only) as hub,
        caplog.at_level(logging.ERROR),
    ):
        assert await _translate(OWN_BUILD) is None
    assert hub.call_args_list, "the cache was never checked"
    assert all(call.kwargs.get("local_files_only") for call in hub.call_args_list)
    assert OWN_BUILD not in local._loaded_models
    assert f"{OWN_BUILD!r} is not downloaded. An Admin must download it" in caplog.text


def test_a_model_folder_on_the_server_loads_without_the_hub(tmp_path, monkeypatch):
    folder = str(tmp_path)
    monkeypatch.delitem(local._loaded_models, folder, raising=False)
    with (
        patch("huggingface_hub.snapshot_download") as hub,
        patch("ctranslate2.Translator") as translator,
        patch("transformers.AutoTokenizer.from_pretrained"),
    ):
        local.get_model_and_tokenizer(folder)
    local._loaded_models.pop(folder)
    hub.assert_not_called()
    assert translator.call_args.args == (folder,)


def test_a_second_download_of_the_same_model_starts_nothing(monkeypatch):
    monkeypatch.setitem(local._download_progress, "test/in-flight", {"status": "downloading"})
    with patch("huggingface_hub.snapshot_download") as download:
        trigger_download("test/in-flight")
    download.assert_not_called()
    assert local._download_progress["test/in-flight"] == {"status": "downloading"}


def test_a_model_folder_on_the_server_counts_as_downloaded(tmp_path):
    with patch("huggingface_hub.snapshot_download") as cache_lookup:
        assert get_download_progress(str(tmp_path))["status"] == "completed"
    cache_lookup.assert_not_called()


# ── HTTP ───────────────────────────────────────────────────────────────────


@pytest.fixture
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def people(setup_db):
    """An Admin and a plain event owner, as session cookies, plus the owner's event and room."""
    async with get_session() as s:
        admin = User(email="models-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        owner = User(email="models-owner@example.com", display_name="Owner", email_verified=True, can_host=True)
        s.add_all([admin, owner])
        event = await create_event(s, slug="modelcon", display_name="ModelCon")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        await s.flush()
        await set_event_membership(s, user_id=owner.id, event_id=event.id, role=EVENT_OWNER)
        admin_cookie = {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)}
        owner_cookie = {"user_token": create_user_token(user_id=owner.id, email=owner.email)}
        return admin_cookie, owner_cookie, event.id, room.id


def _client(cookies: dict[str, str]) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookies)


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_an_event_owner_cannot_start_or_see_a_download(people):
    _, owner_cookie, _, _ = people
    with patch.object(local, "trigger_download") as trigger:
        async with _client(owner_cookie) as client:
            old_route = await client.post("/workspace/models/trigger_download", json={"model": NLLB})
            old_admin_route = await client.post("/admin/models/trigger_download", json={"model": NLLB})
            new_route = await client.post("/admin/translation-models/download", data={"model": NLLB})
            page = await client.get("/admin/translation-models/")
    # The old event-owner routes are gone; the new ones are Admin only.
    assert old_route.status_code == 404
    assert old_admin_route.status_code == 404
    assert new_route.status_code == 403
    assert page.status_code == 403
    trigger.assert_not_called()


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_admin_downloads_an_offered_model(people, offered, no_downloads):
    admin_cookie, _, _, _ = people
    async with _client(admin_cookie) as client:
        res = await client.post("/admin/translation-models/download", data={"model": OWN_BUILD})
    assert res.status_code == 303
    assert res.headers["location"] == "/admin/translation-models/"
    no_downloads.assert_called_once_with(OWN_BUILD)


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
@pytest.mark.parametrize("model", ["evil/any-repo", "x" * 101, ""])
async def test_admin_cannot_download_a_model_that_is_not_offered(people, offered, no_downloads, model):
    admin_cookie, _, _, _ = people
    async with _client(admin_cookie) as client:
        res = await client.post("/admin/translation-models/download", data={"model": model})
    assert res.status_code in (404, 422)
    no_downloads.assert_not_called()


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_an_unlisted_registry_model_cannot_be_downloaded(people, none_offered, no_downloads):
    admin_cookie, _, _, _ = people
    async with _client(admin_cookie) as client:
        res = await client.post("/admin/translation-models/download", data={"model": NLLB})
    assert res.status_code == 404
    assert res.json()["detail"] == "That model is not offered on this server."
    no_downloads.assert_not_called()


@pytest.mark.anyio
async def test_admin_download_needs_the_csrf_token(people, offered, no_downloads):
    admin_cookie, _, _, _ = people
    async with _client(admin_cookie) as client:
        res = await client.post("/admin/translation-models/download", data={"model": NLLB}, headers=ORIGIN)
    assert res.status_code == 403
    no_downloads.assert_not_called()


@pytest.mark.anyio
async def test_admin_page_lists_models_with_size_and_download_status(people, offered):
    admin_cookie, _, _, _ = people
    states = {NLLB: {"status": "completed"}, OWN_BUILD: {"status": "downloading"}}
    with patch("portal.routers.translation_models.get_download_progress", side_effect=lambda m: states[m]):
        async with _client(admin_cookie) as client:
            page = await client.get("/admin/translation-models/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    html = page.text
    assert 'href="/admin/translation-models/" class="is-active">Translation models</a>' in html
    nllb_row = html[html.index(f"<code>{NLLB}</code>") : html.index(f"<code>{OWN_BUILD}</code>")]
    own_row = html[html.index(f"<code>{OWN_BUILD}</code>") : html.index("</tbody>")]
    assert "~600 MB" in nllb_row and "CC-BY-NC-4.0" in nllb_row and "Downloaded" in nllb_row
    assert "<td>Unknown</td>" in own_row and "Downloading" in own_row
    assert 'action="/admin/translation-models/download"' not in html


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("models", "status", "shown", "button"),
    [
        (NLLB, "", "Download</button>", True),
        (NLLB, "error", "Try again</button>", True),
        ("", "", "Not downloaded", False),
    ],
)
async def test_admin_page_offers_a_download_only_for_offered_models(people, monkeypatch, models, status, shown, button):
    monkeypatch.setattr(settings, "local_translation_models", models)
    admin_cookie, _, _, _ = people
    with patch("portal.routers.translation_models.get_download_progress", return_value={"status": status}):
        async with _client(admin_cookie) as client:
            html = (await client.get("/admin/translation-models/")).text
    assert f"<code>{NLLB}</code>" in html
    assert shown in html
    assert ('<input type="hidden" name="model" value="nllb-200-distilled-600M" />' in html) is button
    assert ("Failed" in html) is (status == "error")


# ── Room and booth settings ────────────────────────────────────────────────


async def _room(room_id: int) -> Room:
    async with get_session() as s:
        return await s.get(Room, room_id)


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_models_endpoint_lists_only_offered_models(people, monkeypatch, offered):
    _, owner_cookie, _, _ = people
    async with _client(owner_cookie) as client:
        listed = (await client.get("/api/workspace/providers/translation/models")).json()
        monkeypatch.setattr(settings, "local_translation_models", "")
        cleared = (await client.get("/api/workspace/providers/translation/models")).json()
    assert listed["local"] == [NLLB, OWN_BUILD]
    assert "local" not in cleared
    assert "openai" in cleared


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
@pytest.mark.parametrize(("model", "saved"), [(OWN_BUILD, OWN_BUILD), ("evil/any-repo", NLLB), ("", NLLB)])
async def test_room_ai_form_keeps_only_offered_models(people, offered, model, saved):
    _, owner_cookie, event_id, room_id = people
    async with _client(owner_cookie) as client:
        res = await client.post(
            f"/workspace/events/{event_id}/rooms/{room_id}/edit",
            data={"form_section": "ai", "floor_translation_provider": "local", "floor_translation_model": model},
            headers=ORIGIN,
        )
    assert res.status_code == 303
    room = await _room(room_id)
    assert (room.floor_translation_provider, room.floor_translation_model) == ("local", saved)


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_room_ai_form_passes_cloud_models_through(people, offered):
    _, owner_cookie, event_id, room_id = people
    async with get_session() as s:
        # Any stored key makes the provider selectable; it is never decrypted here.
        (await s.get(Event, event_id)).encrypted_translation_openai_api_key = "stored-key"
    async with _client(owner_cookie) as client:
        res = await client.post(
            f"/workspace/events/{event_id}/rooms/{room_id}/edit",
            data={"form_section": "ai", "floor_translation_provider": "openai", "floor_translation_model": "gpt-4o"},
            headers=ORIGIN,
        )
    assert res.status_code == 303
    room = await _room(room_id)
    assert (room.floor_translation_provider, room.floor_translation_model) == ("openai", "gpt-4o")


async def _older_form(cookie, event_id, room_id, enabled: str, model: str):
    async with _client(cookie) as client:
        return await client.post(
            f"/workspace/events/{event_id}/rooms/{room_id}/edit",
            data={
                "form_section": "translation",
                "floor_translation_enabled": enabled,
                "floor_translation_provider": "local",
                "floor_translation_model": model,
                "floor_translation_languages": ["de"],
            },
            headers=ORIGIN,
        )


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_older_room_form_replaces_a_model_that_is_not_offered(people, offered):
    _, owner_cookie, event_id, room_id = people
    assert (await _older_form(owner_cookie, event_id, room_id, "on", "evil/any-repo")).status_code == 303
    assert (await _room(room_id)).floor_translation_model == NLLB


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
@pytest.mark.parametrize(("enabled", "status"), [("on", 400), ("", 303)])
async def test_older_room_form_turns_built_in_off_but_not_on_when_none_is_offered(
    people, none_offered, enabled, status
):
    _, owner_cookie, event_id, room_id = people
    res = await _older_form(owner_cookie, event_id, room_id, enabled, NLLB)
    assert res.status_code == status
    room = await _room(room_id)
    assert room.floor_translation_enabled is False
    if status == 400:
        assert res.json()["detail"] == "Built-in translation is not offered on this server"
    else:
        assert (room.floor_translation_provider, room.floor_translation_model) == ("local", None)


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_setup_resave_replaces_a_model_taken_off_the_list(people, monkeypatch, offered):
    _, owner_cookie, event_id, room_id = people
    form = {
        "display_name": "Main Hall",
        "spoken_language": "en",
        "languages": ["de"],
        "delivery_de": "ai_text",
        "speech_engine": "local",
        "translation_engine": "local",
    }
    async with get_session() as s:
        room = await s.get(Room, room_id)
        room.floor_translation_provider, room.floor_translation_model = "local", OWN_BUILD
    async with _client(owner_cookie) as client:
        assert (
            await client.post(f"/workspace/events/{event_id}/rooms/{room_id}/setup", data=form, headers=ORIGIN)
        ).status_code == 303
        assert (await _room(room_id)).floor_translation_model == OWN_BUILD

        monkeypatch.setattr(settings, "local_translation_models", NLLB)
        assert (
            await client.post(f"/workspace/events/{event_id}/rooms/{room_id}/setup", data=form, headers=ORIGIN)
        ).status_code == 303
    assert (await _room(room_id)).floor_translation_model == NLLB


@pytest.fixture
async def booth(people):
    admin_cookie, owner_cookie, event_id, room_id = people
    async with get_session() as s:
        db_booth = await create_booth(s, event_id=event_id, room_id=room_id, language_code="fr", language_name="French")
    return owner_cookie, event_id, room_id, db_booth.id


async def _save_booth(booth, **fields):
    cookie, event_id, room_id, booth_id = booth
    async with _client(cookie) as client:
        return await client.post(
            f"/workspace/events/{event_id}/rooms/{room_id}/booths/{booth_id}/translation-settings",
            data={"translation_languages": ["de"], "translation_provider": "local", **fields},
            headers=ORIGIN,
        )


async def _booth(booth_id: int) -> DBBooth:
    async with get_session() as s:
        return await s.get(DBBooth, booth_id)


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_booth_page_and_save_use_only_offered_models(booth, monkeypatch, offered):
    cookie, event_id, room_id, booth_id = booth
    res = await _save_booth(booth, translation_enabled="true", translation_model=OWN_BUILD)
    assert res.status_code == 303
    assert (await _booth(booth_id)).translation_model == OWN_BUILD

    monkeypatch.setattr(settings, "local_translation_models", NLLB)
    async with _client(cookie) as client:
        page = (await client.get(f"/workspace/events/{event_id}/rooms/{room_id}/booths/{booth_id}/")).text
    assert f'<option value="{NLLB}" data-provider="local" >{NLLB}</option>' in page
    assert OWN_BUILD not in page


@pytest.mark.anyio
@pytest.mark.usefixtures("auto_csrf")
async def test_booth_rejects_a_model_that_is_not_offered_but_can_turn_translation_off(booth, offered):
    _, _, _, booth_id = booth
    res = await _save_booth(booth, translation_enabled="true", translation_model="evil/any-repo")
    assert res.status_code == 400
    assert res.json()["detail"] == "That built-in translation model is not offered on this server"
    assert (await _booth(booth_id)).translation_model is None

    res = await _save_booth(booth, translation_model="evil/any-repo")
    assert res.status_code == 303
    assert (await _booth(booth_id)).translation_enabled is False


# ── API v1 ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def api_event(people):
    async with get_session() as s:
        owner = await s.scalar(select(User).where(User.email == "models-owner@example.com"))
        event = await s.scalar(select(Event).where(Event.slug == "modelcon"))
        account = DeveloperAccount(user_id=owner.id, status="approved")
        s.add(account)
        await s.flush()
        oauth_client = OAuthClient(developer_account_id=account.id, client_id="local-models-client", name="API")
        s.add(oauth_client)
        await s.flush()
        s.add(
            OAuthToken(
                client_id=oauth_client.id,
                user_id=owner.id,
                event_id=event.id,
                scopes=["rooms:write"],
                access_token_hash=hash_token(ACCESS_TOKEN),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )


async def _put_room(body: dict):
    async with _client({}) as client:
        return await client.put(
            "/api/v1/events/modelcon/rooms/hall-1", json=body, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
        )


async def _api_room() -> Room:
    async with get_session() as s:
        return await s.scalar(select(Room).where(Room.eventyay_room_id == "hall-1"))


@pytest.mark.anyio
async def test_api_uses_only_offered_models(api_event, offered):
    res = await _put_room({"name": "Hall", "translation_provider": "local"})
    assert res.status_code == 200, res.text
    assert (await _api_room()).floor_translation_model == NLLB

    assert (await _put_room({"translation_model": OWN_BUILD})).status_code == 200
    assert (await _api_room()).floor_translation_model == OWN_BUILD

    res = await _put_room({"translation_model": "evil/any-repo"})
    assert res.status_code == 422
    assert res.json()["detail"] == "translation_model is not allowed for built-in translation"
    assert (await _api_room()).floor_translation_model == OWN_BUILD


@pytest.mark.anyio
async def test_api_turns_built_in_off_but_not_on_when_none_is_offered(api_event, monkeypatch, offered):
    await _put_room({"name": "Hall", "enable_translation": True, "translation_provider": "local"})
    monkeypatch.setattr(settings, "local_translation_models", "")

    res = await _put_room({"enable_translation": True})
    assert res.status_code == 422
    assert res.json()["detail"] == "Built-in translation is not offered on this server"
    res = await _put_room({"enable_translation": False, "translation_provider": "local"})
    assert res.status_code == 200, res.text
    assert (await _api_room()).floor_translation_enabled is False
