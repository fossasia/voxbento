from __future__ import annotations

from portal.meeting_analysis.providers.base import AnalysisProvider, Completion, tokens
from portal.meeting_analysis.schema import AnalysisError


class AnthropicProvider(AnalysisProvider):
    name = "anthropic"

    async def complete(self, *, system: str, user: str, model: str, api_key: str | None) -> Completion:
        if not api_key:
            raise AnalysisError(f"Add a {self.label} API key on the API keys page first.")
        data = await self.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            body={
                "model": model,
                "max_tokens": 4096,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        try:
            text = "".join(part["text"] for part in data["content"] if part.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise AnalysisError(f"{self.label} returned an answer that couldn't be read.") from exc
        usage = data.get("usage") or {}
        return Completion(text, tokens(usage.get("input_tokens"), usage.get("output_tokens")))
