from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from portal.meeting_analysis.constants import (
    DEFAULT_RETRY_WAIT_SECONDS,
    MAX_RETRY_WAIT_SECONDS,
    PROVIDER_LABELS,
    RATE_LIMIT_RETRIES,
    REQUEST_TIMEOUT_SECONDS,
)
from portal.meeting_analysis.schema import AnalysisError, UnreadableAnswer


@dataclass(frozen=True)
class Completion:
    text: str
    tokens_used: int | None = None


class AnalysisProvider(ABC):
    name: str

    @abstractmethod
    async def complete(self, *, system: str, user: str, model: str, api_key: str | None) -> Completion: ...

    @property
    def label(self) -> str:
        return PROVIDER_LABELS.get(self.name, self.name)

    async def send(self, url: str, *, headers: dict[str, str], body: dict) -> httpx.Response:
        import portal.globals as pg

        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                res = await pg.get_http_client().post(
                    url, headers=headers, json=body, timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
                )
            except httpx.TimeoutException as exc:
                raise AnalysisError(f"{self.label} took too long to answer. Try again.") from exc
            except httpx.HTTPError as exc:
                raise AnalysisError(f"Couldn't reach {self.label}.") from exc
            if res.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
                return res
            await asyncio.sleep(retry_wait(res))
        return res

    async def post(self, url: str, *, headers: dict[str, str], body: dict) -> dict:
        return self.read(await self.send(url, headers=headers, body=body))

    def read(self, res: httpx.Response) -> dict:
        if res.status_code >= 400 and "json_validate_failed" in res.text:
            raise UnreadableAnswer(f"{self.label} returned a report that couldn't be read.")
        if res.status_code >= 400:
            raise AnalysisError(self.refusal(res))
        try:
            return res.json()
        except ValueError as exc:
            raise AnalysisError(f"{self.label} returned an answer that couldn't be read.") from exc

    def refusal(self, res: httpx.Response) -> str:
        status = res.status_code
        detail = res.text.lower()
        if status in (401, 403):
            return f"{self.label} rejected the API key. Check it on the API keys page."
        if status == 413 or any(word in detail for word in TOO_LONG_HINTS):
            return (
                f"The transcript is too long for this {self.label} model. Choose a model with a larger context window."
            )
        if status == 404 or "model_not_found" in detail or "does not exist" in detail:
            return f"{self.label} doesn't offer this model. Check the model name on the API keys page."
        if status == 429:
            return f"{self.label} is limiting requests on this API key. Try again in a few minutes."
        return f"{self.label} refused the request (HTTP {status})."


TOO_LONG_HINTS = ("context_length", "context length", "context window", "reduce the length", "too long", "too large")


def retry_wait(res: httpx.Response) -> float:
    try:
        wait = float(res.headers.get("retry-after", DEFAULT_RETRY_WAIT_SECONDS))
    except ValueError:
        wait = DEFAULT_RETRY_WAIT_SECONDS
    return min(max(wait, 1.0), MAX_RETRY_WAIT_SECONDS)


def tokens(*values: object) -> int | None:
    counts = [value for value in values if isinstance(value, int)]
    return sum(counts) if counts else None
