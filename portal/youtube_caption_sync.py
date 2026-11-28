"""Keep each room's YouTube caption delivery in step with its saved choice (#1087, plan 006).

``portal/youtube_captions.py`` is the sending engine; this module decides when it runs. The saved
choice is ``rooms.youtube_captions_language``. A background loop restores delivery after a restart
(no room page or caption needed), replaces a target whose broadcast, stream or language changed, and
stops one whose event, connection, broadcast or language is no longer usable.

Bounds: the loop runs every ``RECONCILE_INTERVAL_SECS``, so a cleared choice, a deleted room or event,
or a disabled translation stops delivery within that long. Revoked Google access or an ended broadcast
is noticed when a running target is re-checked at YouTube, at most ``VERIFY_INTERVAL_SECS`` apart (one
``liveBroadcasts.list`` unit per room), or sooner when the room page's status poll records the error.

Caption URLs never leave memory: they are fetched again from YouTube when delivery restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from portal import youtube_account as yt
from portal import youtube_captions
from portal.database import get_session
from portal.models import Event, Room, RoomTranslationLanguage, YouTubeConnection
from portal.program_ingest import room_lock
from portal.tasks import track_task

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL_SECS = 30.0
VERIFY_INTERVAL_SECS = 300.0
# After YouTube refused to set a broadcast up (latency, ended, already live), wait before asking again.
RETRY_AFTER_REFUSAL_SECS = 300.0
NO_CAPTION_ADDRESS = "YouTube did not provide a caption address for this stream. Try again in a moment."
FLOOR = "floor"


@dataclass
class _Running:
    event_id: int
    language: str
    broadcast_id: str
    stream_id: str
    verified_at: float

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.language, self.broadcast_id, self.stream_id)


_running: dict[int, _Running] = {}
# room_id -> monotonic time before which a failed start is not retried.
_retry_at: dict[int, float] = {}
# Reconciles started by ``kick``; shutdown cancels them so none can start a sender after its sweep.
_kicks: set[asyncio.Task] = set()


def _now() -> float:
    return time.monotonic()


def _key(room: Room) -> tuple[str, str, str]:
    return (room.youtube_captions_language or "", room.youtube_broadcast_id or "", room.youtube_stream_id or "")


async def enabled_languages(session, room_id: int) -> list[RoomTranslationLanguage]:
    result = await session.execute(
        select(RoomTranslationLanguage)
        .where(RoomTranslationLanguage.room_id == room_id, RoomTranslationLanguage.enabled.is_(True))
        .order_by(RoomTranslationLanguage.language_name)
    )
    return list(result.scalars())


def choices(room: Room, languages: list[RoomTranslationLanguage]) -> list[dict]:
    """The caption languages the owner can pick for the room's YouTube track."""
    options = [{"value": FLOOR, "label": f"Spoken language ({room.floor_source_language_code or 'floor'})"}]
    if room.floor_translation_enabled:
        options += [{"value": lang.language_code, "label": lang.language_name} for lang in languages]
    return options


def language_problem(room: Room, language: str, languages: list[RoomTranslationLanguage]) -> str | None:
    """Why this language would never put text on the YouTube track, or None when it can."""
    if not room.has_floor_captions:
        return "Turn on floor transcription for this room first: YouTube captions are made from it."
    return youtube_captions.unusable_target_reason(
        language, bool(room.floor_translation_enabled), {lang.language_code for lang in languages}
    )


def problem(room: Room, connection: YouTubeConnection | None, languages: list[RoomTranslationLanguage]) -> str | None:
    """Why the room's saved caption choice cannot run right now, or None when it can."""
    if connection is None:
        return "Sign in with YouTube to post captions."
    if connection.last_error:
        return connection.last_error
    if not room.youtube_broadcast_id or not room.youtube_stream_id:
        return "Link a YouTube broadcast to post captions."
    return language_problem(room, room.youtube_captions_language or "", languages)


def status(room: Room, languages: list[RoomTranslationLanguage]) -> dict:
    """The room's caption delivery for the room page. Never carries the caption URL."""
    health = youtube_captions.health.get(room.id)
    language = room.youtube_captions_language
    return {
        "language": language,
        # Before the first reconcile after a restart there is no health yet: delivery is about to start.
        "state": (health.state if health else "starting") if language else "off",
        "reason": (health.reason if health else "") if language else "",
        "last_ok_at": health.last_ok_at if health and language else None,
        "choices": choices(room, languages),
    }


async def start(room: Room, connection: YouTubeConnection, language: str) -> None:
    """Set the broadcast up for posted captions and point the engine at its caption URL.

    Raises ValueError (organiser-facing), ``YouTubeAuthError`` or ``YouTubeAPIError``. Whatever ran
    before for the room is stopped first, so captions never go to an obsolete stream.
    """
    await enable(room, connection)
    stream = await yt.get_stream(connection, room.youtube_stream_id)
    if stream is None or not stream.captions_url:
        raise ValueError(NO_CAPTION_ADDRESS)
    await youtube_captions.activate(room.id, language, stream.captions_url)
    _running[room.id] = _Running(
        event_id=room.event_id,
        language=language,
        broadcast_id=room.youtube_broadcast_id,
        stream_id=room.youtube_stream_id,
        verified_at=_now(),
    )
    _retry_at.pop(room.id, None)
    logger.info("youtube_captions room_id=%s action=started language=%s", room.id, language)


async def enable(room: Room, connection: YouTubeConnection) -> None:
    # A separate name so tests can count YouTube set-up calls without a real broadcast. The saved stream is
    # passed so a broadcast rebound in YouTube Studio is refused instead of posting to the old stream.
    await yt.enable_http_captions(connection, room.youtube_broadcast_id, room.youtube_stream_id)


async def stop(room_id: int, reason: str = "") -> None:
    """Stop delivery for the room. With a reason, the room page shows it as the failure."""
    was_running = _running.pop(room_id, None) is not None or room_id in youtube_captions.targets
    await youtube_captions.deactivate(room_id)
    if reason:
        youtube_captions.health[room_id] = youtube_captions.CaptionHealth(state="failing", reason=reason)
    if was_running:
        logger.info("youtube_captions room_id=%s action=stopped", room_id)


def _set_failing(room_id: int, reason: str) -> None:
    youtube_captions.health[room_id] = youtube_captions.CaptionHealth(state="failing", reason=reason)


async def reconcile_room(room_id: int, now: float | None = None) -> None:
    """Bring one room's delivery in line with its saved choice.

    Holds the same room and event locks as the owner routes, so a link, unlink or disconnect never
    interleaves with a start that is talking to YouTube.
    """
    now = _now() if now is None else now
    async with room_lock(room_id):
        async with get_session() as session:
            room = await session.get(Room, room_id)
            event = await session.get(Event, room.event_id) if room is not None else None
            # A deleted event keeps its rooms and saved choice; delivery stops and never restarts for it.
            if room is None or event is None or event.deleted_at is not None or not room.youtube_captions_language:
                if room_id in _running or room_id in youtube_captions.targets:
                    await stop(room_id)
                _retry_at.pop(room_id, None)
                return
            async with yt.event_lock(room.event_id):
                await _reconcile(session, room, now)


async def _reconcile(session, room: Room, now: float) -> None:
    connection = await yt.get_connection(session, room.event_id)
    languages = await enabled_languages(session, room.id)
    reason = problem(room, connection, languages)
    running = _running.get(room.id)
    if running is not None and room.id not in youtube_captions.targets:
        running = None  # the engine was stopped elsewhere; start again
        _running.pop(room.id, None)
    if reason:
        if running is not None:
            await stop(room.id, reason)
        else:
            _set_failing(room.id, reason)
        return
    if running is not None and running.key == _key(room):
        if now - running.verified_at >= VERIFY_INTERVAL_SECS:
            await _verify(room, connection, running, now)
        return
    if _retry_at.get(room.id, 0.0) > now:
        return
    language = room.youtube_captions_language
    try:
        await start(room, connection, language)
    except ValueError as exc:
        await stop(room.id, str(exc))
        _retry_at[room.id] = now + RETRY_AFTER_REFUSAL_SECS
    except yt.YouTubeAuthError as exc:
        await stop(room.id, str(exc))
        _retry_at[room.id] = now + RETRY_AFTER_REFUSAL_SECS
    except yt.YouTubeAPIError as exc:
        await stop(room.id, yt.describe(exc))
        quota = exc.reason in yt.QUOTA_REASONS
        _retry_at[room.id] = now + (yt.QUOTA_BACKOFF_SECS if quota else RECONCILE_INTERVAL_SECS)


async def _verify(room: Room, connection: YouTubeConnection, running: _Running, now: float) -> None:
    """Re-check a running target at YouTube: revoked access, an ended broadcast or a rebound stream."""
    try:
        broadcast = await yt.get_broadcast(connection, running.broadcast_id)
    except yt.YouTubeAuthError as exc:
        await stop(room.id, str(exc))
        return
    except yt.YouTubeAPIError as exc:
        # Transient or quota: keep posting and look again next window.
        logger.warning("youtube_captions room_id=%s action=verify_failed reason=%s", room.id, exc.reason)
        running.verified_at = now
        return
    if broadcast is None:
        await stop(room.id, "The linked broadcast is no longer on the connected channel.")
    elif broadcast.life_cycle_status in ("complete", "revoked"):
        await stop(room.id, yt.ENDED_MESSAGE)
    elif broadcast.bound_stream_id and broadcast.bound_stream_id != running.stream_id:
        await stop(room.id, yt.REBOUND_MESSAGE)
    else:
        running.verified_at = now


async def reconcile_once(now: float | None = None) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(Room.id).join(Event).where(Room.youtube_captions_language.is_not(None), Event.deleted_at.is_(None))
        )
        wanted = set(result.scalars())
    # Rooms whose choice was cleared, or whose room or event was deleted, stop here.
    for room_id in set(_running) | set(youtube_captions.targets):
        if room_id not in wanted:
            await reconcile_room(room_id, now)
    for room_id in sorted(wanted):
        await reconcile_room(room_id, now)


async def reconcile_loop() -> None:
    while True:
        try:
            await reconcile_once()
        except SQLAlchemyError:
            logger.warning("youtube_captions reconcile_failed reason=database")
        except Exception as exc:  # noqa: BLE001 - the loop must outlive any single bad tick
            # The type only: exception text from YouTube calls must never reach the log.
            logger.error("youtube_captions reconcile_error type=%s", type(exc).__name__)
        await asyncio.sleep(RECONCILE_INTERVAL_SECS)


def kick(room_id: int) -> None:
    """Reconcile one room soon, after the caller has released the room's locks."""
    _retry_at.pop(room_id, None)
    task = asyncio.create_task(_kick(room_id))
    _kicks.add(task)
    task.add_done_callback(_kicks.discard)
    track_task(task)


async def _kick(room_id: int) -> None:
    try:
        await reconcile_room(room_id)
    except Exception as exc:  # noqa: BLE001 - the background loop retries
        logger.warning("youtube_captions room_id=%s action=kick_failed type=%s", room_id, type(exc).__name__)


async def shutdown() -> None:
    """Stop every sender (application shutdown).

    Pending kicks are cancelled and awaited first: one still talking to YouTube could otherwise activate a
    sender after the sweep. The reconcile loop is cancelled by the caller before this runs.
    """
    kicks = list(_kicks)
    for task in kicks:
        task.cancel()
    await asyncio.gather(*kicks, return_exceptions=True)
    for room_id in list(youtube_captions.targets):
        await youtube_captions.deactivate(room_id)
    _running.clear()
    _retry_at.clear()
