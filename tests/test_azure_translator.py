"""Azure Translator as a translation provider (#1203)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import portal.crypto
from portal.crypto import encrypt_val
from portal.models import Event
from portal.room_setup import engine_catalog
from portal.translations.constants import TRANSLATION_MODELS, TranslationProviderEnum
from portal.translations.engine import EngineKind, Language, TranslationContext
from portal.translations.keys import get_azure_translator_region, get_translation_api_key
from portal.translations.providers.azure import AzureTranslatorProvider
from portal.translations.worker import ENGINES, TranslationWorker

pytestmark = pytest.mark.anyio

_TEST_KEY = "unit-test-encryption-key-at-least-32-chars"
AZURE = TranslationProviderEnum.AZURE.value
SUBSCRIPTION_KEY = "azure-subscription-key-0123456789"
REGION = "eastus"


@pytest.fixture(autouse=True)
def reset_fernet():
    portal.crypto._fernet = None
    yield
    portal.crypto._fernet = None


def ok_response(text: str = "Bonjour", to: str = "fr") -> httpx.Response:
    return httpx.Response(
        200,
        json=[{"translations": [{"text": text, "to": to}]}],
        request=httpx.Request("POST", AzureTranslatorProvider.ENDPOINT),
    )


def mock_client(response: httpx.Response | None = None, *, side_effect=None):
    """A stand-in for the shared httpx client whose ``post`` returns ``response``."""
    client = MagicMock()
    client.post = AsyncMock(return_value=response, side_effect=side_effect)
    return client


async def translate(client, **overrides) -> str | None:
    """Call the provider with the usual arguments, routing HTTP through ``client``."""
    kwargs = {
        "provider_name": AZURE,
        "text": "Hello",
        "target_lang_name": "French",
        "target_lang_code": "fr",
        "source_lang_name": "English",
        "source_lang_code": "en",
        "model": "translator-text-v3",
        "api_key": f"{SUBSCRIPTION_KEY}|{REGION}",
    }
    kwargs.update(overrides)
    with patch("portal.globals.get_http_client", return_value=client):
        return await AzureTranslatorProvider().translate(**kwargs)


# --- the happy path ------------------------------------------------------------------------


async def test_returns_the_translated_text():
    client = mock_client(ok_response())
    assert await translate(client) == "Bonjour"


async def test_calls_the_v3_translate_endpoint_with_the_language_pair():
    client = mock_client(ok_response())
    await translate(client)

    args, kwargs = client.post.await_args
    assert args[0] == "https://api.cognitive.microsofttranslator.com/translate"
    assert kwargs["params"] == {"api-version": "3.0", "from": "en", "to": "fr"}
    assert kwargs["json"] == [{"text": "Hello"}]


async def test_sends_the_key_and_region_as_headers():
    client = mock_client(ok_response())
    await translate(client)

    headers = client.post.await_args.kwargs["headers"]
    assert headers["Ocp-Apim-Subscription-Key"] == SUBSCRIPTION_KEY
    assert headers["Ocp-Apim-Subscription-Region"] == REGION
    assert headers["Content-Type"] == "application/json"


async def test_omits_the_region_header_for_a_global_resource():
    """A key with no region is a global Azure resource, which takes no region header."""
    client = mock_client(ok_response())
    assert await translate(client, api_key=SUBSCRIPTION_KEY) == "Bonjour"

    headers = client.post.await_args.kwargs["headers"]
    assert headers["Ocp-Apim-Subscription-Key"] == SUBSCRIPTION_KEY
    assert "Ocp-Apim-Subscription-Region" not in headers


async def test_strips_surrounding_whitespace():
    client = mock_client(ok_response(text="  Bonjour  "))
    assert await translate(client) == "Bonjour"


async def test_whitespace_only_translation_is_treated_as_no_translation():
    client = mock_client(ok_response(text="   "))
    assert await translate(client) is None


# --- languages -----------------------------------------------------------------------------


async def test_falls_back_to_the_english_language_name_when_no_code_is_given():
    client = mock_client(ok_response())
    await translate(client, source_lang_code="", target_lang_code="")

    assert client.post.await_args.kwargs["params"] == {"api-version": "3.0", "from": "en", "to": "fr"}


async def test_maps_chinese_to_the_script_azure_expects():
    """Azure rejects a bare ``zh``; it wants ``zh-Hans``."""
    client = mock_client(ok_response(text="你好", to="zh-Hans"))
    assert await translate(client, target_lang_name="Chinese", target_lang_code="zh") == "你好"

    assert client.post.await_args.kwargs["params"]["to"] == "zh-Hans"


@pytest.mark.parametrize("field", ["source", "target"])
async def test_an_unresolvable_language_is_not_sent_to_azure(field):
    client = mock_client(ok_response())
    overrides = {f"{field}_lang_code": "xx", f"{field}_lang_name": "Nonexistent"}

    assert await translate(client, **overrides) is None
    client.post.assert_not_awaited()


# --- failures ------------------------------------------------------------------------------


async def test_no_key_means_no_request():
    client = mock_client(ok_response())
    assert await translate(client, api_key=None) is None
    client.post.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 503])
async def test_an_error_status_yields_no_translation(status):
    response = httpx.Response(status, text="denied", request=httpx.Request("POST", AzureTranslatorProvider.ENDPOINT))
    assert await translate(mock_client(response)) is None


async def test_a_timeout_yields_no_translation():
    client = mock_client(side_effect=httpx.TimeoutException("timed out"))
    assert await translate(client) is None


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        [{}],
        [{"translations": []}],
        [{"translations": [{}]}],
        [{"translations": [{"text": None}]}],
        {"error": {"code": 401000, "message": "denied"}},
    ],
)
async def test_an_unexpected_response_shape_yields_no_translation(payload):
    response = httpx.Response(200, json=payload, request=httpx.Request("POST", AzureTranslatorProvider.ENDPOINT))
    assert await translate(mock_client(response)) is None


async def test_a_body_that_is_not_json_yields_no_translation():
    response = httpx.Response(
        200, text="<html>no</html>", request=httpx.Request("POST", AzureTranslatorProvider.ENDPOINT)
    )
    assert await translate(mock_client(response)) is None


# --- the key never leaks -------------------------------------------------------------------


async def test_the_key_is_never_logged_when_azure_rejects_it(caplog):
    """An Azure error body can quote the request; the key must not reach the log through it."""
    response = httpx.Response(
        401,
        text=f'{{"error": {{"message": "key {SUBSCRIPTION_KEY} in region {REGION} is invalid"}}}}',
        request=httpx.Request("POST", AzureTranslatorProvider.ENDPOINT),
    )
    with caplog.at_level(logging.DEBUG):
        assert await translate(mock_client(response)) is None

    assert SUBSCRIPTION_KEY not in caplog.text


async def test_the_key_is_never_logged_when_the_request_itself_fails(caplog):
    client = mock_client(side_effect=httpx.ConnectError(f"cannot reach host with key {SUBSCRIPTION_KEY}"))
    with caplog.at_level(logging.DEBUG):
        assert await translate(client) is None

    assert SUBSCRIPTION_KEY not in caplog.text


async def test_the_key_travels_in_a_header_not_the_url_or_the_body():
    client = mock_client(ok_response())
    await translate(client)

    args, kwargs = client.post.await_args
    assert SUBSCRIPTION_KEY not in args[0]
    assert SUBSCRIPTION_KEY not in str(kwargs["params"])
    assert SUBSCRIPTION_KEY not in str(kwargs["json"])


# --- key storage ---------------------------------------------------------------------------


def test_the_key_and_region_survive_a_round_trip_through_the_database():
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(
            encrypted_azure_translator_api_key=encrypt_val(SUBSCRIPTION_KEY),
            encrypted_azure_translator_region=encrypt_val(REGION),
        )
        assert get_translation_api_key(event, AZURE) == SUBSCRIPTION_KEY
        assert get_azure_translator_region(event) == REGION


def test_the_key_and_region_are_encrypted_at_rest():
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(
            encrypted_azure_translator_api_key=encrypt_val(SUBSCRIPTION_KEY),
            encrypted_azure_translator_region=encrypt_val(REGION),
        )
        assert SUBSCRIPTION_KEY not in event.encrypted_azure_translator_api_key
        assert REGION not in event.encrypted_azure_translator_region


def test_an_event_without_azure_credentials_has_none():
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event()
        assert get_translation_api_key(event, AZURE) is None
        assert get_azure_translator_region(event) is None


# --- the worker's glue ---------------------------------------------------------------------


def test_the_worker_pairs_the_key_with_its_region():
    worker = TranslationWorker(broadcast_callback=None)
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(
            encrypted_azure_translator_api_key=encrypt_val(SUBSCRIPTION_KEY),
            encrypted_azure_translator_region=encrypt_val(REGION),
        )
        assert worker._get_translation_api_key(event, AZURE) == f"{SUBSCRIPTION_KEY}|{REGION}"


def test_the_worker_warns_but_still_translates_when_no_region_is_set(caplog):
    worker = TranslationWorker(broadcast_callback=None)
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(encrypted_azure_translator_api_key=encrypt_val(SUBSCRIPTION_KEY))
        with caplog.at_level(logging.WARNING):
            assert worker._get_translation_api_key(event, AZURE) == SUBSCRIPTION_KEY

    assert "region" in caplog.text.lower()
    assert SUBSCRIPTION_KEY not in caplog.text


def test_one_events_credentials_never_reach_another():
    worker = TranslationWorker(broadcast_callback=None)
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        first = Event(
            encrypted_azure_translator_api_key=encrypt_val("key-one"),
            encrypted_azure_translator_region=encrypt_val("eastus"),
        )
        second = Event(
            encrypted_azure_translator_api_key=encrypt_val("key-two"),
            encrypted_azure_translator_region=encrypt_val("westeurope"),
        )
        assert worker._get_translation_api_key(first, AZURE) == "key-one|eastus"
        assert worker._get_translation_api_key(second, AZURE) == "key-two|westeurope"


def test_azure_credentials_are_not_handed_to_another_provider():
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(encrypted_azure_translator_api_key=encrypt_val(SUBSCRIPTION_KEY))
        assert get_translation_api_key(event, TranslationProviderEnum.OPENAI.value) is None
        assert get_translation_api_key(event, TranslationProviderEnum.GEMINI.value) is None


# --- registration --------------------------------------------------------------------------


def test_azure_is_registered_as_a_machine_translation_engine():
    assert ENGINES[AZURE].kind is EngineKind.MT_API


def test_an_event_with_an_azure_key_can_choose_azure():
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(
            slug="x",
            display_name="X",
            transcription_api_enabled=False,
            encrypted_azure_translator_api_key=encrypt_val(SUBSCRIPTION_KEY),
            encrypted_azure_translator_region=encrypt_val(REGION),
        )
        catalog = engine_catalog(event)

    assert AZURE in [engine.provider for engine in catalog.translation]
    assert "Azure Translator" not in catalog.locked


def test_an_event_without_an_azure_key_cannot_choose_azure():
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        catalog = engine_catalog(Event(slug="x", display_name="X", transcription_api_enabled=False))

    assert AZURE not in [engine.provider for engine in catalog.translation]
    assert "Azure Translator" in catalog.locked


async def test_a_room_translates_through_azure_with_the_events_credentials():
    """The whole chain: the event's stored key and region reach Azure as headers."""
    worker = TranslationWorker(broadcast_callback=None)
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(
            encrypted_azure_translator_api_key=encrypt_val(SUBSCRIPTION_KEY),
            encrypted_azure_translator_region=encrypt_val(REGION),
        )
        api_key = worker._get_translation_api_key(event, AZURE)

    client = mock_client(ok_response())
    with patch("portal.globals.get_http_client", return_value=client):
        rows = await ENGINES[AZURE].translate_batch(
            Language(code="en", name="English"),
            [Language(code="fr", name="French")],
            ["Hello"],
            TranslationContext(persona="Formal", style="Concise"),
            model="translator-text-v3",
            api_key=api_key,
        )

    assert rows == [["Bonjour"]]
    headers = client.post.await_args.kwargs["headers"]
    assert headers["Ocp-Apim-Subscription-Key"] == SUBSCRIPTION_KEY
    assert headers["Ocp-Apim-Subscription-Region"] == REGION


def test_azure_offers_a_model_to_choose():
    assert TRANSLATION_MODELS[AZURE] == ["translator-text-v3"]
