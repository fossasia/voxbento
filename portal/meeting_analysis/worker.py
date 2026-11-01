from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from portal.database import get_session
from portal.meeting_analysis.constants import (
    IN_PROGRESS,
    STALE_AFTER,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    AnalysisProviderEnum,
)
from portal.meeting_analysis.prompts import build_analysis_messages, build_transcript, transcript_lines
from portal.meeting_analysis.providers import get_provider
from portal.meeting_analysis.schema import AnalysisError, parse_analysis
from portal.models import Event, MeetingReport, Room, TranscriptSegment, utc_now
from portal.room_setup import language_name
from portal.tasks import track_task
from portal.translations.keys import get_translation_api_key

logger = logging.getLogger(__name__)

FAILED_UNEXPECTEDLY = "Something went wrong while writing the report. Try again."
INTERRUPTED = "The server restarted before the report was ready. Generate it again."
UNREADABLE_KEY = "The saved API key for this AI service can't be read. Save it again on the API keys page."


@dataclass(frozen=True)
class _Job:
    provider: str
    model: str
    api_key: str | None
    system: str
    user: str


async def _fail(report_id: int, message: str) -> None:
    async with get_session() as session:
        await session.execute(
            update(MeetingReport)
            .where(MeetingReport.id == report_id, MeetingReport.status.in_(IN_PROGRESS))
            .values(status=STATUS_FAILED, error=message[:500])
        )


async def _prepare(report_id: int) -> _Job | None:
    async with get_session() as session:
        report = await session.get(MeetingReport, report_id)
        if report is None or report.status != STATUS_PENDING:
            return None
        event = await session.get(Event, report.event_id)
        room = await session.get(Room, report.room_id)
        if event is None or room is None:
            report.status = STATUS_FAILED
            report.error = "The event or room no longer exists."
            return None
        segments = (
            await session.scalars(
                select(TranscriptSegment)
                .where(TranscriptSegment.room_id == room.id, TranscriptSegment.language_code == report.channel_id)
                .order_by(TranscriptSegment.created_at, TranscriptSegment.id)
            )
        ).all()
        lines = transcript_lines(segments)
        if not lines:
            report.status = STATUS_FAILED
            report.error = "There is no transcript for this channel yet."
            return None
        api_key = None
        if report.provider != AnalysisProviderEnum.LOCAL.value:
            try:
                api_key = get_translation_api_key(event, report.provider)
            except ValueError as exc:
                raise AnalysisError(UNREADABLE_KEY) from exc
            if not api_key:
                report.status = STATUS_FAILED
                report.error = "The event has no API key for the chosen AI service. Add it on the API keys page."
                return None
        transcript, truncated = build_transcript(lines)
        system, user = build_analysis_messages(
            event_name=event.display_name,
            room_name=room.display_name,
            channel_label=report.channel_label,
            language_name=language_name(report.language_code) if report.language_code != "floor" else "",
            transcript=transcript,
            truncated=truncated,
        )
        report.status = STATUS_RUNNING
        report.segment_count = len(lines)
        report.truncated = truncated
        return _Job(report.provider, report.model, api_key, system, user)


async def run_analysis(report_id: int) -> None:
    try:
        await _run(report_id)
    except AnalysisError as exc:
        logger.warning("Meeting report %s failed: %s", report_id, exc)
        await _fail(report_id, str(exc))
    except Exception:
        await _fail(report_id, FAILED_UNEXPECTEDLY)
        raise


async def _run(report_id: int) -> None:
    job = await _prepare(report_id)
    if job is None:
        return
    completion = await get_provider(job.provider).complete(
        system=job.system, user=job.user, model=job.model, api_key=job.api_key
    )
    result = parse_analysis(completion.text)
    async with get_session() as session:
        await session.execute(
            update(MeetingReport)
            .where(MeetingReport.id == report_id, MeetingReport.status == STATUS_RUNNING)
            .values(
                summary=result.summary,
                action_items_json=json.dumps(result.action_items, ensure_ascii=False),
                key_topics_json=json.dumps(result.key_topics, ensure_ascii=False),
                digest_json=result.digest_json(),
                tokens_used=completion.tokens_used,
                status=STATUS_DONE,
                error=None,
                generated_at=utc_now(),
            )
        )


def start_analysis(report_id: int) -> None:
    track_task(asyncio.create_task(run_analysis(report_id), name=f"meeting-report-{report_id}"))


async def mark_interrupted_reports(session: AsyncSession, room_id: int) -> None:
    await session.execute(
        update(MeetingReport)
        .where(
            MeetingReport.room_id == room_id,
            MeetingReport.status.in_(IN_PROGRESS),
            MeetingReport.created_at < utc_now() - STALE_AFTER,
        )
        .values(status=STATUS_FAILED, error=INTERRUPTED)
    )
