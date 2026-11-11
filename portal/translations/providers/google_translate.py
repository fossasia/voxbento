"""Google Cloud Translation (Basic v2) as a machine-translation engine (#1202).

The API takes no prompt, so a room's persona, style and glossary do not apply here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import httpx

from portal.translations.engine import (
    EngineKind,
    Language,
    TranslationContext,
    TranslationEngine,
    TranslationRows,
    translate_each,
)

logger = logging.getLogger(__name__)

GOOGLE_TRANSLATE_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"


class GoogleTranslateEngine(TranslationEngine):
    kind = EngineKind.MT_API

    async def translate_batch(
        self,
        source: Language,
        targets: Sequence[Language],
        texts: Sequence[str],
        context: TranslationContext,
        *,
        model: str,
        api_key: str | None = None,
    ) -> TranslationRows:
        if not api_key:
            return [[None] * len(targets) for _ in texts]

        async def translate_one(text: str, target: Language) -> str | None:
            return await self._translate(source, target, text, model, api_key)

        return await translate_each(translate_one, targets, texts)

    async def _translate(self, source: Language, target: Language, text: str, model: str, api_key: str) -> str | None:
        payload = {"q": [text], "target": target.code, "format": "text", "model": model}
        if source.code:  # Without a source language Google detects it from the text.
            payload["source"] = source.code

        import portal.globals as pg

        # The key travels in a header, never the URL: httpx logs URLs and puts them in its errors.
        # For the same reason the handlers below log a status or a type, never the exception text.
        try:
            res = await pg.get_http_client().post(
                GOOGLE_TRANSLATE_ENDPOINT,
                headers={"X-Goog-Api-Key": api_key},
                json=payload,
                timeout=httpx.Timeout(10.0),
            )
            res.raise_for_status()
            return res.json()["data"]["translations"][0]["translatedText"].strip()
        except httpx.HTTPStatusError as e:
            logger.error("Google Cloud Translation returned HTTP %s for %s", e.response.status_code, target.code)
        except Exception as e:
            logger.error("Google Cloud Translation failed for %s: %s", target.code, type(e).__name__)
        return None
