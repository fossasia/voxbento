"""Self-hosted translation: an operator-configured OpenAI-compatible server (#1171)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select

import portal.globals as pg
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
from portal.models import (
    AIVocabularyEntry,
    DBBooth,
    DeveloperAccount,
    Event,
    OAuthClient,
    OAuthToken,
    Room,
    RoomTranslationLanguage,
    TranscriptSegment,
    User,
)
from portal.roles import EVENT_OWNER
from portal.room_setup import blank_plan, engine_catalog
from portal.routers.oauth import hash_token
from portal.translations.keys import get_translation_api_key
from portal.translations.providers.openai import SELF_HOSTED_MAX_TOKENS, OpenAIProvider, strip_reasoning
from portal.translations.worker import TranslationWorker

ORIGIN = {"origin": "http://test"}
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
    state = {"status": 200, "content": "Hallo Welt", "finish_reason": "stop"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # Error replies carry a usable body too, so only the status check can reject them.
        choice = {"message": {"content": f"  {state['content']}  "}, "finish_reason": state["finish_reason"]}
        return httpx.Response(state["status"], json={"choices": [choice]})

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
        "http://bad host/v1",
        "http://bad%20host/v1",
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
@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (httpx.ConnectError("connection refused"), "ConnectError('connection refused')"),
        # A timeout has an empty message, so the log names the error type instead.
        (httpx.ReadTimeout(""), "ReadTimeout('')"),
    ],
)
async def test_unreachable_or_slow_server_is_logged_by_provider_and_error(self_hosted, caplog, error, logged):
    def fail(request: httpx.Request) -> httpx.Response:
        error.request = request
        raise error

    previous = pg.shared_http_client
    pg.shared_http_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    try:
        assert await _translate() is None
    finally:
        pg.shared_http_client = previous
    assert f"Translation via self_hosted failed for German: {logged}" in caplog.text
    assert "OpenAI" not in caplog.text


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


def test_offered_after_nllb_with_the_first_model(self_hosted):
    catalog = engine_catalog(_event())
    assert [e.provider for e in catalog.translation] == ["local", "self_hosted"]
    assert catalog.find("translation", "self_hosted").model == "llama3.2"
    assert blank_plan(catalog).translation_engine == "local"


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


@pytest.mark.anyio
async def test_room_page_names_a_removed_self_hosted_service_plainly(admin_room, unset):
    cookie, event_id, room_id = admin_room
    async with get_session() as s:
        room = await s.get(Room, room_id)
        # AI languages always come with captions; the AI cards only render for such rooms.
        room.floor_transcription_enabled = True
        room.floor_translation_enabled = True
        room.floor_translation_provider = "self_hosted"
        room.floor_translation_model = "llama3.2"
        s.add(RoomTranslationLanguage(room_id=room_id, language_code="de", language_name="German", enabled=True))
    async with _client(cookie) as client:
        page = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/")
    assert page.status_code == 200
    assert "Self-hosted — not available now, choose another" in page.text
    assert "Self_hosted" not in page.text


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


@pytest.mark.anyio
async def test_worker_sends_self_hosted_segments_through_the_real_provider(setup_db, self_hosted, llm_server):
    requests, _ = llm_server
    room_id, segment_id = await _floor_segment("self_hosted")
    worker = TranslationWorker(AsyncMock())
    with (
        patch("portal.tts.worker.synthesize", new=AsyncMock(return_value=b"")),
        patch("portal.websockets.manager.tts_manager.has_listeners", return_value=True),
        patch("portal.websockets.manager.TTSConnectionManager.broadcast_bundle", new_callable=AsyncMock) as sent,
    ):
        await worker.handle_translation(room_id, segment_id, "Hello world", "floor", "uuid-1", 1)

    assert [str(r.url) for r in requests] == [f"{BASE_URL}/chat/completions"]
    assert "Hallo Welt" in [c.args[7] for c in sent.await_args_list]


# ── Settings edge cases ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://llm.internal:0/v1",
        "http://llm.internal:99999/v1",
        "http://llm.internal:abc/v1",
        "http://localhost:11434/v1/chat/completions",
        "http://localhost:11434/v1/chat/completions/",
    ],
)
def test_bad_ports_and_full_endpoint_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        _settings(url)


@pytest.mark.parametrize("url", ["http://[::1]:11434/v1", "http://ollama_server:11434/v1", "http://10.0.0.5:8080/v1"])
def test_ip_and_service_name_base_urls_are_accepted(url):
    assert _settings(url).self_hosted_llm_base_url == url


def test_model_names_longer_than_the_column_are_rejected():
    assert Settings(_env_file=None, self_hosted_llm_models="m" * 100).self_hosted_llm_model_list == ("m" * 100,)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, self_hosted_llm_models="ok," + "m" * 101)


@pytest.mark.parametrize(
    ("url", "models", "enabled"),
    [("", "", False), ("", MODELS, False), (BASE_URL, "", False), (BASE_URL, " , ", False), (BASE_URL, MODELS, True)],
)
def test_enabled_needs_both_url_and_models(url, models, enabled):
    s = Settings(_env_file=None, self_hosted_llm_base_url=url, self_hosted_llm_models=models)
    assert s.self_hosted_llm_enabled is enabled
    assert s.self_hosted_llm_allows("llama3.2") is enabled
    assert s.self_hosted_llm_model_or_default("missing") == ("llama3.2" if enabled else None)
    assert s.self_hosted_llm_model_or_default("qwen2.5:7b") == ("qwen2.5:7b" if enabled else None)
    assert s.self_hosted_llm_allows(None) is False


# ── Reasoning output ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("Hallo Welt", "Hallo Welt"),
        ("<think>The user wants German.</think>\n\nHallo Welt", "Hallo Welt"),
        ("<THINK>Upper case tags</THINK> Hallo Welt", "Hallo Welt"),
        # Templates that open the tag in the prompt return only the closing tag.
        ("German, formal.</think>Hallo Welt", "Hallo Welt"),
        ("İstanbul, İzmir.</think>Hallo Welt", "Hallo Welt"),
        ("<think>a</think><think>b</think>Hallo Welt", "Hallo Welt"),
        ("Hallo <think>choose the noun</think>Welt", "Hallo Welt"),
        ("<think>Cut off by the token limit", None),
        ("<think>a</think>Hallo<think>more", None),
        ("<think>only reasoning</think>", None),
        ("   ", None),
        ("", None),
        (None, None),
        (42, None),
    ],
)
def test_reasoning_is_never_shown_to_listeners(content, expected):
    assert strip_reasoning(content) == expected


@pytest.mark.anyio
async def test_reasoning_reply_is_stripped_and_output_is_capped(self_hosted, llm_server):
    requests, state = llm_server
    state["content"] = "<think>Formal German is needed.</think>Hallo Welt"
    assert await _translate() == "Hallo Welt"
    assert json.loads(requests[0].content)["max_tokens"] == SELF_HOSTED_MAX_TOKENS


@pytest.mark.anyio
async def test_reply_cut_off_at_the_token_limit_counts_as_no_translation(self_hosted, llm_server, caplog):
    _, state = llm_server
    state["finish_reason"] = "length"
    assert await _translate() is None
    assert "hit the 1024-token limit" in caplog.text


@pytest.mark.anyio
async def test_null_content_counts_as_no_translation(self_hosted):
    def null_content(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "reasoning_content": "..."}}]})

    previous = pg.shared_http_client
    pg.shared_http_client = httpx.AsyncClient(transport=httpx.MockTransport(null_content))
    try:
        assert await _translate() is None
    finally:
        pg.shared_http_client = previous


@pytest.mark.anyio
async def test_cloud_requests_are_unchanged(self_hosted, llm_server):
    requests, state = llm_server
    state["content"] = "<think>kept for cloud</think>Hallo"
    result = await OpenAIProvider().translate(
        provider_name="groq",
        text="Hello",
        target_lang_name="German",
        target_lang_code="de",
        source_lang_name="English",
        model="openai/gpt-oss-20b",
        api_key="sk-cloud",
    )
    assert result == "<think>kept for cloud</think>Hallo"
    assert "max_tokens" not in json.loads(requests[0].content)


# ── Booths ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def admin_booth(admin_room):
    cookie, event_id, room_id = admin_room
    async with get_session() as s:
        booth = await create_booth(s, event_id=event_id, room_id=room_id, language_code="fr", language_name="French")
        return cookie, event_id, room_id, booth.id


async def _booth_page(cookie, event_id, room_id, booth_id) -> str:
    async with _client(cookie) as client:
        page = await client.get(f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/")
    assert page.status_code == 200
    return page.text


async def _save_booth(cookie, event_id, room_id, booth_id, **fields):
    async with _client(cookie) as client:
        return await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/translation-settings",
            data={"translation_languages": ["de"], **fields},
            headers={"origin": "http://test"},
        )


async def _booth(booth_id: int) -> DBBooth:
    async with get_session() as s:
        return await s.get(DBBooth, booth_id)


@pytest.mark.anyio
async def test_booth_page_lists_self_hosted_models_only_when_configured(admin_booth, monkeypatch, self_hosted):
    page = await _booth_page(*admin_booth)
    assert '<option value="self_hosted" >Self-hosted LLM — ' in page
    assert '<option value="llama3.2" data-provider="self_hosted" >llama3.2</option>' in page
    assert '<option value="qwen2.5:7b" data-provider="self_hosted" >qwen2.5:7b</option>' in page

    monkeypatch.setattr(settings, "self_hosted_llm_base_url", "")
    page = await _booth_page(*admin_booth)
    assert "Self-hosted" not in page
    assert 'data-provider="self_hosted"' not in page


@pytest.mark.anyio
async def test_booth_saves_self_hosted_with_an_allowed_model(admin_booth, self_hosted):
    cookie, event_id, room_id, booth_id = admin_booth
    res = await _save_booth(
        cookie,
        event_id,
        room_id,
        booth_id,
        translation_enabled="true",
        translation_provider="self_hosted",
        translation_model="qwen2.5:7b",
    )
    assert res.status_code == 303
    booth = await _booth(booth_id)
    assert (booth.translation_provider, booth.translation_model) == ("self_hosted", "qwen2.5:7b")

    page = await _booth_page(cookie, event_id, room_id, booth_id)
    assert '<option value="self_hosted" selected>Self-hosted LLM — ' in page
    assert '<option value="qwen2.5:7b" data-provider="self_hosted" selected>qwen2.5:7b</option>' in page


@pytest.mark.anyio
async def test_booth_page_does_not_offer_a_self_hosted_model_the_server_dropped(admin_booth, monkeypatch, self_hosted):
    cookie, event_id, room_id, booth_id = admin_booth
    await _save_booth(
        cookie,
        event_id,
        room_id,
        booth_id,
        translation_enabled="true",
        translation_provider="self_hosted",
        translation_model="qwen2.5:7b",
    )
    monkeypatch.setattr(settings, "self_hosted_llm_models", "llama3.2")

    page = await _booth_page(cookie, event_id, room_id, booth_id)
    assert '<option value="llama3.2" data-provider="self_hosted" >llama3.2</option>' in page
    assert "qwen2.5:7b" not in page


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("configured", "model", "detail"),
    [
        (False, "llama3.2", "Self-hosted translation is not configured on this server"),
        (True, "http://169.254.169.254/v1", "That model is not allowed for self-hosted translation"),
        (True, "gpt-4o-mini", "That model is not allowed for self-hosted translation"),
    ],
)
async def test_booth_rejects_self_hosted_it_cannot_run(
    admin_booth, monkeypatch, self_hosted, configured, model, detail
):
    if not configured:
        monkeypatch.setattr(settings, "self_hosted_llm_models", "")
    cookie, event_id, room_id, booth_id = admin_booth
    res = await _save_booth(
        cookie,
        event_id,
        room_id,
        booth_id,
        translation_enabled="true",
        translation_provider="self_hosted",
        translation_model=model,
    )
    assert res.status_code == 400
    assert res.json()["detail"] == detail
    assert (await _booth(booth_id)).translation_provider != "self_hosted"


@pytest.mark.anyio
async def test_booth_left_on_a_removed_server_can_still_turn_translation_off(admin_booth, monkeypatch, self_hosted):
    cookie, event_id, room_id, booth_id = admin_booth
    await _save_booth(
        cookie,
        event_id,
        room_id,
        booth_id,
        translation_enabled="true",
        translation_provider="self_hosted",
        translation_model="llama3.2",
    )
    monkeypatch.setattr(settings, "self_hosted_llm_base_url", "")

    page = await _booth_page(cookie, event_id, room_id, booth_id)
    assert (
        '<option value="self_hosted" selected>Self-hosted LLM — not set up on this server, choose another</option>'
        in page
    )

    res = await _save_booth(cookie, event_id, room_id, booth_id, translation_provider="self_hosted")
    assert res.status_code == 303
    assert (await _booth(booth_id)).translation_enabled is False


# ── API v1 ─────────────────────────────────────────────────────────────────

ACCESS_TOKEN = "self-hosted-access-token"


@pytest.fixture
async def api_event(setup_db):
    async with get_session() as s:
        user = User(email="api-owner@example.com", display_name="Owner", email_verified=True)
        s.add(user)
        event = await create_event(s, slug="apicon", display_name="APICon")
        await s.flush()
        await set_event_membership(s, user_id=user.id, event_id=event.id, role=EVENT_OWNER)
        account = DeveloperAccount(user_id=user.id, status="approved")
        s.add(account)
        await s.flush()
        oauth_client = OAuthClient(developer_account_id=account.id, client_id="self-hosted-client", name="API")
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
    async with get_session() as s:
        return await s.scalar(select(Room).where(Room.eventyay_room_id == "hall-1"))


@pytest.mark.anyio
async def test_api_creates_and_updates_self_hosted_rooms(api_event, self_hosted):
    res = await _put_room({"name": "Hall", "translation_provider": "self_hosted"})
    assert res.status_code == 200, res.text
    room = await _api_room()
    assert (room.floor_translation_provider, room.floor_translation_model) == ("self_hosted", "llama3.2")

    assert (await _put_room({"translation_model": "qwen2.5:7b"})).status_code == 200
    assert (await _api_room()).floor_translation_model == "qwen2.5:7b"

    # Switching back keeps an allowed model; leaving the model out never resets it.
    assert (await _put_room({"translation_provider": "self_hosted"})).status_code == 200
    assert (await _api_room()).floor_translation_model == "qwen2.5:7b"


@pytest.mark.anyio
async def test_api_switch_from_a_cloud_model_gets_the_default(api_event, self_hosted):
    await _put_room({"name": "Hall", "translation_provider": "openai", "translation_model": "gpt-4o-mini"})
    assert (await _put_room({"translation_provider": "self_hosted"})).status_code == 200
    room = await _api_room()
    assert (room.floor_translation_provider, room.floor_translation_model) == ("self_hosted", "llama3.2")


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("configured", "body", "detail"),
    [
        (False, {"translation_provider": "self_hosted"}, "Self-hosted translation is not configured on this server"),
        (
            True,
            {"translation_provider": "self_hosted", "translation_model": "http://169.254.169.254/v1"},
            "translation_model is not allowed for self-hosted translation",
        ),
    ],
)
async def test_api_rejects_self_hosted_it_cannot_run(api_event, monkeypatch, self_hosted, configured, body, detail):
    await _put_room({"name": "Hall", "translation_provider": "openai", "translation_model": "gpt-4o-mini"})
    if not configured:
        monkeypatch.setattr(settings, "self_hosted_llm_base_url", "")
    res = await _put_room(body)
    assert res.status_code == 422
    assert res.json()["detail"] == detail
    room = await _api_room()
    assert (room.floor_translation_provider, room.floor_translation_model) == ("openai", "gpt-4o-mini")


@pytest.mark.anyio
async def test_api_model_only_update_on_a_self_hosted_room_is_checked(api_event, self_hosted):
    await _put_room({"name": "Hall", "translation_provider": "self_hosted"})
    res = await _put_room({"translation_model": "not-listed"})
    assert res.status_code == 422
    assert (await _api_room()).floor_translation_model == "llama3.2"


@pytest.mark.anyio
async def test_api_turns_off_self_hosted_after_the_server_is_removed(api_event, monkeypatch, self_hosted):
    await _put_room({"name": "Hall", "enable_translation": True, "translation_provider": "self_hosted"})
    monkeypatch.setattr(settings, "self_hosted_llm_base_url", "")

    res = await _put_room({"enable_translation": True})
    assert res.status_code == 422
    res = await _put_room({"enable_translation": False, "translation_provider": "self_hosted"})
    assert res.status_code == 200, res.text
    assert (await _api_room()).floor_translation_enabled is False


@pytest.mark.anyio
async def test_api_full_state_update_turns_off_a_room_whose_model_was_removed(api_event, monkeypatch, self_hosted):
    room_state = {"name": "Hall", "translation_provider": "self_hosted", "translation_model": "qwen2.5:7b"}
    assert (await _put_room({**room_state, "enable_translation": True})).status_code == 200
    room = await _api_room()
    assert (room.floor_translation_enabled, room.floor_translation_model) == (True, "qwen2.5:7b")
    monkeypatch.setattr(settings, "self_hosted_llm_models", "llama3.2")

    res = await _put_room({**room_state, "enable_translation": False})
    assert res.status_code == 200, res.text
    assert (await _api_room()).floor_translation_enabled is False


@pytest.mark.anyio
async def test_api_turning_translation_on_replaces_a_model_taken_off_the_list(api_event, monkeypatch, self_hosted):
    await _put_room({"name": "Hall", "translation_provider": "self_hosted", "translation_model": "qwen2.5:7b"})
    monkeypatch.setattr(settings, "self_hosted_llm_models", "llama3.2")

    assert (await _put_room({"name": "Main Hall"})).status_code == 200
    assert (await _api_room()).floor_translation_model == "qwen2.5:7b"
    assert (await _put_room({"enable_translation": True})).status_code == 200
    room = await _api_room()
    assert (room.floor_translation_enabled, room.floor_translation_model) == (True, "llama3.2")


@pytest.mark.anyio
async def test_api_requests_without_self_hosted_are_untouched(api_event, unset):
    res = await _put_room({"name": "Hall", "translation_provider": "openai", "translation_model": "gpt-4o"})
    assert res.status_code == 200
    assert (await _put_room({"enable_translation": True})).status_code == 200
    room = await _api_room()
    assert (room.floor_translation_provider, room.floor_translation_model) == ("openai", "gpt-4o")


# ── Room setup and the older room form ─────────────────────────────────────


@pytest.mark.anyio
async def test_setup_resave_replaces_a_model_taken_off_the_list(admin_room, monkeypatch, self_hosted):
    cookie, event_id, room_id = admin_room
    form = {
        "display_name": "Main Hall",
        "spoken_language": "en",
        "languages": ["de"],
        "delivery_de": "ai_text",
        "speech_engine": "local",
        "translation_engine": "self_hosted",
    }
    async with _client(cookie) as client:
        res = await client.post(f"/admin/events/{event_id}/rooms/{room_id}/setup", data=form, headers=ORIGIN)
        assert res.status_code == 303, res.text
        await _save_ai(cookie, event_id, room_id, floor_translation_model="qwen2.5:7b")

        monkeypatch.setattr(settings, "self_hosted_llm_models", "llama3.2")
        res = await client.post(f"/admin/events/{event_id}/rooms/{room_id}/setup", data=form, headers=ORIGIN)
        assert res.status_code == 303
    assert (await s_room(room_id)).floor_translation_model == "llama3.2"


@pytest.mark.anyio
async def test_setup_resave_keeps_an_allowed_model(admin_room, self_hosted):
    cookie, event_id, room_id = admin_room
    form = {
        "display_name": "Main Hall",
        "spoken_language": "en",
        "languages": ["de"],
        "delivery_de": "ai_text",
        "speech_engine": "local",
        "translation_engine": "self_hosted",
    }
    async with _client(cookie) as client:
        await client.post(f"/admin/events/{event_id}/rooms/{room_id}/setup", data=form, headers=ORIGIN)
        await _save_ai(cookie, event_id, room_id, floor_translation_model="qwen2.5:7b")
        await client.post(f"/admin/events/{event_id}/rooms/{room_id}/setup", data=form, headers=ORIGIN)
    assert (await s_room(room_id)).floor_translation_model == "qwen2.5:7b"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("model", "saved"),
    [("http://169.254.169.254/v1", "llama3.2"), ("qwen2.5:7b", "qwen2.5:7b")],
)
async def test_older_room_form_keeps_self_hosted_models_on_the_list(admin_room, self_hosted, model, saved):
    cookie, event_id, room_id = admin_room
    async with _client(cookie) as client:
        res = await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/edit",
            data={
                "form_section": "translation",
                "floor_translation_enabled": "on",
                "floor_translation_provider": "self_hosted",
                "floor_translation_model": model,
                "floor_translation_languages": ["de"],
            },
            headers=ORIGIN,
        )
    assert res.status_code == 303
    room = await s_room(room_id)
    assert (room.floor_translation_provider, room.floor_translation_model) == ("self_hosted", saved)


@pytest.mark.anyio
@pytest.mark.parametrize(("enabled", "status"), [("on", 400), ("", 303)])
async def test_older_room_form_turns_self_hosted_off_but_not_on_after_the_server_is_removed(
    admin_room, monkeypatch, self_hosted, enabled, status
):
    monkeypatch.setattr(settings, "self_hosted_llm_base_url", "")
    cookie, event_id, room_id = admin_room
    async with _client(cookie) as client:
        res = await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/edit",
            data={
                "form_section": "translation",
                "floor_translation_enabled": enabled,
                "floor_translation_provider": "self_hosted",
                "floor_translation_model": "llama3.2",
                "floor_translation_languages": ["de"],
            },
            headers=ORIGIN,
        )
    assert res.status_code == status
    room = await s_room(room_id)
    assert room.floor_translation_enabled is False
    if status == 400:
        assert res.json()["detail"] == "Self-hosted translation is not configured on this server"
    else:
        assert (room.floor_translation_provider, room.floor_translation_model) == ("self_hosted", None)


async def s_room(room_id: int) -> Room:
    async with get_session() as s:
        return await s.get(Room, room_id)


@pytest.mark.anyio
async def test_other_providers_pass_through_both_room_forms(admin_room, self_hosted):
    cookie, event_id, room_id = admin_room
    room = await _save_ai(cookie, event_id, room_id, floor_translation_provider="local", floor_translation_model="")
    assert (room.floor_translation_provider, room.floor_translation_model) == ("local", "nllb-200-distilled-600M")

    async with _client(cookie) as client:
        res = await client.post(
            f"/admin/events/{event_id}/rooms/{room_id}/edit",
            data={
                "form_section": "translation",
                "floor_translation_enabled": "on",
                "floor_translation_provider": "openai",
                "floor_translation_model": "gpt-4o",
                "floor_translation_languages": ["de"],
            },
            headers=ORIGIN,
        )
    assert res.status_code == 303
    room = await s_room(room_id)
    assert (room.floor_translation_provider, room.floor_translation_model) == ("openai", "gpt-4o")
