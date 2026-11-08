"""Self-hosted translation: an operator-configured OpenAI-compatible server (#1171)."""

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
from portal.database import configure, create_event, create_room, dispose, get_session, init_db
from portal.models import AIVocabularyEntry, Event, Room, RoomTranslationLanguage, TranscriptSegment, User
from portal.room_setup import engine_catalog
from portal.translations.keys import get_translation_api_key
from portal.translations.providers.openai import OpenAIProvider
from portal.translations.worker import TranslationWorker

BASE_URL = "http://llm.internal:11434/v1"
MODELS = "llama3.2,qwen2.5:7b"


@pytest.fixture
def self_hosted(monkeypatch):
    monkeypatch.setattr(settings, "self_hosted_llm_base_url", BASE_URL)
    monkeypatch.setattr(settings, "self_hosted_llm_models", MODELS)
    monkeypatch.setattr(settings, "self_hosted_llm_api_key", "")


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.setattr(settings, "self_hosted_llm_base_url", "")
    monkeypatch.setattr(settings, "self_hosted_llm_models", "")
    monkeypatch.setattr(settings, "self_hosted_llm_api_key", "")


@pytest.fixture
def llm_server():
    """A mocked OpenAI-compatible server that records each request."""
    requests: list[httpx.Request] = []
    state = {"status": 200, "content": "Hallo Welt"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": "down"})
        return httpx.Response(200, json={"choices": [{"message": {"content": f"  {state['content']}  "}}]})

    previous = pg.shared_http_client
    pg.shared_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    yield requests, state
    pg.shared_http_client = previous


async def _translate(model: str = "llama3.2", api_key: str | None = None, **extra) -> str | None:
    return await OpenAIProvider().translate(
        provider_name="self_hosted",
        text="Hello world",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="English",
        model=model,
        api_key=api_key,
        **extra,
    )


# ── Settings ───────────────────────────────────────────────────────────────


def _settings(url: str) -> Settings:
    return Settings(_env_file=None, self_hosted_llm_base_url=url)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("", ""),
        ("  ", ""),
        ("http://localhost:11434/v1/", "http://localhost:11434/v1"),
        ("https://llm.example.org/v1", "https://llm.example.org/v1"),
    ],
)
def test_base_url_is_normalized(url, expected):
    assert _settings(url).self_hosted_llm_base_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "ftp://llm.example.org/v1",
        "localhost:11434/v1",
        "http:///v1",
        "http://user:secret@llm.example.org/v1",
        "http://llm.example.org/v1?key=secret",
        "http://llm.example.org/v1#frag",
    ],
)
def test_unsafe_base_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        _settings(url)


def test_model_list_drops_blanks_and_duplicates():
    s = Settings(_env_file=None, self_hosted_llm_models=" llama3.2, ,qwen2.5:7b,llama3.2 ")
    assert s.self_hosted_llm_model_list == ("llama3.2", "qwen2.5:7b")
    assert Settings(_env_file=None, self_hosted_llm_models="").self_hosted_llm_model_list == ()


def test_api_key_is_hidden_from_settings_repr():
    s = Settings(_env_file=None, self_hosted_llm_api_key="sk-local-secret")
    assert s.self_hosted_llm_api_key == "sk-local-secret"
    assert "sk-local-secret" not in repr(s)


# ── Key lookup ─────────────────────────────────────────────────────────────


def test_key_comes_from_the_server_not_the_event(monkeypatch):
    event = Event(slug="x", display_name="X")
    monkeypatch.setattr(settings, "self_hosted_llm_api_key", "")
    assert get_translation_api_key(event, "self_hosted") is None
    monkeypatch.setattr(settings, "self_hosted_llm_api_key", "sk-local")
    assert get_translation_api_key(event, "self_hosted") == "sk-local"


# ── Provider ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_request_goes_to_the_configured_server_without_a_key(self_hosted, llm_server):
    requests, _ = llm_server
    entry = AIVocabularyEntry(event_id=1, source_term="booth", target_language="de", target_term="Kabine")

    result = await _translate(persona="An open source conference", style="Formal", vocabulary_entries=[entry])

    assert result == "Hallo Welt"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == f"{BASE_URL}/chat/completions"
    assert "authorization" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "llama3.2"
    system = body["messages"][0]["content"]
    assert "An open source conference" in system and "Formal" in system and "Kabine" in system


@pytest.mark.anyio
async def test_optional_key_is_sent_as_a_bearer_token(self_hosted, llm_server):
    requests, _ = llm_server
    assert await _translate(model="qwen2.5:7b", api_key="sk-local") == "Hallo Welt"
    assert requests[0].headers["authorization"] == "Bearer sk-local"


@pytest.mark.anyio
async def test_model_outside_the_allow_list_is_never_requested(self_hosted, llm_server, caplog):
    requests, _ = llm_server
    assert await _translate(model="some-other-model") is None
    assert requests == []
    assert "not configured" in caplog.text


@pytest.mark.anyio
async def test_nothing_is_requested_when_the_server_is_not_configured(unset, llm_server):
    requests, _ = llm_server
    assert await _translate() is None
    assert requests == []


@pytest.mark.anyio
async def test_server_errors_fail_the_segment_quietly(self_hosted, llm_server):
    _, state = llm_server
    state["status"] = 503
    assert await _translate() is None


@pytest.mark.anyio
async def test_unreachable_server_fails_the_segment_quietly(self_hosted):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    previous = pg.shared_http_client
    pg.shared_http_client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    try:
        assert await _translate() is None
    finally:
        pg.shared_http_client = previous


@pytest.mark.anyio
async def test_cloud_providers_still_need_their_key(self_hosted, llm_server):
    requests, _ = llm_server
    result = await OpenAIProvider().translate(
        provider_name="openai",
        text="Hello",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="English",
        model="gpt-4o-mini",
        api_key=None,
    )
    assert result is None
    assert requests == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider", "url"),
    [("groq", "https://api.groq.com/openai/v1/chat/completions"), ("no-such-provider", None)],
)
async def test_cloud_providers_keep_their_fixed_endpoints(self_hosted, llm_server, provider, url):
    requests, _ = llm_server
    result = await OpenAIProvider().translate(
        provider_name=provider,
        text="Hello",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="English",
        model="llama3.2",
        api_key="sk-cloud",
    )
    assert [str(r.url) for r in requests] == ([url] if url else [])
    assert result == ("Hallo Welt" if url else None)
    if url:
        assert requests[0].headers["authorization"] == "Bearer sk-cloud"


# ── Room engine list ───────────────────────────────────────────────────────


def _event() -> Event:
    return Event(slug="x", display_name="X", transcription_api_enabled=False)


def test_not_offered_when_unset(unset):
    catalog = engine_catalog(_event())
    assert [e.provider for e in catalog.translation] == ["local"]
    assert not any("Self-hosted" in reason for reason in catalog.unavailable)


def test_offered_before_nllb_with_the_first_model(self_hosted):
    catalog = engine_catalog(_event())
    assert [e.provider for e in catalog.translation] == ["self_hosted", "local"]
    assert catalog.find("translation", "self_hosted").model == "llama3.2"


def test_url_without_models_is_explained(monkeypatch, self_hosted):
    monkeypatch.setattr(settings, "self_hosted_llm_models", " , ")
    catalog = engine_catalog(_event())
    assert catalog.find("translation", "self_hosted") is None
    assert "Self-hosted LLM needs SELF_HOSTED_LLM_MODELS" in catalog.unavailable


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
        admin = User(email="llm-admin@example.com", display_name="Admin", is_admin=True, email_verified=True)
        s.add(admin)
        event = await create_event(s, slug="llmcon", display_name="LLMCon")
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
            data={"form_section": "ai", "floor_translation_provider": "self_hosted", **fields},
            headers={"origin": "http://test"},
        )
    assert res.status_code == 303
    async with get_session() as s:
        return await s.get(Room, room_id)


@pytest.mark.anyio
async def test_models_endpoint_lists_self_hosted_models_only_when_configured(admin_room, monkeypatch, self_hosted):
    cookie, _, _ = admin_room
    async with _client(cookie) as client:
        configured = (await client.get("/api/admin/providers/translation/models")).json()
        monkeypatch.setattr(settings, "self_hosted_llm_base_url", "")
        cleared = (await client.get("/api/admin/providers/translation/models")).json()
    assert configured["self_hosted"] == ["llama3.2", "qwen2.5:7b"]
    assert "openai" in configured
    assert "self_hosted" not in cleared
    assert BASE_URL not in json.dumps(configured)


@pytest.mark.anyio
async def test_room_keeps_only_allowed_models(admin_room, self_hosted):
    cookie, event_id, room_id = admin_room
    room = await _save_ai(cookie, event_id, room_id, floor_translation_model="qwen2.5:7b")
    assert (room.floor_translation_provider, room.floor_translation_model) == ("self_hosted", "qwen2.5:7b")

    room = await _save_ai(cookie, event_id, room_id, floor_translation_model="http://169.254.169.254/latest")
    assert room.floor_translation_model == "llama3.2"


@pytest.mark.anyio
async def test_room_form_cannot_choose_the_server_address(admin_room, self_hosted, llm_server):
    requests, _ = llm_server
    cookie, event_id, room_id = admin_room
    await _save_ai(
        cookie,
        event_id,
        room_id,
        floor_translation_model="llama3.2",
        self_hosted_llm_base_url="http://169.254.169.254/v1",
        floor_translation_base_url="http://169.254.169.254/v1",
        base_url="http://169.254.169.254/v1",
    )

    assert settings.self_hosted_llm_base_url == BASE_URL
    assert await _translate() == "Hallo Welt"
    assert [r.url.host for r in requests] == ["llm.internal"]


@pytest.mark.anyio
async def test_unset_server_hides_the_option_from_the_room_form(admin_room, unset):
    cookie, event_id, room_id = admin_room
    room = await _save_ai(cookie, event_id, room_id, floor_translation_model="llama3.2")
    assert room.floor_translation_provider != "self_hosted"


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
            floor_translation_model="llama3.2",
        )
        s.add(room)
        await s.flush()
        s.add(RoomTranslationLanguage(room_id=room.id, language_code="de", language_name="German", enabled=True))
        segment = TranscriptSegment(room_id=room.id, text="Hello world", language_code="en")
        s.add(segment)
        await s.flush()
        return room.id, segment.id


@pytest.mark.anyio
@pytest.mark.parametrize(("provider", "translated"), [("self_hosted", True), ("openai", False)])
async def test_worker_runs_self_hosted_without_an_event_key(setup_db, self_hosted, provider, translated):
    room_id, segment_id = await _floor_segment(provider)
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

    assert calls == ([("self_hosted", "llama3.2", None)] if translated else [])
