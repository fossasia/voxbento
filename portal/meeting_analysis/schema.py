from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from portal.meeting_analysis.constants import MAX_POINTS, MAX_SECTIONS


class AnalysisError(Exception):
    pass


@dataclass(frozen=True)
class Section:
    heading: str
    points: list[str]


@dataclass(frozen=True)
class AnalysisResult:
    title: str
    overview: str
    sections: list[Section]

    def sections_json(self) -> str:
        return json.dumps([asdict(section) for section in self.sections], ensure_ascii=False)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _sections(value: Any) -> list[Section]:
    if not isinstance(value, list):
        return []
    sections = []
    for item in value:
        if not isinstance(item, dict):
            continue
        heading = _text(item.get("heading"), 160)
        raw_points = item.get("points")
        points = [_text(point, 600) for point in raw_points] if isinstance(raw_points, list) else []
        points = [point for point in points if point][:MAX_POINTS]
        if heading and points:
            sections.append(Section(heading, points))
    return sections[:MAX_SECTIONS]


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
    overview = _text(data.get("overview"), 2000)
    if not overview:
        raise AnalysisError("The AI service returned an empty summary.")
    return AnalysisResult(
        title=_text(data.get("title"), 200),
        overview=overview,
        sections=_sections(data.get("sections")),
    )


def load_sections(value: str | None) -> list[Section]:
    try:
        data = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return _sections(data)
