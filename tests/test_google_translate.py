"""Google Cloud Translation as an engine (#1202), with HTTP mocked."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import portal.crypto
import portal.globals as pg
from portal.crypto import encrypt_val
from portal.models import Event
from portal.room_setup import engine_catalog
from portal.translations.constants import TRANSLATION_MODELS, TranslationProviderEnum
from portal.translations.engine import EngineKind, Language, TranslationContext
from portal.translations.keys import get_translation_api_key
from portal.translations.providers.google_translate import GOOGLE_TRANSLATE_ENDPOINT, GoogleTranslateEngine
from portal.translations.worker import ENGINES, TranslationWorker

pytestmark = pytest.mark.anyio

PROVIDER = TranslationProviderEnum.GOOGLE_TRANSLATE.value
MODEL = TRANSLATION_MODELS[PROVIDER][0]
SECRET = "AIza-very-secret-key"
ENGLISH = Language("en", "English")
FRENCH = Language("fr", "French")
GERMAN = Language("de", "German")


def _answer(text: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"data": {"translations": [{"translatedText": text}]}}
    return response


@pytest.fixture
def http(monkeypatch):
    client = MagicMock()
    client.is_closed = False
    client.post = AsyncMock(return_value=_answer("Bonjour"))
    monkeypatch.setattr(pg, "shared_http_client", client)
    return client


async def _translate(source=ENGLISH, target=FRENCH, context=TranslationContext(), api_key=SECRET):
    return await GoogleTranslateEngine().translate_text(source, target, "Hello", context, model=MODEL, api_key=api_key)


async def test_sends_the_languages_and_text_with_the_key_in_a_header(http):
    assert await _translate() == "Bonjour"

    http.post.assert_awaited_once()
    args, kwargs = http.post.call_args
    assert args == (GOOGLE_TRANSLATE_ENDPOINT,)
    assert kwargs["headers"] == {"X-Goog-Api-Key": SECRET}
    assert kwargs["json"] == {"q": ["Hello"], "target": "fr", "source": "en", "format": "text", "model": MODEL}


async def test_the_key_appears_only_in_the_header(http):
    await _translate()

    args, kwargs = http.post.call_args
    assert SECRET not in args[0]
    assert "params" not in kwargs
    assert SECRET not in str(kwargs["json"])


async def test_lets_google_detect_an_unknown_source_language(http):
    await _translate(source=Language("", "English"))

    assert "source" not in http.post.call_args.kwargs["json"]


async def test_a_room_prompt_is_not_sent(http):
    await _translate(context=TranslationContext(persona="Formal interpreter", style="Concise"))

    assert "Formal interpreter" not in str(http.post.call_args)
    assert "Concise" not in str(http.post.call_args)


async def test_no_key_means_no_request(http):
    assert await _translate(api_key=None) is None

    http.post.assert_not_awaited()


async def test_one_failed_language_does_not_cost_the_others(http):
    failed = MagicMock()
    failed.raise_for_status.side_effect = httpx.HTTPStatusError(
        "bad language", request=MagicMock(), response=MagicMock(status_code=400)
    )
    http.post.side_effect = [_answer("Bonjour"), failed]

    rows = await GoogleTranslateEngine().translate_batch(
        ENGLISH, [FRENCH, GERMAN], ["Hello"], TranslationContext(), model=MODEL, api_key=SECRET
    )

    assert rows == [["Bonjour", None]]


async def test_an_empty_translation_is_not_a_failure(http):
    http.post.return_value = _answer("  ")

    assert await _translate() == ""


async def test_an_error_response_logs_the_status_but_not_the_key_or_body(http, caplog):
    response = MagicMock(status_code=403, text=f"rejected {SECRET}")
    failed = MagicMock()
    failed.raise_for_status.side_effect = httpx.HTTPStatusError(
        f"rejected {SECRET}", request=MagicMock(), response=response
    )
    http.post.return_value = failed

    with caplog.at_level(logging.DEBUG):
        assert await _translate() is None

    assert "403" in caplog.text
    assert SECRET not in caplog.text


@pytest.mark.parametrize(
    "error", [httpx.ConnectError(f"cannot reach ?key={SECRET}"), RuntimeError(f"unexpected {SECRET}")]
)
async def test_a_transport_error_never_logs_its_message(http, caplog, error):
    http.post.side_effect = error

    with caplog.at_level(logging.DEBUG):
        assert await _translate() is None

    assert type(error).__name__ in caplog.text
    assert SECRET not in caplog.text


async def test_a_malformed_response_is_a_failed_translation(http):
    http.post.return_value.json.return_value = {"data": {}}

    assert await _translate() is None


def test_is_registered_as_a_machine_translation_engine():
    assert isinstance(ENGINES[PROVIDER], GoogleTranslateEngine)
    assert ENGINES[PROVIDER].kind is EngineKind.MT_API


async def test_a_room_can_use_it_with_the_event_key(http):
    """The issue's acceptance path: an encrypted event key unlocks the service and reaches the request header."""
    portal.crypto._fernet = None
    try:
        with patch("portal.config.settings.api_key_encryption_key", "unit-test-encryption-key-at-least-32-chars"):
            event = Event(encrypted_google_translate_api_key=encrypt_val(SECRET), transcription_api_enabled=False)

            offered = {engine.provider: engine.model for engine in engine_catalog(event).translation}
            key = get_translation_api_key(event, PROVIDER)
    finally:
        portal.crypto._fernet = None

    assert offered[PROVIDER] == MODEL
    assert key == SECRET

    translated = await TranslationWorker(SimpleNamespace())._call_llm(
        PROVIDER, MODEL, key, "Hello", "French", target_lang_code="fr", source_language_code="en"
    )

    assert translated == "Bonjour"
    assert http.post.call_args.kwargs["headers"] == {"X-Goog-Api-Key": SECRET}


def test_the_service_stays_locked_without_a_key():
    catalog = engine_catalog(Event(transcription_api_enabled=False))

    assert PROVIDER not in {engine.provider for engine in catalog.translation}
    assert "Google Cloud Translation" in catalog.locked_translation
