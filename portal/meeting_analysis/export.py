from __future__ import annotations

from portal.meeting_analysis.constants import PROVIDER_LABELS
from portal.meeting_analysis.schema import load_sections
from portal.models import Event, MeetingReport, Room


def _line(value: object) -> str:
    return " ".join(str(value).split())


def report_markdown(report: MeetingReport, event: Event, room: Room) -> str:
    when = report.generated_at or report.created_at
    lines = [
        f"# {_line(report.title) or f'Meeting report: {_line(room.display_name)}'}",
        "",
        f"- Event: {_line(event.display_name)}",
        f"- Room: {_line(room.display_name)}",
        f"- Channel: {_line(report.channel_label)}",
        f"- Generated: {when.strftime('%Y-%m-%d %H:%M UTC')}",
        f"- AI service: {PROVIDER_LABELS.get(report.provider, report.provider)} ({report.model})",
        f"- Transcript lines: {report.segment_count}{' (only the first part fit)' if report.truncated else ''}",
        "",
        _line(report.summary),
    ]
    for section in load_sections(report.sections_json):
        lines += ["", f"## {_line(section.heading)}", ""]
        lines += [f"- {_line(point)}" for point in section.points]
    return "\n".join(lines).rstrip() + "\n"
