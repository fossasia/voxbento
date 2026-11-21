"""Automatic meeting reports (#1080, phase 2).

When a channel's captions stop, and its event asks for automatic reports, a report on the
channel's latest session is written a minute later. The wait lets a quick restart (a changed
key, a dropped stream) carry on the same session instead of ending it. Nothing is written for a
session shorter than ``MEETING_ANALYSIS_AUTO_MIN_LINES`` lines, or one that already has a report
covering it. A report is never started while another one of the room is being written: the
automatic one waits its turn. A server restart during the wait drops it; an owner can still
generate the report on the room's Meeting reports page.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from portal.config import settings
from portal.database import create_meeting_report, get_session
from portal.meeting_analysis.channels import Channel, list_channels
from portal.meeting_analysis.constants import (
    AUTO_DELAY,
    AUTO_RETRIES,
    AUTO_RETRY_WAIT,
    STATUS_FAILED,
    STATUS_PENDING,
    model_for,
)
from portal.meeting_analysis.sessions import Session
from portal.meeting_analysis.worker import mark_interrupted_reports, room_busy, start_analysis
from portal.models import Event, MeetingReport, Room
from portal.tasks import track_task

logger = logging.getLogger(__name__)


class _Busy(Exception):
    """Another report of the room is being written."""


def schedule_auto_report(room_id: int, channel_id: str, captions_running: Callable[[], bool]) -> None:
    """Write a report on the channel's latest session soon, unless its captions start again first."""
    track_task(
        asyncio.create_task(
            auto_report(room_id, channel_id, captions_running), name=f"auto-meeting-report-{room_id}-{channel_id}"
        )
    )


async def auto_report(room_id: int, channel_id: str, captions_running: Callable[[], bool]) -> int | None:
    await asyncio.sleep(AUTO_DELAY.total_seconds())
    for _ in range(AUTO_RETRIES):
        if captions_running():
            return None
        try:
            report_id = await create_auto_report(room_id, channel_id)
        except _Busy:
            await asyncio.sleep(AUTO_RETRY_WAIT.total_seconds())
            continue
        if report_id is not None:
            logger.info("Writing an automatic meeting report %s for room %s (%s)", report_id, room_id, channel_id)
            start_analysis(report_id)
        return report_id
    logger.info("Room %s stayed busy, so no automatic meeting report was written for %s", room_id, channel_id)
    return None


async def _already_reported(session: AsyncSession, room_id: int, channel: Channel, stretch: Session) -> bool:
    reports = await session.scalars(
        select(MeetingReport).where(
            MeetingReport.room_id == room_id,
            MeetingReport.channel_id == channel.id,
            MeetingReport.transcript_language.is_(None),
            MeetingReport.status != STATUS_FAILED,
            MeetingReport.session_started_at.is_not(None),
        )
    )
    return any(
        report.session_started_at == stretch.started_at
        and report.session_ended_at is not None
        and report.session_ended_at >= stretch.ended_at
        for report in reports
    )


async def create_auto_report(room_id: int, channel_id: str) -> int | None:
    """Queue a report on the channel's latest session, or None when none is due. Raises ``_Busy``."""
    try:
        return await _create(room_id, channel_id)
    except IntegrityError:
        # Another report of the room started between the check and this one.
        raise _Busy from None


async def _create(room_id: int, channel_id: str) -> int | None:
    async with get_session() as session:
        room = await session.get(Room, room_id)
        if room is None:
            return None
        event = await session.get(Event, room.event_id)
        if event is None or not (event.meeting_analysis_enabled and event.meeting_analysis_auto):
            return None
        channel = next((c for c in await list_channels(session, room) if c.id == channel_id), None)
        if channel is None:
            return None
        stretch = channel.sessions[0]
        if stretch.lines < settings.meeting_analysis_auto_min_lines:
            return None
        if await _already_reported(session, room_id, channel, stretch):
            return None
        await mark_interrupted_reports(session, room_id)
        if await room_busy(session, room_id):
            raise _Busy
        report = await create_meeting_report(
            session,
            event_id=event.id,
            room_id=room_id,
            channel_id=channel.id,
            channel_label=channel.label,
            language_code=channel.language_code,
            provider=event.meeting_analysis_provider,
            model=model_for(event.meeting_analysis_provider, event.meeting_analysis_model),
            status=STATUS_PENDING,
            session_started_at=stretch.started_at,
            session_ended_at=stretch.ended_at,
            automatic=True,
        )
        return report.id
