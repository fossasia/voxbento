"""Room floor transcription worker helpers shared by every floor source.

The floor worker reads ``{event_slug}/{room_id}/floor`` from MediaMTX no matter
who publishes it (Jitsi floor bot or Program Stream Ingest), and is keyed by
the floor booth id so at most one worker exists per room.
"""

from __future__ import annotations

import logging

import httpx

from portal.booth_identity import make_booth_id
from portal.config import settings
from portal.globals import get_http_client
from portal.models import Event, Room
from portal.transcription.constants import ProviderEnum
from portal.transcription.providers.base import ProviderConfig, get_api_key
from portal.transcription.worker import State, active_workers, start_transcription_worker, stop_transcription_worker
from portal.websockets.manager import broadcast_transcription

logger = logging.getLogger(__name__)

FLOOR_LANGUAGE = "floor"
DISABLED_PROVIDER = "none"


def floor_booth_id(event_slug: str, room_id: int) -> str:
    return make_booth_id(event_slug, room_id, FLOOR_LANGUAGE)


def floor_transcription_issue(room: Room) -> str | None:
    """Return why floor transcription cannot run for ``room``, or ``None`` if it can."""
    if not room.floor_transcription_enabled:
        return "Floor transcription is disabled for this room."
    if not room.floor_transcription_provider or room.floor_transcription_provider == DISABLED_PROVIDER:
        return "No floor transcription provider is configured."
    if room.floor_transcription_provider not in {p.value for p in ProviderEnum}:
        return "The configured floor transcription provider is not supported."
    if not room.floor_transcription_model or room.floor_transcription_model == DISABLED_PROVIDER:
        return "No floor transcription model is configured."
    return None


async def start_floor_transcription_worker(event: Event, room: Room) -> None:
    """Start (or keep) the room's single floor transcription worker.

    Idempotent: an already running worker for this room is left untouched.

    :raises ValueError: when the provider is invalid or worker capacity is exhausted.
    """
    provider = ProviderEnum(room.floor_transcription_provider)
    await start_transcription_worker(
        event_slug=event.slug,
        language_code=FLOOR_LANGUAGE,
        booth_id=floor_booth_id(event.slug, room.id),
        broadcast_callback=broadcast_transcription,
        provider=room.floor_transcription_provider,
        model_size=room.floor_transcription_model,
        config=ProviderConfig(api_key=get_api_key(event, provider)),
        transcription_language=room.floor_language_code,
        room_id=room.id,
    )


async def stop_floor_transcription_worker(event_slug: str, room_id: int) -> None:
    await stop_transcription_worker(floor_booth_id(event_slug, room_id))


def floor_worker_running(event_slug: str, room_id: int) -> bool:
    session = active_workers.get(floor_booth_id(event_slug, room_id))
    return session is not None and session.state in (State.STARTING, State.RUNNING)


async def request_floor_bot_stop(event_slug: str, room_id: int) -> None:
    """Ask the floor-bot service to leave the room's Jitsi meeting (best effort)."""
    try:
        await get_http_client().post(
            f"{settings.floor_bot_base}/stop", json={"event_slug": event_slug, "room_id": room_id}, timeout=15.0
        )
    except httpx.HTTPError as exc:
        logger.warning("Failed to stop floor-bot for room_id=%s: %s", room_id, exc)
