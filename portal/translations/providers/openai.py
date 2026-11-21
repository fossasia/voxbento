from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, NotRequired, TypedDict

import httpx

from portal.config import settings
from portal.translations.constants import OPENAI_COMPATIBLE_ENDPOINTS, TranslationProviderEnum
from portal.translations.prompts import build_interpretation_messages
from portal.translations.providers.base import TranslationProvider

logger = logging.getLogger(__name__)


#: Caption segments are short (the aggregator closes one at 50 words or 15 seconds), so this
#: leaves room for a reasoning model's thinking while bounding how long a run-on answer holds the server.
SELF_HOSTED_MAX_TOKENS = 1024


def self_hosted_endpoint(model: str) -> str | None:
    """The operator's chat-completions URL, or None when it isn't set or the model isn't allowed."""
    if not settings.self_hosted_llm_allows(model):
        return None
    return f"{settings.self_hosted_llm_base_url}/chat/completions"


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_CLOSE = re.compile(r"</think>", re.IGNORECASE)


def strip_reasoning(content: object) -> str | None:
    """The translation without a reasoning model's ``<think>`` sections.

    Some servers return the model's thinking inline. Listeners must never see it, so an answer
    that stops while still thinking, or holds nothing else, counts as no translation.
    """
    if not isinstance(content, str):
        return None
    answer = _THINK_BLOCK.sub("", content)
    # Some chat templates open the thinking section themselves, so only "</think>" comes back.
    # Matched on the original text: lowering it can change its length (e.g. "İ") and shift an index.
    answer = _THINK_CLOSE.split(answer)[-1]
    if "<think>" in answer.lower():
        return None
    return answer.strip() or None


class ChatRequest(TypedDict):
    model: str
    messages: list[dict[str, str]]
    max_tokens: NotRequired[int]


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
        self_hosted = provider_name == TranslationProviderEnum.SELF_HOSTED.value
        if self_hosted:
            endpoint = self_hosted_endpoint(model)
            if not endpoint:
                logger.error("Self-hosted translation is not configured for model %r", model)
                return None
        else:
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

        payload: ChatRequest = {"model": model, "messages": messages}
        if self_hosted:
            payload["max_tokens"] = SELF_HOSTED_MAX_TOKENS

        try:
            client = pg.get_http_client()
            res = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json=payload,
                timeout=timeout,
            )
            res.raise_for_status()
            choice = res.json()["choices"][0]
            if not self_hosted:
                return choice["message"]["content"].strip()
            # A reply cut off at max_tokens is incomplete, so it counts as no translation.
            if choice.get("finish_reason") == "length":
                logger.error(
                    "Self-hosted translation for %s hit the %d-token limit", target_lang_name, SELF_HOSTED_MAX_TOKENS
                )
                return None
            return strip_reasoning(choice["message"]["content"])
        except Exception as e:
            logger.error("Translation via %s failed for %s: %r", provider_name, target_lang_name, e)
            return None
