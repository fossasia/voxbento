"""The channels of a room that have a stored transcript, and the sessions in each."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.database import list_booths_for_room
from portal.meeting_analysis.sessions import Session, split_sessions
from portal.models import Room, TranscriptSegment, TranscriptTranslation
from portal.room_setup import language_name

FLOOR = "floor"


@dataclass(frozen=True)
class Channel:
    id: str
    label: str
    language_code: str
    sessions: list[Session]  # newest first; never empty

    def __post_init__(self) -> None:
        if not self.sessions:
            raise ValueError(f"Channel {self.id!r} must have at least one session")


async def list_channels(session: AsyncSession, room: Room) -> list[Channel]:
    rows: dict[str, list] = defaultdict(list)
    for language_code, segment_id, created_at in await session.execute(
        select(TranscriptSegment.language_code, TranscriptSegment.id, TranscriptSegment.created_at)
        .where(TranscriptSegment.room_id == room.id)
        .order_by(TranscriptSegment.created_at, TranscriptSegment.id)
    ):
        rows[language_code].append((segment_id, created_at))
    channels = []
    if rows.get(FLOOR):
        channels.append(Channel(FLOOR, "Floor audio", room.floor_language_code or FLOOR, split_sessions(rows[FLOOR])))
    for booth in await list_booths_for_room(session, room.id):
        if rows.get(booth.language_code):
            channels.append(
                Channel(
                    booth.language_code,
                    f"{booth.language_name} booth",
                    booth.language_code,
                    split_sessions(rows[booth.language_code]),
                )
            )
    return channels


def session_choice(channels: list[Channel], choice: str) -> tuple[Channel, Session] | None:
    channel_id, _, first_id = choice.partition(":")
    channel = next((c for c in channels if c.id == channel_id), None)
    if channel is None:
        return None
    if not first_id:
        return channel, channel.sessions[0]
    stretch = next((s for s in channel.sessions if str(s.first_id) == first_id), None)
    return (channel, stretch) if stretch else None


async def translation_languages(session: AsyncSession, room: Room) -> list[tuple[str, str]]:
    """The languages this room's transcript was translated into, as (code, name), by name."""
    codes = await session.scalars(
        select(distinct(TranscriptTranslation.language_code))
        .join(TranscriptSegment, TranscriptSegment.id == TranscriptTranslation.segment_id)
        .where(TranscriptSegment.room_id == room.id)
    )
    return sorted(((code, language_name(code)) for code in codes if code), key=lambda item: item[1])


async def translated_lines(
    session: AsyncSession, room: Room, channel: Channel, stretch: Session, language_code: str
) -> int:
    """How many lines of one session of ``channel`` were translated into ``language_code``."""
    return (
        await session.scalar(
            select(func.count())
            .select_from(TranscriptTranslation)
            .join(TranscriptSegment, TranscriptSegment.id == TranscriptTranslation.segment_id)
            .where(
                TranscriptSegment.room_id == room.id,
                TranscriptSegment.language_code == channel.id,
                TranscriptSegment.created_at >= stretch.started_at,
                TranscriptSegment.created_at <= stretch.ended_at,
                TranscriptTranslation.language_code == language_code,
            )
        )
        or 0
    )
