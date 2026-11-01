from __future__ import annotations

from portal.meeting_analysis.providers.base import AnalysisProvider, Completion
from portal.meeting_analysis.schema import AnalysisError


class GeminiProvider(AnalysisProvider):
    name = "gemini"

    async def complete(self, *, system: str, user: str, model: str, api_key: str | None) -> Completion:
        if not api_key:
            raise AnalysisError(f"Add a {self.label} API key on the API keys page first.")
        data = await self.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            headers={"x-goog-api-key": api_key},
            body={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json"},
            },
        )
        try:
            text = "".join(part.get("text", "") for part in data["candidates"][0]["content"]["parts"])
        except (KeyError, IndexError, TypeError) as exc:
            raise AnalysisError(f"{self.label} returned an answer that couldn't be read.") from exc
        return Completion(text, (data.get("usageMetadata") or {}).get("totalTokenCount"))
