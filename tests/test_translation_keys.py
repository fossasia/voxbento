from __future__ import annotations

from unittest.mock import patch

import pytest

import portal.crypto
from portal.crypto import encrypt_val
from portal.models import Event
from portal.translations.constants import TranslationProviderEnum
from portal.translations.keys import get_translation_api_key

_TEST_KEY = "unit-test-encryption-key-at-least-32-chars"

# provider value -> Event encrypted attribute name
_PROVIDER_FIELDS = {
    TranslationProviderEnum.OPENAI.value: "encrypted_translation_openai_api_key",
    TranslationProviderEnum.OPENROUTER.value: "encrypted_openrouter_api_key",
    TranslationProviderEnum.GEMINI.value: "encrypted_gemini_api_key",
    TranslationProviderEnum.ANTHROPIC.value: "encrypted_anthropic_api_key",
    TranslationProviderEnum.GROQ.value: "encrypted_groq_api_key",
}


@pytest.fixture(autouse=True)
def reset_fernet():
    portal.crypto._fernet = None
    yield
    portal.crypto._fernet = None


@pytest.mark.parametrize("provider,field", list(_PROVIDER_FIELDS.items()))
def test_returns_decrypted_key_for_each_provider(provider, field):
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        plaintext = f"sk-{provider}-secret"
        event = Event(**{field: encrypt_val(plaintext)})
        assert get_translation_api_key(event, provider) == plaintext


@pytest.mark.parametrize("provider", list(_PROVIDER_FIELDS))
def test_returns_none_when_key_unset(provider):
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event()
        assert get_translation_api_key(event, provider) is None


def test_returns_none_for_provider_without_key_field():
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(encrypted_groq_api_key=encrypt_val("sk-groq-secret"))
        # LOCAL has no key field; an unmapped provider yields None.
        assert get_translation_api_key(event, TranslationProviderEnum.LOCAL.value) is None
        assert get_translation_api_key(event, "nonexistent-provider") is None


def test_only_requested_provider_key_is_returned():
    with patch("portal.config.settings.api_key_encryption_key", _TEST_KEY):
        event = Event(encrypted_groq_api_key=encrypt_val("groq-key"))
        assert get_translation_api_key(event, TranslationProviderEnum.GROQ.value) == "groq-key"
        assert get_translation_api_key(event, TranslationProviderEnum.OPENAI.value) is None
