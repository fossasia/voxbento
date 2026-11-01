from __future__ import annotations

from portal.meeting_analysis.constants import PROVIDER_LABELS
from portal.meeting_analysis.schema import load_list
from portal.models import Event, MeetingReport, Room


def _line(value: object) -> str:
    return " ".join(str(value).split())


def report_markdown(report: MeetingReport, event: Event, room: Room) -> str:
    when = report.generated_at or report.created_at
    lines = [
        f"# Meeting report: {_line(room.display_name)}",
        "",
        f"- Event: {_line(event.display_name)}",
        f"- Channel: {_line(report.channel_label)}",
        f"- Generated: {when.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- AI service: {PROVIDER_LABELS.get(report.provider, report.provider)} ({report.model})",
        f"- Transcript lines: {report.segment_count}{' (only the first part fit)' if report.truncated else ''}",
        "",
        "## Summary",
        "",
        report.summary,
        "",
        "## Action items",
        "",
    ]
    actions = [_line(item) for item in load_list(report.action_items_json) if isinstance(item, str)]
    lines += [f"- {item}" for item in actions] or ["None."]
    lines += ["", "## Key topics", ""]
    topics = [_line(item) for item in load_list(report.key_topics_json) if isinstance(item, str)]
    lines += [f"- {item}" for item in topics] or ["None."]
    digest = [chunk for chunk in load_list(report.digest_json) if isinstance(chunk, dict) and chunk.get("text")]
    if digest:
        lines += ["", "## Timeline", ""]
        for chunk in digest:
            span = "–".join(part for part in (_line(chunk.get("start", "")), _line(chunk.get("end", ""))) if part)
            lines += [f"**{span}**" if span else "", _line(chunk["text"]), ""]
    return "\n".join(lines).rstrip() + "\n"
