"""LibreTranslate as a self-hosted translation provider (#1204).

Covers settings validation, key lookup, the provider, the room-engine catalog,
the admin routes and the API v1 room upsert. HTTP is mocked everywhere.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import portal.globals as pg
from fastapi_app import app
from portal.auth import create_user_token
from portal.config import Settings, settings
from portal.database import (
    configure,
    create_event,
    create_room,
    dispose,
    get_session,
    init_db,
)
from portal.models import (
    Event,
    Room,
    RoomTranslationLanguage,
    TranscriptSegment,
    User,
)
from portal.room_setup import blank_plan, engine_catalog
from portal.translations.constants import LIBRETRANSLATE_MODEL_ID
from portal.translations.keys import get_translation_api_key
from portal.translations.providers.libretranslate import (
    LibreTranslateProvider,
    language_to_iso,
    libretranslate_endpoint,
)
from portal.translations.worker import KEYLESS_PROVIDERS, TranslationWorker

# These tests are not about CSRF; their requests carry the token a real page would send.
pytestmark = pytest.mark.usefixtures("auto_csrf")

BASE_URL = "http://libretranslate.internal:5000"


@pytest.fixture
def libre(monkeypatch):
    monkeypatch.setattr(settings, "libretranslate_base_url", BASE_URL)
    monkeypatch.setattr(settings, "libretranslate_api_key", "")


@pytest.fixture
def libre_with_key(monkeypatch):
    monkeypatch.setattr(settings, "libretranslate_base_url", BASE_URL)
    monkeypatch.setattr(settings, "libretranslate_api_key", "sk-libre")


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.setattr(settings, "libretranslate_base_url", "")
    monkeypatch.setattr(settings, "libretranslate_api_key", "")


@pytest.fixture
def libre_server():
    """A mocked LibreTranslate server that records each request."""
    requests: list[httpx.Request] = []
    state = {"status": 200, "translated": "Hallo Welt"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = {"translatedText": state["translated"]} if state["translated"] is not None else {}
        return httpx.Response(state["status"], json=body)

    previous = pg.shared_http_client
    pg.shared_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    yield requests, state
    pg.shared_http_client = previous


async def _translate(api_key: str | None = None, **extra) -> str | None:
    return await LibreTranslateProvider().translate(
        provider_name="libretranslate",
        text="Hello world",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="English",
        model=LIBRETRANSLATE_MODEL_ID,
        api_key=api_key,
        **extra,
    )


# ── Settings ───────────────────────────────────────────────────────────────


def _settings(url: str) -> Settings:
    return Settings(_env_file=None, libretranslate_base_url=url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", ""),
        ("  ", ""),
        ("http://localhost:5000/", "http://localhost:5000"),
        ("https://libre.example.org", "https://libre.example.org"),
    ],
)
def test_base_url_is_normalized(url, expected):
    assert _settings(url).libretranslate_base_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://libre.example.org",
        "localhost:5000",
        "http:///",
        "http://user:secret@libre.example.org",
        "http://libre.example.org?key=secret",
        "http://libre.example.org#frag",
        # Trailing '?' or '#' with an empty query/fragment still breaks the appended /translate path.
        "http://libre.example.org?",
        "http://libre.example.org#",
        "http://bad host:5000",
        "http://bad%20host:5000",
        "http://libre.internal:0",
        "http://libre.internal:99999",
        "http://libre.internal:abc",
        "http://localhost:5000/translate",
        "http://localhost:5000/translate/",
    ],
)
def test_unsafe_base_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        _settings(url)


@pytest.mark.parametrize("url", ["http://[::1]:5000", "http://libretranslate:5000", "http://10.0.0.5:5000"])
def test_ip_and_service_name_base_urls_are_accepted(url):
    assert _settings(url).libretranslate_base_url == url


def test_api_key_is_hidden_from_settings_repr():
    s = Settings(_env_file=None, libretranslate_api_key="sk-libre-secret")
    assert s.libretranslate_api_key == "sk-libre-secret"
    assert "sk-libre-secret" not in repr(s)


@pytest.mark.parametrize(
    ("url", "enabled"),
    [
        ("", False),
        ("http://libre.internal:5000", True),
    ],
)
def test_enabled_matches_url_presence(url, enabled):
    assert _settings(url).libretranslate_enabled is enabled


def test_libretranslate_endpoint_requires_configuration(unset):
    assert libretranslate_endpoint() is None


def test_libretranslate_endpoint_appends_translate(libre):
    assert libretranslate_endpoint() == f"{BASE_URL}/translate"


# ── Key lookup ─────────────────────────────────────────────────────────────


def test_key_comes_from_the_server_not_the_event(monkeypatch):
    event = Event(slug="x", display_name="X")
    monkeypatch.setattr(settings, "libretranslate_api_key", "")
    assert get_translation_api_key(event, "libretranslate") is None
    monkeypatch.setattr(settings, "libretranslate_api_key", "sk-libre")
    assert get_translation_api_key(event, "libretranslate") == "sk-libre"


def test_libretranslate_is_keyless_in_worker():
    assert "libretranslate" in KEYLESS_PROVIDERS


# ── Language code mapping ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("English", "en"),
        ("German", "de"),
        ("French", "fr"),
        ("Chinese", "zh"),
        ("en", "en"),
        ("DE", "de"),
        ("pt-BR", "pt"),
        ("zh-Hans", "zh"),
        ("en_US", "en"),
        ("", None),
        ("Klingon", None),
    ],
)
def test_language_to_iso(name, expected):
    assert language_to_iso(name) == expected


# ── Provider ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_request_goes_to_the_configured_server_without_a_key(libre, libre_server):
    requests, _ = libre_server
    result = await _translate()

    assert result == "Hallo Welt"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"{BASE_URL}/translate"
    body = json.loads(request.content)
    assert body == {"q": "Hello world", "source": "en", "target": "de", "format": "text"}


@pytest.mark.anyio
async def test_optional_key_is_sent_in_the_json_body(libre, libre_server):
    requests, _ = libre_server
    assert await _translate(api_key="sk-libre") == "Hallo Welt"
    assert json.loads(requests[0].content)["api_key"] == "sk-libre"
    # LibreTranslate takes the key in the body, not the Authorization header.
    assert "authorization" not in requests[0].headers


@pytest.mark.anyio
async def test_persona_style_and_glossary_are_not_sent(libre, libre_server):
    """LibreTranslate is sequence-to-sequence: it takes no prompt context."""
    from portal.models import AIVocabularyEntry

    requests, _ = libre_server
    entry = AIVocabularyEntry(event_id=1, source_term="booth", target_language="de", target_term="Kabine")
    assert await _translate(persona="Formal", style="Concise", vocabulary_entries=[entry]) == "Hallo Welt"
    body = json.loads(requests[0].content)
    assert set(body) == {"q", "source", "target", "format"}
    assert "Formal" not in str(body)
    assert "Kabine" not in str(body)


@pytest.mark.anyio
async def test_nothing_is_requested_when_the_server_is_not_configured(unset, libre_server, caplog):
    requests, _ = libre_server
    assert await _translate() is None
    assert requests == []
    assert "not configured" in caplog.text


@pytest.mark.anyio
async def test_missing_target_language_fails_the_segment(libre, libre_server, caplog):
    requests, _ = libre_server
    result = await LibreTranslateProvider().translate(
        provider_name="libretranslate",
        text="Hello world",
        target_lang_name="German",
        target_lang_code="",
        source_lang_name="English",
        model=LIBRETRANSLATE_MODEL_ID,
        api_key=None,
    )
    assert result is None
    assert requests == []
    assert "missing target language code" in caplog.text


@pytest.mark.anyio
async def test_server_errors_fail_the_segment_quietly(libre, libre_server):
    _, state = libre_server
    state["status"] = 503
    assert await _translate() is None


@pytest.mark.anyio
async def test_blank_translation_is_rejected(libre, libre_server, caplog):
    _, state = libre_server
    state["translated"] = "   "
    assert await _translate() is None
    assert "no translatedText" in caplog.text


@pytest.mark.anyio
async def test_missing_translatedtext_field_is_rejected(libre, libre_server, caplog):
    _, state = libre_server
    state["translated"] = None
    assert await _translate() is None
    assert "no translatedText" in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (httpx.ConnectError("connection refused"), "ConnectError('connection refused')"),
        (httpx.ReadTimeout(""), "ReadTimeout('')"),
    ],
)
async def test_unreachable_or_slow_server_is_logged(libre, caplog, error, logged):
    def fail(request: httpx.Request) -> httpx.Response:
        error.request = request
        raise error

    previous = pg.shared_http_client
    pg.shared_http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    try:
        assert await _translate() is None
    finally:
        pg.shared_http_client = previous
    assert f"Translation via LibreTranslate failed for German: {logged}" in caplog.text


@pytest.mark.anyio
async def test_source_falls_back_to_auto_when_name_is_unknown(libre, libre_server):
    requests, _ = libre_server
    result = await LibreTranslateProvider().translate(
        provider_name="libretranslate",
        text="Hello world",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="Klingon",  # not a real ISO 639 language
        model=LIBRETRANSLATE_MODEL_ID,
        api_key=None,
    )
    # LibreTranslate's "auto" lets its own detector pick the language rather than mislabel
    # non-English text as English.
    assert result == "Hallo Welt"
    assert json.loads(requests[0].content)["source"] == "auto"


@pytest.mark.anyio
async def test_source_normalises_regional_codes(libre, libre_server):
    requests, _ = libre_server
    result = await LibreTranslateProvider().translate(
        provider_name="libretranslate",
        text="Olá mundo",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="pt-BR",
        model=LIBRETRANSLATE_MODEL_ID,
        api_key=None,
    )
    assert result == "Hallo Welt"
    assert json.loads(requests[0].content)["source"] == "pt"


@pytest.mark.anyio
async def test_source_accepts_a_two_letter_code_directly(libre, libre_server):
    requests, _ = libre_server
    result = await LibreTranslateProvider().translate(
        provider_name="libretranslate",
        text="Hola",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="es",
        model=LIBRETRANSLATE_MODEL_ID,
        api_key=None,
    )
    assert result == "Hallo Welt"
    assert json.loads(requests[0].content)["source"] == "es"


# ── Room engine list ───────────────────────────────────────────────────────


def _event() -> Event:
    return Event(slug="x", display_name="X", transcription_api_enabled=False)


def test_not_offered_when_unset(unset):
    catalog = engine_catalog(_event())
    assert "libretranslate" not in [e.provider for e in catalog.translation]


def test_offered_after_nllb_with_a_fixed_model(libre):
    from portal.translations.registry import NLLB_200_DISTILLED_600M

    catalog = engine_catalog(_event())
    providers = [e.provider for e in catalog.translation]
    assert providers[-1] == "libretranslate"
    engine = catalog.find("translation", "libretranslate")
    assert engine.model == LIBRETRANSLATE_MODEL_ID
    assert engine.label == "LibreTranslate"
    # New rooms still default to NLLB, which comes before LibreTranslate.
    plan = blank_plan(catalog)
    assert plan.translation_engine == "local"
    nllb = catalog.find("translation", "local")
    assert nllb.model == NLLB_200_DISTILLED_600M.id


# ── Routes ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def admin_room(setup_db):
    async with get_session() as s:
        admin = User(email="libre-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        s.add(admin)
        event = await create_event(s, slug="librecon", display_name="LibreCon")
        room = await create_room(s, event_id=event.id, display_name="Main Hall")
        await s.flush()
        cookie = {"user_token": create_user_token(user_id=admin.id, email=admin.email, is_admin=True)}
        return cookie, event.id, room.id


def _client(cookies: dict[str, str]) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", cookies=cookies)


async def _save_ai(cookie, event_id, room_id, **fields) -> Room:
    async with _client(cookie) as client:
        res = await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/edit",
            data={"form_section": "ai", "floor_translation_provider": "libretranslate", **fields},
            headers={"origin": "http://test"},
        )
    assert res.status_code == 303
    async with get_session() as s:
        return await s.get(Room, room_id)


@pytest.mark.anyio
async def test_models_endpoint_lists_libretranslate_only_when_configured(admin_room, monkeypatch, libre):
    cookie, _, _ = admin_room
    async with _client(cookie) as client:
        configured = (await client.get("/api/admin/providers/translation/models")).json()
        monkeypatch.setattr(settings, "libretranslate_base_url", "")
        cleared = (await client.get("/api/admin/providers/translation/models")).json()
    assert configured["libretranslate"] == [LIBRETRANSLATE_MODEL_ID]
    assert "openai" in configured
    assert "libretranslate" not in cleared
    # Make sure the server URL never leaks into the response.
    assert BASE_URL not in json.dumps(configured)


@pytest.mark.anyio
async def test_room_save_pins_the_registry_model(admin_room, libre):
    cookie, event_id, room_id = admin_room
    room = await _save_ai(cookie, event_id, room_id, floor_translation_model=LIBRETRANSLATE_MODEL_ID)
    assert (room.floor_translation_provider, room.floor_translation_model) == (
        "libretranslate",
        LIBRETRANSLATE_MODEL_ID,
    )

    # A bogus model is replaced with the fixed one.
    room = await _save_ai(cookie, event_id, room_id, floor_translation_model="not-a-model")
    assert room.floor_translation_model == LIBRETRANSLATE_MODEL_ID


@pytest.mark.anyio
async def test_room_form_cannot_choose_the_server_address(admin_room, libre, libre_server):
    requests, _ = libre_server
    cookie, event_id, room_id = admin_room
    await _save_ai(
        cookie,
        event_id,
        room_id,
        floor_translation_model=LIBRETRANSLATE_MODEL_ID,
        libretranslate_base_url="http://169.254.169.254:5000",
        floor_translation_base_url="http://169.254.169.254:5000",
        base_url="http://169.254.169.254:5000",
    )
    assert settings.libretranslate_base_url == BASE_URL
    assert await _translate() == "Hallo Welt"
    assert [r.url.host for r in requests] == ["libretranslate.internal"]


@pytest.mark.anyio
async def test_unset_server_hides_the_option_from_the_room_form(admin_room, unset):
    cookie, event_id, room_id = admin_room
    async with _client(cookie) as client:
        page = (await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")).text
    # The page must not render LibreTranslate as a selectable translation service.
    assert 'value="libretranslate"' not in page
    # And a save attempt that names it anyway doesn't stick.
    room = await _save_ai(cookie, event_id, room_id, floor_translation_model=LIBRETRANSLATE_MODEL_ID)
    assert room.floor_translation_provider != "libretranslate"


@pytest.mark.anyio
async def test_room_page_names_a_removed_libretranslate_plainly(admin_room, unset):
    cookie, event_id, room_id = admin_room
    async with get_session() as s:
        room = await s.get(Room, room_id)
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_translation_provider = "libretranslate"
        room.floor_translation_model = LIBRETRANSLATE_MODEL_ID
        s.add(RoomTranslationLanguage(room_id=room_id, language_code="de", language_name="German", enabled=True))
    async with _client(cookie) as client:
        page = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")
    assert page.status_code == 200
    # The generic select-fallback prints the provider name with underscores replaced.
    assert "Libretranslate — not available now" in page.text


# ── Worker ─────────────────────────────────────────────────────────────────


async def _floor_segment(provider: str) -> tuple[int, int]:
    async with get_session() as s:
        event = Event(slug=f"worker-{provider.replace('_', '-')}", display_name="Worker")
        s.add(event)
        await s.flush()
        room = Room(
            event_id=event.id,
            display_name="Hall",
            floor_language_code="en",
            floor_translation_enabled=True,
            floor_translation_provider=provider,
            floor_translation_model=LIBRETRANSLATE_MODEL_ID,
        )
        s.add(room)
        await s.flush()
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="de", language_name="German", enabled=True))
        segment = TranscriptSegment(room_id=room.id, text="Hello world", language_code="en")
        s.add(segment)
        await s.flush()
        return room.id, segment.id


@pytest.mark.anyio
async def test_worker_runs_libretranslate_without_an_event_key(setup_db, libre):
    room_id, segment_id = await _floor_segment("libretranslate")
    worker = TranslationWorker(AsyncMock())
    calls = []

    async def fake_call_llm(provider, model, api_key, text, lang_name, source_lang_name, **kwargs):
        calls.append((provider, model, api_key))
        return "Hallo Welt"

    with (
        patch.object(worker, "_call_llm", new=fake_call_llm),
        patch("portal.tts.worker.synthesize", new=AsyncMock(return_value=b"")),
        patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True),
        patch("portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock),
    ):
        await worker.handle_translation(room_id, segment_id, "Hello world", "floor", "uuid-1", 1)

    assert calls == [("libretranslate", LIBRETRANSLATE_MODEL_ID, None)]


@pytest.mark.anyio
async def test_worker_sends_libretranslate_segments_through_the_real_provider(setup_db, libre, libre_server):
    requests, _ = libre_server
    room_id, segment_id = await _floor_segment("libretranslate")
    worker = TranslationWorker(AsyncMock())
    with (
        patch("portal.tts.worker.synthesize", new=AsyncMock(return_value=b"")),
        patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True),
        patch("portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock) as sent,
    ):
        await worker.handle_translation(room_id, segment_id, "Hello world", "floor", "uuid-1", 1)

    assert [str(r.url) for r in requests] == [f"{BASE_URL}/translate"]
    assert "Hallo Welt" in [c.args[7] for c in sent.await_args_list]


@pytest.mark.anyio
async def test_worker_sends_the_server_key_when_configured(setup_db, libre_with_key, libre_server):
    requests, _ = libre_server
    room_id, segment_id = await _floor_segment("libretranslate")
    worker = TranslationWorker(AsyncMock())
    with (
        patch("portal.tts.worker.synthesize", new=AsyncMock(return_value=b"")),
        patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True),
        patch("portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock),
    ):
        await worker.handle_translation(room_id, segment_id, "Hello world", "floor", "uuid-1", 1)

    assert json.loads(requests[0].content)["api_key"] == "sk-libre"


# ── API v1 ─────────────────────────────────────────────────────────────────

ACCESS_TOKEN = "libretranslate-access-token"


@pytest.fixture
async def api_event(setup_db):
    from datetime import datetime, timedelta, timezone

    from portal.database import set_event_membership
    from portal.models import DeveloperAccount, OAuthClient, OAuthToken
    from portal.roles import EVENT_OWNER
    from portal.routers.oauth import hash_token

    async with get_session() as s:
        user = User(email="libre-api-owner@example.com", display_name="Owner", email_verified=True)
        s.add(user)
        event = await create_event(s, slug="apicon", display_name="APICon")
        await s.flush()
        await set_event_membership(s, user_id=user.id, event_id=event.id, role=EVENT_OWNER)
        account = DeveloperAccount(user_id=user.id, status="approved")
        s.add(account)
        await s.flush()
        oauth_client = OAuthClient(developer_account_id=account.id, client_id="libretranslate-client", name="API")
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
    async with _client({}) as client:
        return await client.put(
            "/api/v1/events/apicon/rooms/hall-1", json=body, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
        )


async def _api_room() -> Room:
    from sqlalchemy import select

    async with get_session() as s:
        return await s.scalar(select(Room).where(Room.eventyay_room_id == "hall-1"))


@pytest.mark.anyio
async def test_api_creates_libretranslate_rooms_with_the_pinned_model(api_event, libre):
    res = await _put_room({"name": "Hall", "translation_provider": "libretranslate"})
    assert res.status_code == 200, res.text
    room = await _api_room()
    assert (room.floor_translation_provider, room.floor_translation_model) == (
        "libretranslate",
        LIBRETRANSLATE_MODEL_ID,
    )


@pytest.mark.anyio
async def test_api_switch_from_a_cloud_model_pins_libretranslate(api_event, libre):
    await _put_room({"name": "Hall", "translation_provider": "openai", "translation_model": "gpt-4o-mini"})
    assert (await _put_room({"translation_provider": "libretranslate"})).status_code == 200
    room = await _api_room()
    assert (room.floor_translation_provider, room.floor_translation_model) == (
        "libretranslate",
        LIBRETRANSLATE_MODEL_ID,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("configured", "body", "detail"),
    [
        (False, {"translation_provider": "libretranslate"}, "LibreTranslate is not configured on this server"),
        (
            True,
            {"translation_provider": "libretranslate", "translation_model": "not-a-model"},
            "translation_model is not valid for LibreTranslate",
        ),
    ],
)
async def test_api_rejects_libretranslate_it_cannot_run(api_event, monkeypatch, libre, configured, body, detail):
    await _put_room({"name": "Hall", "translation_provider": "openai", "translation_model": "gpt-4o-mini"})
    if not configured:
        monkeypatch.setattr(settings, "libretranslate_base_url", "")
    res = await _put_room(body)
    assert res.status_code == 422
    assert res.json()["detail"] == detail
    room = await _api_room()
    assert (room.floor_translation_provider, room.floor_translation_model) == ("openai", "gpt-4o-mini")


@pytest.mark.anyio
async def test_api_turns_off_libretranslate_after_server_is_removed(api_event, monkeypatch, libre):
    await _put_room({"name": "Hall", "enable_translation": True, "translation_provider": "libretranslate"})
    monkeypatch.setattr(settings, "libretranslate_base_url", "")

    res = await _put_room({"enable_translation": True})
    assert res.status_code == 422
    res = await _put_room({"enable_translation": False, "translation_provider": "libretranslate"})
    assert res.status_code == 200, res.text
    assert (await _api_room()).floor_translation_enabled is False
