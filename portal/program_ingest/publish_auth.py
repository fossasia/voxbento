"""Publish authorization for MediaMTX room floor paths.

MediaMTX is configured with ``authMethod: http`` and calls the portal for
every publish. Floor paths (``{event_slug}/{room_id}/floor``) are owned by
exactly one source, selected by ``Room.floor_source_mode``:

* ``jitsi_bot`` — only the internal floor bot's RTSP publish is accepted.
* ``program_ingest`` — only an encoder presenting the room's unexpired
  credential over an allowed protocol is accepted; the floor bot is refused.

Interpreter booth paths keep their existing behaviour (application-level
gating via the WHIP URL endpoint plus MediaMTX ``overridePublisher``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from portal.booth_identity import validate_event_slug
from portal.config import settings
from portal.database import get_session
from portal.models import FLOOR_SOURCE_JITSI_BOT, FLOOR_SOURCE_PROGRAM_INGEST, Room
from portal.program_ingest.credentials import verify_room_ingest_secret

logger = logging.getLogger(__name__)

FLOOR_SEGMENT = "floor"
FLOOR_BOT_PROTOCOL = "rtsp"


class MediaMTXAuthRequest(BaseModel):
    """Payload MediaMTX POSTs to ``authHTTPAddress``. Never log it: it carries secrets."""

    user: str = ""
    password: str = ""
    token: str = ""
    ip: str = ""
    action: str = ""
    path: str = ""
    protocol: str = ""
    id: str | None = None
    query: str = ""


class DenyReason(str, Enum):
    UNKNOWN_ROOM = "unknown_room"
    PROTOCOL_NOT_ALLOWED = "protocol_not_allowed"
    INVALID_CREDENTIAL = "invalid_credential"
    RATE_LIMITED = "rate_limited"
    FLOOR_BOT_CONFLICT = "floor_bot_conflict"
    BOT_MODE_EXTERNAL_PUBLISH = "bot_mode_external_publish"
    CAPACITY = "capacity"


@dataclass(frozen=True)
class PublishDecision:
    allowed: bool
    room_id: int | None = None
    reason: DenyReason | None = None


@dataclass(frozen=True)
class FloorPath:
    event_slug: str
    room_id: int


def parse_floor_path(path: str) -> FloorPath | None:
    parts = path.strip().strip("/").split("/")
    if len(parts) != 3 or parts[2] != FLOOR_SEGMENT:
        return None
    if not (parts[1].isascii() and parts[1].isdigit()):
        return None
    try:
        slug = validate_event_slug(parts[0])
    except ValueError:
        return None
    return FloorPath(event_slug=slug, room_id=int(parts[1]))


class AuthFailureLimiter:
    """Sliding-window count of failed program-ingest publishes per client IP."""

    def __init__(self, max_failures: int, window_seconds: float) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, ip: str, now: float) -> deque[float]:
        bucket = self._failures.setdefault(ip, deque())
        while bucket and now - bucket[0] > self.window_seconds:
            bucket.popleft()
        if not bucket:
            self._failures.pop(ip, None)
        return bucket

    def is_blocked(self, ip: str, now: float | None = None) -> bool:
        return len(self._prune(ip, now if now is not None else time.monotonic())) >= self.max_failures

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        self._prune(ip, now)
        self._failures.setdefault(ip, deque()).append(now)

    def reset(self) -> None:
        self._failures.clear()


failure_limiter = AuthFailureLimiter(
    max_failures=settings.program_ingest_auth_max_failures,
    window_seconds=settings.program_ingest_auth_failure_window_seconds,
)


async def load_room_for_floor_path(floor_path: FloorPath) -> Room | None:
    async with get_session() as session:
        room = await session.scalar(select(Room).options(joinedload(Room.event)).where(Room.id == floor_path.room_id))
    if room is None or room.event is None or room.event.slug != floor_path.event_slug:
        return None
    return room


def presented_secret(req: MediaMTXAuthRequest) -> str:
    # OBS "Bearer Token" (WHIP) arrives as ``token``; Basic auth, RTMP
    # ``pass=``, SRT stream IDs and RTSP userinfo arrive as ``password``.
    return req.token or req.password


async def authorize_publish(
    req: MediaMTXAuthRequest, can_accept_new_feed: Callable[[int], bool] | None = None
) -> PublishDecision:
    """Decide whether a MediaMTX publish request may proceed.

    ``can_accept_new_feed`` is an optional ``(room_id) -> bool`` capacity check
    consulted only for an otherwise-valid program ingest publish.
    """
    if req.action != "publish":
        return PublishDecision(allowed=True)
    floor_path = parse_floor_path(req.path)
    if floor_path is None:
        return PublishDecision(allowed=True)
    # Checked before any lookup so a blocked client learns nothing about rooms.
    if failure_limiter.is_blocked(req.ip):
        return _deny(req, floor_path.room_id, DenyReason.RATE_LIMITED, count_failure=False)

    room = await load_room_for_floor_path(floor_path)
    if room is None:
        return _deny(req, None, DenyReason.UNKNOWN_ROOM)

    if room.floor_source_mode == FLOOR_SOURCE_JITSI_BOT:
        if req.protocol == FLOOR_BOT_PROTOCOL:
            return PublishDecision(allowed=True, room_id=room.id)
        return _deny(req, room.id, DenyReason.BOT_MODE_EXTERNAL_PUBLISH)

    if room.floor_source_mode != FLOOR_SOURCE_PROGRAM_INGEST:
        return _deny(req, room.id, DenyReason.UNKNOWN_ROOM)

    secret = presented_secret(req)
    if req.protocol == FLOOR_BOT_PROTOCOL and not secret:
        # The floor bot publishes over RTSP without credentials; program
        # ingest owns this room, so the bot is refused deterministically.
        return _deny(req, room.id, DenyReason.FLOOR_BOT_CONFLICT)
    if req.protocol not in settings.program_ingest_protocol_set:
        return _deny(req, room.id, DenyReason.PROTOCOL_NOT_ALLOWED)
    if not await asyncio.to_thread(verify_room_ingest_secret, room, secret):
        return _deny(req, room.id, DenyReason.INVALID_CREDENTIAL)
    if can_accept_new_feed is not None and not can_accept_new_feed(room.id):
        return _deny(req, room.id, DenyReason.CAPACITY, count_failure=False)

    logger.info("program ingest publish accepted room_id=%s protocol=%s ip=%s", room.id, req.protocol, req.ip)
    return PublishDecision(allowed=True, room_id=room.id)


def _deny(
    req: MediaMTXAuthRequest, room_id: int | None, reason: DenyReason, *, count_failure: bool = True
) -> PublishDecision:
    if count_failure and reason in (DenyReason.INVALID_CREDENTIAL, DenyReason.UNKNOWN_ROOM):
        failure_limiter.record_failure(req.ip)
    logger.warning(
        "floor publish denied room_id=%s reason=%s protocol=%s ip=%s", room_id, reason.value, req.protocol, req.ip
    )
    return PublishDecision(allowed=False, room_id=room_id, reason=reason)
