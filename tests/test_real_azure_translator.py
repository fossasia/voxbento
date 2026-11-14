"""Real calls to the Azure Translator Text API (#1203).

AZURE_TRANSLATOR_KEY=... AZURE_TRANSLATOR_REGION=eastus uv run pytest tests/test_real_azure_translator.py -v

Nothing is mocked. tests/test_azure_translator.py checks the provider against a mocked
endpoint, which cannot tell whether the header names, the api-version parameter or the
response shape match what Azure actually serves: a request built to the wrong spec passes
every mocked test. This file is the only place that finds that out.

Opt-in: it is skipped without a key, so it never runs in CI. A free F0 Translator resource
is enough. Set the region to the one the resource was created in, or leave it unset for a
global resource. Translator output is not stable across service versions, so the
assertions check that something in the target language came back, never an exact string.
"""

from __future__ import annotations

import os

import pytest

from portal.translations.providers.azure import AzureTranslatorProvider

KEY = os.environ.get("AZURE_TRANSLATOR_KEY", "")
REGION = os.environ.get("AZURE_TRANSLATOR_REGION", "")
CREDENTIAL = f"{KEY}|{REGION}" if REGION else KEY

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not KEY,
        reason="Real Azure Translator calls are opt-in (AZURE_TRANSLATOR_KEY=...)",
    ),
]


async def translate(text: str, target_code: str, target_name: str) -> str | None:
    return await AzureTranslatorProvider().translate(
        provider_name="azure",
        text=text,
        target_lang_name=target_name,
        target_lang_code=target_code,
        source_lang_name="English",
        source_lang_code="en",
        model="translator-text-v3",
        api_key=CREDENTIAL,
    )


async def test_azure_accepts_the_request_we_build():
    """The headers, the api-version parameter and the response shape are all as documented."""
    translated = await translate("Good morning, everyone.", "fr", "French")

    assert translated, "Azure returned nothing: the request shape or the credentials are wrong"
    assert translated.lower() != "good morning, everyone."


async def test_chinese_comes_back_in_chinese_script():
    """Azure rejects a bare ``zh``; this proves the zh-Hans mapping is both needed and correct."""
    translated = await translate("Good morning.", "zh", "Chinese")

    assert translated
    assert any("一" <= character <= "鿿" for character in translated), translated


async def test_a_rejected_key_is_handled_rather_than_raised():
    """A bad key must fail the segment quietly, the way the worker expects."""
    provider = AzureTranslatorProvider()
    assert (
        await provider.translate(
            provider_name="azure",
            text="Good morning.",
            target_lang_name="French",
            target_lang_code="fr",
            source_lang_name="English",
            source_lang_code="en",
            model="translator-text-v3",
            api_key=f"not-a-real-key|{REGION}" if REGION else "not-a-real-key",
        )
        is None
    )
