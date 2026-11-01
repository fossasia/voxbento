from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from portal.meeting_analysis.constants import PROVIDER_LABELS, REQUEST_TIMEOUT_SECONDS
from portal.meeting_analysis.schema import AnalysisError


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

        try:
            return await pg.get_http_client().post(
                url, headers=headers, json=body, timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS)
            )
        except httpx.TimeoutException as exc:
            raise AnalysisError(f"{self.label} took too long to answer. Try again.") from exc
        except httpx.HTTPError as exc:
            raise AnalysisError(f"Couldn't reach {self.label}.") from exc

    async def post(self, url: str, *, headers: dict[str, str], body: dict) -> dict:
        return self.read(await self.send(url, headers=headers, body=body))

    def read(self, res: httpx.Response) -> dict:
        if res.status_code >= 400:
            raise AnalysisError(f"{self.label} refused the request (HTTP {res.status_code}).")
        try:
            return res.json()
        except ValueError as exc:
            raise AnalysisError(f"{self.label} returned an answer that couldn't be read.") from exc


def tokens(*values: object) -> int | None:
    counts = [value for value in values if isinstance(value, int)]
    return sum(counts) if counts else None
