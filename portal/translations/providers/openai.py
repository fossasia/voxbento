from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx

from portal.translations.constants import OPENAI_COMPATIBLE_ENDPOINTS
from portal.translations.prompts import build_interpretation_messages
from portal.translations.providers.base import TranslationProvider

logger = logging.getLogger(__name__)


class OpenAIProvider(TranslationProvider):
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

        endpoint = OPENAI_COMPATIBLE_ENDPOINTS.get(provider_name)
        if not endpoint:
            logger.error(f"Endpoint not found for provider {provider_name}")
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
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": messages,
                },
                timeout=timeout,
            )
            res.raise_for_status()
            return res.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error(f"OpenAI translation failed for {target_lang_name}: {e}")
            return None
