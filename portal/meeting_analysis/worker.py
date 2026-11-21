from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from portal.config import settings
from portal.database import get_session
from portal.job_queue import Job, job_queue
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
_analysis_tasks: dict[int, asyncio.Task] = {}
_schedule_tasks: set[asyncio.Task] = set()

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


def session_filter(report: MeetingReport, *, covered_only: bool = False) -> list:
    conditions = [TranscriptSegment.room_id == report.room_id, TranscriptSegment.language_code == report.channel_id]
    if report.session_started_at is not None:
        conditions.append(TranscriptSegment.created_at >= report.session_started_at)
    end = report.covered_until if covered_only and report.covered_until is not None else report.session_ended_at
    if end is not None:
        conditions.append(TranscriptSegment.created_at <= end)
    elif covered_only and report.last_segment_id is not None:
        conditions.append(TranscriptSegment.id <= report.last_segment_id)
    return conditions


async def _prepare(report_id: int) -> _Job | None:
    async with get_session() as session:
        claimed = await session.scalar(
            update(MeetingReport)
            .where(MeetingReport.id == report_id, MeetingReport.status == STATUS_PENDING)
            .values(status=STATUS_RUNNING, started_at=utc_now())
            .returning(MeetingReport.id)
        )
        if claimed is None:
            return None
        report = await session.get(MeetingReport, report_id)
        event = await session.get(Event, report.event_id)
        room = await session.get(Room, report.room_id)
        if event is None or room is None:
            report.status = STATUS_FAILED
            report.error = "The event or room no longer exists."
            return None
        segments = (
            await session.scalars(
                select(TranscriptSegment)
                .where(*session_filter(report))
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
        covered = lines[: len(transcript.splitlines())] or lines[:1]
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
        report.covered_until = covered[-1].created_at
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
    if settings.job_queue_backend == "redis":
        task = asyncio.create_task(schedule_analysis(report_id), name=f"schedule-report-{report_id}")
        _schedule_tasks.add(task)
        task.add_done_callback(_schedule_tasks.discard)
        track_task(task)
    else:
        launch_analysis(report_id)


async def schedule_analysis(report_id: int) -> None:
    if not await job_queue.schedule([Job("analysis", report_id, utc_now())]):
        launch_analysis(report_id)


def launch_analysis(report_id: int) -> None:
    if report_id in _analysis_tasks:
        return
    task = asyncio.create_task(run_analysis(report_id), name=f"meeting-report-{report_id}")
    _analysis_tasks[report_id] = task
    task.add_done_callback(lambda done: _analysis_tasks.pop(report_id, None))
    track_task(task)


async def analysis_worker_loop() -> None:
    """Recover unstarted reports, with DB polling when Redis is disabled or unavailable."""
    last_maintenance = float("-inf")
    try:
        while True:
            try:
                if time.monotonic() - last_maintenance >= settings.job_queue_reconcile_seconds:
                    async with get_session() as session:
                        await mark_interrupted_reports(session)
                    last_maintenance = time.monotonic()
                ids = await job_queue.ready("analysis")
                if ids is None:
                    async with get_session() as session:
                        ids = list(
                            await session.scalars(
                                select(MeetingReport.id)
                                .where(MeetingReport.status == STATUS_PENDING)
                                .order_by(MeetingReport.id)
                                .limit(100)
                            )
                        )
                for report_id in ids:
                    launch_analysis(report_id)
                await asyncio.sleep(1 if settings.job_queue_backend == "redis" else 10)
            except Exception:
                logger.exception("Meeting analysis queue worker error")
                await asyncio.sleep(10)
    finally:
        tasks = [*_analysis_tasks.values(), *_schedule_tasks]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def mark_interrupted_reports(session: AsyncSession, room_id: int | None = None) -> None:
    conditions = [
        MeetingReport.status == STATUS_RUNNING,
        func.coalesce(MeetingReport.started_at, MeetingReport.created_at) < utc_now() - STALE_AFTER,
    ]
    if room_id is not None:
        conditions.append(MeetingReport.room_id == room_id)
    await session.execute(update(MeetingReport).where(*conditions).values(status=STATUS_FAILED, error=INTERRUPTED))
