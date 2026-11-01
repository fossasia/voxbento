from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from portal.auth import get_access_flags, require_event_owner, require_owner_or_coordinator
from portal.database import (
    create_meeting_report,
    get_event_by_id,
    get_meeting_report,
    get_session,
    list_booths_for_room,
    list_meeting_reports,
)
from portal.meeting_analysis.constants import IN_PROGRESS, PROVIDER_LABELS, STATUS_DONE, STATUS_PENDING, model_for
from portal.meeting_analysis.export import report_markdown
from portal.meeting_analysis.schema import load_list
from portal.meeting_analysis.worker import mark_interrupted_reports, start_analysis
from portal.models import MeetingReport, TranscriptSegment
from portal.routers.admin import room_in_event, templates
from portal.utils import safe_redirect, verify_same_origin
from portal.workspace_routing import management_url

router = APIRouter()

FLOOR = "floor"


class _Refused(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Channel:
    id: str
    label: str
    language_code: str
    lines: int


async def _channels(session, room) -> list[Channel]:
    counts = dict(
        (
            await session.execute(
                select(TranscriptSegment.language_code, func.count())
                .where(TranscriptSegment.room_id == room.id)
                .group_by(TranscriptSegment.language_code)
            )
        ).all()
    )
    channels = []
    if counts.get(FLOOR):
        channels.append(Channel(FLOOR, "Floor audio", room.floor_language_code or FLOOR, counts[FLOOR]))
    for booth in await list_booths_for_room(session, room.id):
        if counts.get(booth.language_code):
            channels.append(
                Channel(
                    booth.language_code,
                    f"{booth.language_name} booth",
                    booth.language_code,
                    counts[booth.language_code],
                )
            )
    return channels


def _report_view(report: MeetingReport) -> dict:
    return {
        "report": report,
        "provider_label": PROVIDER_LABELS.get(report.provider, report.provider),
        "action_items": [item for item in load_list(report.action_items_json) if isinstance(item, str)],
        "key_topics": [item for item in load_list(report.key_topics_json) if isinstance(item, str)],
        "digest": [chunk for chunk in load_list(report.digest_json) if isinstance(chunk, dict) and chunk.get("text")],
    }


async def _room_report(session, event_id: int, room_id: int, report_id: int) -> MeetingReport:
    await room_in_event(session, event_id, room_id)
    report = await get_meeting_report(session, report_id)
    if report is None or report.room_id != room_id or report.event_id != event_id:
        raise HTTPException(status_code=404, detail="Report not found.")
    return report


@router.get("/admin/events/{event_id}/rooms/{room_id}/analysis/", dependencies=[Depends(require_owner_or_coordinator)])
async def room_analysis_page(request: Request, event_id: int, room_id: int):
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await room_in_event(session, event_id, room_id)
        await mark_interrupted_reports(session, room_id)
        reports = await list_meeting_reports(session, room_id)
        channels = await _channels(session, room)
    admin_flags = await get_access_flags(request, event_id=event_id, room_id=room_id)
    return templates.TemplateResponse(
        request=request,
        name="admin/room_analysis.html",
        context={
            "event": event,
            "room": room,
            "channels": channels,
            "reports": [_report_view(report) for report in reports],
            "in_progress": any(report.status in IN_PROGRESS for report in reports),
            "provider_label": PROVIDER_LABELS.get(event.meeting_analysis_provider, event.meeting_analysis_provider),
            "model": model_for(event.meeting_analysis_provider, event.meeting_analysis_model),
            "error": request.query_params.get("error", ""),
            **admin_flags,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/admin/events/{event_id}/rooms/{room_id}/analysis/", dependencies=[Depends(require_event_owner)])
async def room_analysis_generate(request: Request, event_id: int, room_id: int, channel: str = Form("")):
    if not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid request origin.")
    page = f"events/{event_id}/rooms/{room_id}/analysis/"
    try:
        report_id = await _create_report(event_id, room_id, channel)
    except IntegrityError:
        return safe_redirect(url=management_url(request, f"{page}?error=busy"), status_code=303)
    except _Refused as refused:
        return safe_redirect(url=management_url(request, f"{page}?error={refused.reason}"), status_code=303)
    start_analysis(report_id)
    return safe_redirect(url=management_url(request, page), status_code=status.HTTP_303_SEE_OTHER)


async def _create_report(event_id: int, room_id: int, channel: str) -> int:
    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="Event not found.")
        room = await room_in_event(session, event_id, room_id)
        if not event.meeting_analysis_enabled:
            raise _Refused("disabled")
        await mark_interrupted_reports(session, room_id)
        busy = await session.scalar(
            select(func.count())
            .select_from(MeetingReport)
            .where(MeetingReport.room_id == room_id, MeetingReport.status.in_(IN_PROGRESS))
        )
        if busy:
            raise _Refused("busy")
        chosen = next((c for c in await _channels(session, room) if c.id == channel), None)
        if chosen is None:
            raise _Refused("channel")
        report = await create_meeting_report(
            session,
            event_id=event_id,
            room_id=room_id,
            channel_id=chosen.id,
            channel_label=chosen.label,
            language_code=chosen.language_code,
            provider=event.meeting_analysis_provider,
            model=model_for(event.meeting_analysis_provider, event.meeting_analysis_model),
            status=STATUS_PENDING,
        )
        return report.id


@router.get(
    "/admin/events/{event_id}/rooms/{room_id}/analysis/{report_id}.md",
    dependencies=[Depends(require_owner_or_coordinator)],
)
async def room_analysis_markdown(event_id: int, room_id: int, report_id: int):
    async with get_session() as session:
        report = await _room_report(session, event_id, room_id, report_id)
        if report.status != STATUS_DONE:
            raise HTTPException(status_code=404, detail="This report isn't ready.")
        event = await get_event_by_id(session, event_id)
        room = await room_in_event(session, event_id, room_id)
        body = report_markdown(report, event, room)
    return Response(
        body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="meeting-report-{room_id}-{report_id}.md"',
            "Cache-Control": "no-store",
        },
    )


@router.get(
    "/api/admin/events/{event_id}/rooms/{room_id}/analysis/{report_id}",
    dependencies=[Depends(require_owner_or_coordinator)],
)
async def room_analysis_status(event_id: int, room_id: int, report_id: int):
    async with get_session() as session:
        await room_in_event(session, event_id, room_id)
        await mark_interrupted_reports(session, room_id)
        report = await _room_report(session, event_id, room_id, report_id)
        body = {"id": report.id, "status": report.status, "error": report.error}
    return JSONResponse(body, headers={"Cache-Control": "no-store"})
