from __future__ import annotations

from portal.meeting_analysis.providers.base import AnalysisProvider, Completion
from portal.meeting_analysis.schema import AnalysisError
from portal.translations.constants import OPENAI_COMPATIBLE_ENDPOINTS


class OpenAICompatibleProvider(AnalysisProvider):
    def __init__(self, name: str) -> None:
        self.name = name

    async def complete(self, *, system: str, user: str, model: str, api_key: str | None) -> Completion:
        if not api_key:
            raise AnalysisError(f"Add a {self.label} API key on the API keys page first.")
        data = await self.post(
            OPENAI_COMPATIBLE_ENDPOINTS[self.name],
            headers={"Authorization": f"Bearer {api_key}"},
            body={
                "model": model,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            },
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AnalysisError(f"{self.label} returned an answer that couldn't be read.") from exc
        return Completion(text or "", (data.get("usage") or {}).get("total_tokens"))
