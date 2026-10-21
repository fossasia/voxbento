from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from portal.translations.prompts import build_interpretation_messages
from portal.translations.providers.base import TranslationProvider

logger = logging.getLogger(__name__)


class GeminiProvider(TranslationProvider):
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
        if not api_key:
            return None

        messages = build_interpretation_messages(
            source_language_name=source_lang_name,
            target_language_name=target_lang_name,
            text=text,
            persona=persona,
            style=style,
            vocabulary_entries=vocabulary_entries,
        )
        timeout = httpx.Timeout(10.0)

        import portal.globals as pg

        try:
            client = pg.get_http_client()
            res = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key},
                json={
                    "systemInstruction": {"parts": [{"text": messages[0]["content"]}]},
                    "contents": [{"parts": [{"text": messages[1]["content"]}]}],
                },
                timeout=timeout,
            )
            res.raise_for_status()
            return res.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            logger.error(f"Gemini translation failed for {target_lang_name}: {e}")
            return None
