from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from portal.database import get_session
from portal.meeting_analysis.constants import (
    IN_PROGRESS,
    JOB_TIMEOUT,
    STALE_AFTER,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    AnalysisProviderEnum,
)
from portal.meeting_analysis.prompts import (
    build_analysis_messages,
    build_merge_messages,
    build_transcript,
    merge_batches,
    split_transcript,
    transcript_lines,
)
from portal.meeting_analysis.providers import get_provider
from portal.meeting_analysis.schema import AnalysisError, AnalysisResult, UnreadableAnswer, parse_analysis
from portal.models import Event, MeetingReport, Room, TranscriptSegment, utc_now
from portal.room_setup import language_name
from portal.tasks import track_task
from portal.translations.keys import get_translation_api_key

logger = logging.getLogger(__name__)

PARSE_ATTEMPTS = 2
FAILED_UNEXPECTEDLY = "Something went wrong while writing the report. Try again."
INTERRUPTED = "The server restarted before the report was ready. Generate it again."
UNREADABLE_KEY = "The saved API key for this AI service can't be read. Save it again on the API keys page."
TOOK_TOO_LONG = "The AI service took too long to write the report. Try again, or choose a faster model."


class _Retired(Exception):
    pass


@dataclass(frozen=True)
class _Job:
    provider: str
    model: str
    api_key: str | None
    context: dict
    parts: list[tuple[str, str]]
    truncated: bool


async def _fail(report_id: int, message: str) -> None:
    async with get_session() as session:
        await session.execute(
            update(MeetingReport)
            .where(MeetingReport.id == report_id, MeetingReport.status.in_(IN_PROGRESS))
            .values(status=STATUS_FAILED, error=message[:500])
        )


async def _still_running(report_id: int) -> bool:
    async with get_session() as session:
        status = await session.scalar(select(MeetingReport.status).where(MeetingReport.id == report_id))
    return status == STATUS_RUNNING


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
        context = {
            "event_name": event.display_name,
            "room_name": room.display_name,
            "channel_label": report.channel_label,
            "language_name": language_name(report.language_code) if report.language_code != "floor" else "",
        }
        chunks = split_transcript(transcript)
        if len(chunks) == 1:
            parts = [build_analysis_messages(**context, transcript=transcript, truncated=truncated)]
        else:
            parts = [
                build_analysis_messages(**context, transcript=chunk, part=(number, len(chunks)))
                for number, chunk in enumerate(chunks, start=1)
            ]
        report.status = STATUS_RUNNING
        report.segment_count = len(lines)
        report.last_segment_id = segments[-1].id
        report.truncated = truncated
        return _Job(report.provider, report.model, api_key, context, parts, truncated)


async def run_analysis(report_id: int) -> None:
    try:
        async with asyncio.timeout(JOB_TIMEOUT.total_seconds()):
            await _run(report_id)
    except _Retired:
        logger.info("Meeting report %s was retired, so its job stopped", report_id)
    except TimeoutError:
        logger.warning("Meeting report %s took longer than %s", report_id, JOB_TIMEOUT)
        await _fail(report_id, TOOK_TOO_LONG)
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
    provider = get_provider(job.provider)
    used: list[int] = []

    async def ask(system: str, user: str) -> AnalysisResult:
        for attempt in range(PARSE_ATTEMPTS):
            if not await _still_running(report_id):
                raise _Retired
            try:
                completion = await provider.complete(system=system, user=user, model=job.model, api_key=job.api_key)
                used.append(completion.tokens_used or 0)
                return parse_analysis(completion.text)
            except UnreadableAnswer:
                if attempt == PARSE_ATTEMPTS - 1:
                    raise
        raise AnalysisError("The AI service didn't return a report.")

    results = [await ask(system, user) for system, user in job.parts]
    while len(results) > 1:
        results = [
            batch[0]
            if len(batch) == 1
            else await ask(*build_merge_messages(**job.context, parts=batch, truncated=job.truncated))
            for batch in merge_batches(results)
        ]
    result = results[0]
    async with get_session() as session:
        await session.execute(
            update(MeetingReport)
            .where(MeetingReport.id == report_id, MeetingReport.status == STATUS_RUNNING)
            .values(
                title=result.title,
                summary=result.overview,
                sections_json=result.sections_json(),
                tokens_used=sum(used) or None,
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
