from __future__ import annotations

from portal.config import settings
from portal.meeting_analysis.providers.base import AnalysisProvider, Completion, tokens
from portal.meeting_analysis.schema import AnalysisError


class LocalProvider(AnalysisProvider):
    name = "local"

    async def complete(self, *, system: str, user: str, model: str, api_key: str | None) -> Completion:
        base = settings.meeting_analysis_ollama_base_url.rstrip("/")
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        res = await self.send(
            f"{base}/api/chat",
            headers={},
            body={
                "model": model,
                "stream": False,
                "format": "json",
                "messages": messages,
                "options": {"num_ctx": settings.meeting_analysis_ollama_num_ctx},
            },
        )
        if res.status_code == 404:
            data = await self.post(
                f"{base}/v1/chat/completions", headers={}, body={"model": model, "messages": messages}
            )
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                raise AnalysisError(f"{self.label} returned an answer that couldn't be read.") from exc
            return Completion(text or "", (data.get("usage") or {}).get("total_tokens"))
        data = self.read(res)
        try:
            text = data["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise AnalysisError(f"{self.label} returned an answer that couldn't be read.") from exc
        return Completion(text or "", tokens(data.get("prompt_eval_count"), data.get("eval_count")))
