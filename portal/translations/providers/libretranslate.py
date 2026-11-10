"""LibreTranslate provider.

LibreTranslate is a self-hosted machine-translation server (https://libretranslate.com)
that speaks a small JSON API: ``POST /translate`` takes ``q``, ``source``, ``target``, and
returns ``{"translatedText": "..."}``. The operator sets the server URL (and, when the
instance requires it, the API key) in the environment, so an event owner never points
the server at a different address.

Like NLLB, LibreTranslate is sequence-to-sequence: it does not take a persona, style
or glossary, so those context fields are ignored here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, NotRequired, TypedDict

import httpx

from portal.config import settings
from portal.translations.providers.base import TranslationProvider

logger = logging.getLogger(__name__)


def libretranslate_endpoint() -> str | None:
    """The operator's ``/translate`` URL, or None when LibreTranslate isn't configured."""
    if not settings.libretranslate_enabled:
        return None
    return f"{settings.libretranslate_base_url}/translate"


def language_to_iso(language_name: str) -> str | None:
    """Best-effort ISO 639-1 code for a human-readable language name, or None.

    LibreTranslate takes ISO 639-1 (``en``, ``fr``, ``zh``). A caller that already has
    a two-letter code can pass that through here; everything else is resolved with pycountry.
    """
    if not language_name:
        return None
    import pycountry

    lookup = language_name.strip()
    if len(lookup) == 2 and lookup.isalpha():
        return lookup.lower()
    try:
        lang = pycountry.languages.lookup(lookup)
    except LookupError:
        return None
    return getattr(lang, "alpha_2", None)


class _LibreTranslateRequest(TypedDict):
    q: str
    source: str
    target: str
    format: str
    api_key: NotRequired[str]


class LibreTranslateProvider(TranslationProvider):
    """Translate through a self-hosted LibreTranslate server."""

    async def translate(
        self,
        provider_name: str,
        text: str,
        target_lang_name: str,
        target_lang_code: str,
        source_lang_name: str,
        model: str,
        api_key: str | None,
        persona: str | None = None,
        style: str | None = None,
        vocabulary_entries: Sequence[Any] = (),
    ) -> str | None:
        endpoint = libretranslate_endpoint()
        if not endpoint:
            logger.error("LibreTranslate translation is not configured")
            return None

        target = (target_lang_code or "").strip().lower()
        if not target:
            logger.error("LibreTranslate: missing target language code for %r", target_lang_name)
            return None
        source = language_to_iso(source_lang_name) or "en"

        payload: _LibreTranslateRequest = {
            "q": text,
            "source": source,
            "target": target,
            "format": "text",
        }
        if api_key:
            payload["api_key"] = api_key

        timeout = httpx.Timeout(10.0)

        import portal.globals as pg

        try:
            client = pg.get_http_client()
            res = await client.post(endpoint, json=payload, timeout=timeout)
            res.raise_for_status()
            body = res.json()
        except Exception as e:
            logger.error("Translation via LibreTranslate failed for %s: %r", target_lang_name, e)
            return None

        translated = body.get("translatedText") if isinstance(body, dict) else None
        if not isinstance(translated, str) or not translated.strip():
            logger.error("LibreTranslate returned no translatedText for %s", target_lang_name)
            return None
        return translated.strip()
