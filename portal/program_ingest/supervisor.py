"""Program ingest supervisor.

Polls MediaMTX for every room in ``program_ingest`` mode and drives a small
state machine per room:

``waiting``       no encoder has connected since the portal started
``receiving``     a feed with usable audio is present but transcription is not configured
``processing``    the room's floor transcription worker is running on the feed
``degraded``      media is present but unusable (no/unsupported audio, stalled, worker failure)
``disconnected``  the encoder went away; the worker is kept for a grace period, then stopped

State is derived from live MediaMTX data on every tick, so a portal restart
never reports a stale publisher as active. Only the processing worker is ever
started or stopped here — the organizer's parallel YouTube output is untouched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from sqlalchemy import select, update
from sqlalchemy.orm import joinedload

from portal.booth_identity import make_mediamtx_path
from portal.config import settings
from portal.database import get_session
from portal.models import FLOOR_SOURCE_PROGRAM_INGEST, Event, Room, utc_now
from portal.program_ingest.mediamtx import OFFLINE, PathSnapshot, fetch_path_snapshots, remove_path_config
from portal.transcription.floor import (
    FLOOR_LANGUAGE,
    floor_transcription_issue,
    floor_worker_running,
    start_floor_transcription_worker,
    stop_floor_transcription_worker,
)

logger = logging.getLogger(__name__)


class IngestState(str, Enum):
    DISABLED = "disabled"
    WAITING = "waiting"
    RECEIVING = "receiving"
    PROCESSING = "processing"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"


@dataclass
class RoomIngestStatus:
    room_id: int
    state: IngestState = IngestState.WAITING
    detail: str = "Waiting for the encoder to connect."
    online: bool = False
    ever_connected: bool = False
    source_type: str | None = None
    source_id: str | None = None
    audio_codecs: tuple[str, ...] = ()
    video_codecs: tuple[str, ...] = ()
    unsupported_codecs: tuple[str, ...] = ()
    inbound_bytes: int = 0
    bytes_changed_at: float = 0.0
    offline_since: float | None = None
    reconnects: int = 0
    replacements: int = 0
    worker_running: bool = False

    def as_public_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "detail": self.detail,
            "online": self.online,
            "source_type": self.source_type,
            "audio_codecs": list(self.audio_codecs),
            "video_codecs": list(self.video_codecs),
            "unsupported_codecs": list(self.unsupported_codecs),
            "reconnects": self.reconnects,
            "replacements": self.replacements,
            "worker_running": self.worker_running,
        }


@dataclass(frozen=True)
class SupervisorConfig:
    max_active: int
    disconnect_grace_seconds: float
    stall_seconds: float
    poll_seconds: float

    @classmethod
    def from_settings(cls) -> SupervisorConfig:
        return cls(
            max_active=settings.program_ingest_max_active,
            disconnect_grace_seconds=settings.program_ingest_disconnect_grace_seconds,
            stall_seconds=settings.program_ingest_stall_seconds,
            poll_seconds=settings.program_ingest_poll_seconds,
        )


@dataclass(frozen=True)
class WorkerOps:
    """Indirection over the floor worker so tests can observe lifecycle calls."""

    start: Callable[[Event, Room], Awaitable[None]] = start_floor_transcription_worker
    stop: Callable[[str, int], Awaitable[None]] = stop_floor_transcription_worker
    is_running: Callable[[str, int], bool] = floor_worker_running


async def load_program_ingest_rooms() -> list[Room]:
    async with get_session() as session:
        result = await session.execute(
            select(Room).options(joinedload(Room.event)).where(Room.floor_source_mode == FLOOR_SOURCE_PROGRAM_INGEST)
        )
        return list(result.scalars().all())


async def record_ingest_timestamp(room_id: int, *, connected: bool, when: datetime) -> None:
    column = "program_ingest_last_connected_at" if connected else "program_ingest_last_disconnected_at"
    async with get_session() as session:
        await session.execute(update(Room).where(Room.id == room_id).values({column: when}))


@dataclass
class ProgramIngestSupervisor:
    config: SupervisorConfig = field(default_factory=SupervisorConfig.from_settings)
    fetch_snapshots: Callable[[], Awaitable[dict[str, PathSnapshot] | None]] = fetch_path_snapshots
    clear_path_config: Callable[[str], Awaitable[bool]] = remove_path_config
    worker_ops: WorkerOps = field(default_factory=WorkerOps)
    clock: Callable[[], float] = time.monotonic
    statuses: dict[int, RoomIngestStatus] = field(default_factory=dict)
    # Rooms whose floor worker this supervisor started, with their event slug.
    owned_workers: dict[int, str] = field(default_factory=dict)
    # Rooms whose floor path MediaMTX confirmed has no runtime (alwaysAvailable) config.
    path_config_cleared: set[int] = field(default_factory=set)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def status_for(self, room_id: int) -> RoomIngestStatus | None:
        return self.statuses.get(room_id)

    def active_feed_count(self) -> int:
        return sum(1 for s in self.statuses.values() if s.online)

    def can_accept_new_feed(self, room_id: int) -> bool:
        current = self.statuses.get(room_id)
        if current is not None and current.online:
            return True
        return self.active_feed_count() < self.config.max_active

    def metrics(self) -> dict[str, int]:
        counts = {state.value: 0 for state in IngestState if state is not IngestState.DISABLED}
        for status in self.statuses.values():
            counts[status.state.value] = counts.get(status.state.value, 0) + 1
        counts["rooms"] = len(self.statuses)
        counts["workers"] = sum(1 for s in self.statuses.values() if s.worker_running)
        return counts

    async def release_room(self, room_id: int) -> None:
        """Forget a room that left program-ingest mode and stop the worker we started for it."""
        async with self._lock:
            await self.release_room_locked(room_id)

    async def release_room_locked(self, room_id: int) -> None:
        self.statuses.pop(room_id, None)
        self.path_config_cleared.discard(room_id)
        event_slug = self.owned_workers.pop(room_id, None)
        if event_slug is not None:
            await self.worker_ops.stop(event_slug, room_id)
            logger.info("program ingest released room_id=%s; floor worker stopped", room_id)

    async def tick(self) -> None:
        async with self._lock:
            rooms = await load_program_ingest_rooms()
            current_ids = {room.id for room in rooms}
            for stale_id in [rid for rid in self.statuses if rid not in current_ids]:
                await self.release_room_locked(stale_id)
            if not rooms:
                return
            paths = {room.id: make_mediamtx_path(room.event.slug, room.id, FLOOR_LANGUAGE) for room in rooms}
            await self.clear_pending_path_configs(paths)
            snapshots = await self.fetch_snapshots()
            for room in rooms:
                snapshot = None if snapshots is None else snapshots.get(paths[room.id], OFFLINE)
                await self.evaluate_room(room, snapshot)

    async def clear_pending_path_configs(self, paths: dict[int, str]) -> None:
        """Retry removing the Opus-only alwaysAvailable config until MediaMTX confirms it.

        The mode switch removes it once; if MediaMTX was briefly unavailable the
        encoder's tracks (e.g. H264 + AAC) could otherwise be refused. Attempts
        run concurrently so an unreachable MediaMTX costs one request timeout
        per tick, not one per room.
        """
        pending = [(room_id, path) for room_id, path in paths.items() if room_id not in self.path_config_cleared]
        if not pending:
            return
        results = await asyncio.gather(*(self.clear_path_config(path) for _, path in pending))
        for (room_id, _), cleared in zip(pending, results, strict=True):
            if cleared:
                self.path_config_cleared.add(room_id)

    async def evaluate_room(self, room: Room, snapshot: PathSnapshot | None) -> None:
        """Advance one room's state from its path snapshot (``None`` = MediaMTX unreachable)."""
        event = room.event
        status = self.statuses.setdefault(room.id, RoomIngestStatus(room_id=room.id))
        now = self.clock()

        if snapshot is None:
            status.state = IngestState.DEGRADED
            status.detail = "MediaMTX status is unavailable; cannot verify the feed."
            status.worker_running = self.worker_ops.is_running(event.slug, room.id)
            return

        if snapshot.online:
            await self.observe_online(room, status, snapshot, now)
        else:
            await self.observe_offline(room, status, now)
        status.worker_running = self.worker_ops.is_running(event.slug, room.id)

    async def observe_online(self, room: Room, status: RoomIngestStatus, snapshot: PathSnapshot, now: float) -> None:
        if not status.online:
            if status.ever_connected:
                status.reconnects += 1
            status.online = True
            status.ever_connected = True
            status.offline_since = None
            status.bytes_changed_at = now
            status.inbound_bytes = snapshot.inbound_bytes
            await record_ingest_timestamp(room.id, connected=True, when=utc_now())
            logger.info(
                "program ingest connected room_id=%s source_type=%s audio=%s video=%s",
                room.id,
                snapshot.source_type,
                ",".join(snapshot.audio_codecs) or "-",
                ",".join(snapshot.video_codecs) or "-",
            )
        elif status.source_id and snapshot.source_id and snapshot.source_id != status.source_id:
            status.replacements += 1
            status.bytes_changed_at = now
            logger.warning("program ingest publisher replaced room_id=%s source_type=%s", room.id, snapshot.source_type)

        if snapshot.inbound_bytes != status.inbound_bytes:
            status.inbound_bytes = snapshot.inbound_bytes
            status.bytes_changed_at = now
        status.source_type = snapshot.source_type
        status.source_id = snapshot.source_id
        status.audio_codecs = snapshot.audio_codecs
        status.video_codecs = snapshot.video_codecs
        status.unsupported_codecs = snapshot.unsupported_codecs

        if not snapshot.has_decodable_audio:
            await self.stop_owned_worker(room)
            status.state = IngestState.DEGRADED
            if snapshot.unsupported_codecs:
                status.detail = (
                    f"The feed has no decodable audio track (received {', '.join(snapshot.unsupported_codecs)}). "
                    "Send Opus or AAC audio."
                )
            else:
                status.detail = "Video is arriving but the feed has no audio track; captions cannot be produced."
            return

        stalled_for = now - status.bytes_changed_at
        if stalled_for >= self.config.stall_seconds:
            status.state = IngestState.DEGRADED
            status.detail = f"The encoder is connected but no media has arrived for {int(stalled_for)} s."
            return

        issue = floor_transcription_issue(room)
        if issue is not None:
            await self.stop_owned_worker(room)
            status.state = IngestState.RECEIVING
            status.detail = f"Receiving the program feed. {issue}"
            return

        try:
            await self.worker_ops.start(room.event, room)
        except ValueError as exc:
            status.state = IngestState.DEGRADED
            status.detail = f"Could not start floor transcription: {exc}"
            logger.warning("program ingest worker start failed room_id=%s: %s", room.id, exc)
            return
        self.owned_workers[room.id] = room.event.slug
        status.state = IngestState.PROCESSING
        status.detail = "Transcribing the program feed."

    async def observe_offline(self, room: Room, status: RoomIngestStatus, now: float) -> None:
        if status.online:
            status.online = False
            status.offline_since = now
            await record_ingest_timestamp(room.id, connected=False, when=utc_now())
            logger.info("program ingest disconnected room_id=%s", room.id)

        status.source_type = None
        status.source_id = None

        if status.offline_since is None:
            # Nothing has connected since this process started: make sure no
            # stale worker survives and the stored timestamps don't claim a live feed.
            await self.stop_owned_worker(room)
            await self.close_stale_connection(room)
            status.state = IngestState.WAITING
            status.detail = "Waiting for the encoder to connect."
            return

        offline_for = now - status.offline_since
        status.state = IngestState.DISCONNECTED
        if offline_for >= self.config.disconnect_grace_seconds:
            await self.stop_owned_worker(room)
            status.detail = "The encoder disconnected; floor transcription is stopped until it reconnects."
        else:
            remaining = int(self.config.disconnect_grace_seconds - offline_for)
            status.detail = f"The encoder disconnected; keeping transcription ready for {remaining} s."

    async def stop_owned_worker(self, room: Room) -> None:
        event_slug = self.owned_workers.pop(room.id, None)
        if event_slug is not None:
            await self.worker_ops.stop(event_slug, room.id)
            logger.info("program ingest floor worker stopped room_id=%s", room.id)
        elif self.worker_ops.is_running(room.event.slug, room.id):
            # A worker left over from before the room switched to program
            # ingest (or from a previous process state) must not keep reading.
            await self.worker_ops.stop(room.event.slug, room.id)

    async def close_stale_connection(self, room: Room) -> None:
        connected = room.program_ingest_last_connected_at
        disconnected = room.program_ingest_last_disconnected_at
        if connected is not None and (disconnected is None or disconnected < connected):
            when = utc_now()
            room.program_ingest_last_disconnected_at = when
            await record_ingest_timestamp(room.id, connected=False, when=when)

    async def run(self) -> None:
        logger.info("program ingest supervisor started (poll=%ss)", self.config.poll_seconds)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Supervision boundary: a failed tick must never kill the loop.
                logger.exception("program ingest supervisor tick failed")
            await asyncio.sleep(self.config.poll_seconds)


supervisor = ProgramIngestSupervisor()
