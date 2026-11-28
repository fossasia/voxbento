from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

import httpx
import pycountry

from portal.translations.providers.base import TranslationProvider

logger = logging.getLogger(__name__)


def _redact(message: str, secret: str) -> str:
    """Blank out the subscription key in text headed for the log.

    httpx puts the offending value in the message when it cannot encode a header, so an
    exception string is not safe to log verbatim while the key is in scope.
    """
    return message.replace(secret, "***") if secret else message


class AzureTranslatorProvider(TranslationProvider):
    """Azure Translator Text API (v3) via Azure Cognitive Services.

    The request is not a chat completion: it is a direct machine-translation call.
    The worker passes the subscription key and region to the engine as a single
    ``"key|region"`` string (both are required for a regional Azure resource; a
    global resource is called without the region header). The two are split here
    and sent as the ``Ocp-Apim-Subscription-Key`` / ``Ocp-Apim-Subscription-Region``
    headers, so neither one is ever put in the URL or the request body.
    """

    ENDPOINT = "https://api.cognitive.microsofttranslator.com/translate"
    API_VERSION = "3.0"

    #: Azure uses ``zh-Hans``/``zh-Hant`` where ISO 639-1 has only ``zh``.
    _AZURE_CODES = {"zh": "zh-Hans"}

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
        source_lang_code: str = "",
    ) -> str | None:
        if not api_key:
            return None

        if "|" in api_key:
            subscription_key, region = api_key.split("|", 1)
        else:
            subscription_key, region = api_key, None

        from_lang = self._resolve_language_code(source_lang_code, source_lang_name)
        to_lang = self._resolve_language_code(target_lang_code, target_lang_name)
        if not from_lang or not to_lang:
            logger.error(
                "Azure Translator could not resolve a language code (source=%r, target=%r).",
                source_lang_code or source_lang_name,
                target_lang_code or target_lang_name,
            )
            return None

        # Azure Translator takes no prompt, so persona/style/glossary do not apply.
        headers = {
            "Ocp-Apim-Subscription-Key": subscription_key,
            "Content-Type": "application/json",
        }
        if region:
            headers["Ocp-Apim-Subscription-Region"] = region

        timeout = httpx.Timeout(10.0)

        import portal.globals as pg

        try:
            client = pg.get_http_client()
            res = await client.post(
                self.ENDPOINT,
                params={"api-version": self.API_VERSION, "from": from_lang, "to": to_lang},
                headers=headers,
                json=[{"text": text}],
                timeout=timeout,
            )
            res.raise_for_status()
            result = res.json()
            # [{"translations": [{"text": "...", "to": "..."}]}]
            if isinstance(result, list) and result and isinstance(result[0], dict):
                translations = result[0].get("translations")
                if isinstance(translations, list) and translations and isinstance(translations[0], dict):
                    translated = translations[0].get("text")
                    if isinstance(translated, str):
                        return translated.strip() or None
            logger.error("Azure Translator returned an unexpected response shape (status %s).", res.status_code)
            return None
        except httpx.HTTPStatusError as e:
            # Never log e.response.text: an error body is not guaranteed to be free of secrets.
            logger.error("Azure Translator failed for %s: HTTP %s.", target_lang_name, e.response.status_code)
            return None
        except Exception as e:
            logger.error(
                "Azure Translator translation failed for %s: %s: %s",
                target_lang_name,
                type(e).__name__,
                _redact(str(e), subscription_key),
            )
            return None

    def _resolve_language_code(self, lang_code: str, lang_name: str) -> str | None:
        """Resolve an Azure language code from a passed code or English name."""
        code = lang_code.strip().lower() if lang_code else ""
        if not code and lang_name:
            record = pycountry.languages.get(name=lang_name)
            if record is None:
                matches = [lang for lang in pycountry.languages if getattr(lang, "common_name", None) == lang_name]
                record = matches[0] if matches else None
            code = getattr(record, "alpha_2", "").lower() if record else ""
        if not code or not pycountry.languages.get(alpha_2=code):
            return None
        return self._AZURE_CODES.get(code, code)
