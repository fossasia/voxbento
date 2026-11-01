from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from portal.meeting_analysis.constants import MAX_DIGEST_CHUNKS, MAX_LIST_ITEMS


class AnalysisError(Exception):
    pass


@dataclass(frozen=True)
class DigestChunk:
    start: str
    end: str
    text: str


@dataclass(frozen=True)
class AnalysisResult:
    summary: str
    action_items: list[str]
    key_topics: list[str]
    digest: list[DigestChunk]

    def digest_json(self) -> str:
        return json.dumps([asdict(chunk) for chunk in self.digest], ensure_ascii=False)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items = (_text(item, 500) for item in value)
    return [item for item in items if item][:MAX_LIST_ITEMS]


def _digest(value: Any) -> list[DigestChunk]:
    if not isinstance(value, list):
        return []
    chunks = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 2000)
        if text:
            chunks.append(DigestChunk(_text(item.get("start"), 16), _text(item.get("end"), 16), text))
    return chunks[:MAX_DIGEST_CHUNKS]


def parse_analysis(raw: str) -> AnalysisResult:
    text = _FENCE.sub("", raw.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AnalysisError("The AI service didn't return a report.")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AnalysisError("The AI service returned a report that couldn't be read.") from exc
    if not isinstance(data, dict):
        raise AnalysisError("The AI service returned a report that couldn't be read.")
    summary = _text(data.get("summary"), 4000)
    if not summary:
        raise AnalysisError("The AI service returned an empty summary.")
    return AnalysisResult(
        summary=summary,
        action_items=_strings(data.get("action_items")),
        key_topics=_strings(data.get("key_topics")),
        digest=_digest(data.get("digest")),
    )


def load_list(value: str | None) -> list[Any]:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []
