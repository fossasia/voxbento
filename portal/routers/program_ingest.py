"""Room-level Program Stream Ingest management (event owners).

Served under ``/admin/events/{event_id}/rooms/{room_id}/program-ingest/*`` and
reachable from ``/workspace/...`` through the workspace routing middleware.
The publish secret is returned exactly once, by the credential endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from portal.auth import require_event_owner
from portal.booth_identity import make_mediamtx_path
from portal.config import settings
from portal.database import get_session
from portal.models import FLOOR_SOURCE_MODES, FLOOR_SOURCE_PROGRAM_INGEST, Room
from portal.program_ingest.credentials import (
    apply_issued_secret,
    is_secret_expired,
    issue_ingest_secret,
    revoke_ingest_secret,
)
from portal.program_ingest.mediamtx import PublisherKickError, kick_publisher, remove_path_config
from portal.program_ingest.supervisor import IngestState, supervisor
from portal.transcription.floor import (
    FLOOR_LANGUAGE,
    floor_transcription_issue,
    request_floor_bot_stop,
    stop_floor_transcription_worker,
)
from portal.utils import safe_redirect
from portal.workspace_routing import management_url

logger = logging.getLogger(__name__)

router = APIRouter()

ROOM_PREFIX = "/admin/events/{event_id}/rooms/{room_id}/program-ingest"
MAX_SYNC_OFFSET_MS = 30_000
NO_STORE = {"Cache-Control": "no-store"}
KICK_FAILED_DETAIL = (
    "Could not disconnect the current floor publisher from MediaMTX, so nothing was changed. "
    "Check that MediaMTX is running and try again."
)


class CredentialRequest(BaseModel):
    expires_in_days: int | None = None


async def load_room(event_id: int, room_id: int) -> Room:
    async with get_session() as session:
        room = await session.scalar(
            select(Room)
            .options(joinedload(Room.event), selectinload(Room.translation_languages))
            .where(Room.id == room_id)
        )
    if room is None or room.event_id != event_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Room not found.")
    return room


def floor_path(room: Room, event_slug: str) -> str:
    return make_mediamtx_path(event_slug, room.id, FLOOR_LANGUAGE)


def ingest_url(room: Room, event_slug: str) -> str:
    return f"{settings.effective_program_ingest_public_base}/{floor_path(room, event_slug)}/whip"


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    # SQLite returns naive datetimes; every stored timestamp is UTC.
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()


def ingest_warnings(room: Room) -> list[str]:
    warnings: list[str] = []
    issue = floor_transcription_issue(room)
    if issue:
        warnings.append(f"{issue} The feed will be received but not transcribed.")
    elif not room.floor_language_code:
        warnings.append("No floor source language is set; the provider will auto-detect the spoken language.")
    if not room.program_ingest_secret_hash:
        warnings.append("No ingest credential exists yet. Generate one before configuring the encoder.")
    elif is_secret_expired(room):
        warnings.append("The ingest credential has expired. Rotate it and update the encoder.")
    if room.floor_translation_enabled and not any(lang.enabled for lang in room.translation_languages):
        warnings.append("Floor translation is enabled but no target languages are selected.")
    if room.floor_tts_enabled and not room.floor_translation_enabled:
        warnings.append("Floor TTS needs floor translation to be enabled.")
    return warnings


def ingest_status_payload(room: Room, event_slug: str) -> dict[str, object]:
    """Everything the room page needs about ingest. Never includes the secret or its digest."""
    enabled = room.floor_source_mode == FLOOR_SOURCE_PROGRAM_INGEST
    live = supervisor.status_for(room.id) if enabled else None
    if live is not None:
        state: dict[str, object] = live.as_public_dict()
    elif enabled:
        state = {"state": IngestState.WAITING.value, "detail": "Waiting for the first status check."}
    else:
        state = {"state": IngestState.DISABLED.value, "detail": "The Jitsi floor bot is the floor source."}
    return {
        "mode": room.floor_source_mode,
        "enabled": enabled,
        **state,
        "ingest_url": ingest_url(room, event_slug),
        "credential": {
            "configured": bool(room.program_ingest_secret_hash),
            "hint": room.program_ingest_secret_hint,
            "created_at": iso(room.program_ingest_secret_created_at),
            "expires_at": iso(room.program_ingest_secret_expires_at),
            "expired": is_secret_expired(room),
        },
        "last_connected_at": iso(room.program_ingest_last_connected_at),
        "last_disconnected_at": iso(room.program_ingest_last_disconnected_at),
        "sync_offset_ms": room.program_sync_offset_ms,
        "warnings": ingest_warnings(room),
    }


def room_page(request: Request, event_id: int, room_id: int) -> RedirectResponse:
    return safe_redirect(
        url=management_url(request, f"events/{event_id}/rooms/{room_id}/"), status_code=status.HTTP_303_SEE_OTHER
    )


async def disconnect_before_change(path: str) -> None:
    """Disconnect the current floor publisher, or abort the change with 503.

    MediaMTX authenticates only at connect time, so a mode switch, rotation or
    revocation must not be committed while the old publisher may stay live.
    Nobody publishing counts as success.
    """
    try:
        await kick_publisher(path)
    except PublisherKickError as exc:
        logger.warning("floor publisher disconnect failed; change aborted path=%s: %s", path, exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=KICK_FAILED_DETAIL) from exc


async def disconnect_after_change(path: str) -> None:
    """Kick a publisher that reconnected under the old rules before the change was committed."""
    try:
        await kick_publisher(path)
    except PublisherKickError as exc:
        logger.warning("floor publisher re-check after change failed path=%s: %s", path, exc)


@router.get(f"{ROOM_PREFIX}/status", dependencies=[Depends(require_event_owner)])
async def program_ingest_status(event_id: int, room_id: int) -> JSONResponse:
    room = await load_room(event_id, room_id)
    return JSONResponse(ingest_status_payload(room, room.event.slug), headers=NO_STORE)


@router.post(f"{ROOM_PREFIX}/mode", dependencies=[Depends(require_event_owner)])
async def program_ingest_set_mode(request: Request, event_id: int, room_id: int) -> RedirectResponse:
    form = await request.form()
    mode = str(form.get("floor_source_mode", "")).strip()
    if mode not in FLOOR_SOURCE_MODES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown floor source mode.")
    room = await load_room(event_id, room_id)
    previous = room.floor_source_mode
    if mode == previous:
        return room_page(request, event_id, room_id)

    event_slug = room.event.slug
    path = floor_path(room, event_slug)
    await disconnect_before_change(path)

    async with get_session() as session:
        stored = await session.get(Room, room_id)
        stored.floor_source_mode = mode

    if mode == FLOOR_SOURCE_PROGRAM_INGEST:
        # Hand the floor path over: the bot leaves and its worker stops. The
        # Opus-only alwaysAvailable config is dropped here and re-checked by
        # the supervisor until MediaMTX confirms it is gone.
        await request_floor_bot_stop(event_slug, room_id)
        await stop_floor_transcription_worker(event_slug, room_id)
        await remove_path_config(path)
    else:
        await supervisor.release_room(room_id)
        await stop_floor_transcription_worker(event_slug, room_id)
    await disconnect_after_change(path)
    logger.info("floor source changed room_id=%s from=%s to=%s", room_id, previous, mode)
    return room_page(request, event_id, room_id)


@router.post(f"{ROOM_PREFIX}/credential", dependencies=[Depends(require_event_owner)])
async def program_ingest_rotate_credential(event_id: int, room_id: int, body: CredentialRequest) -> JSONResponse:
    """Issue (or rotate) the room's publish secret and return it this one time."""
    room = await load_room(event_id, room_id)
    try:
        # bcrypt hashing is CPU-bound; keep it off the event loop.
        issued = await asyncio.to_thread(issue_ingest_secret, body.expires_in_days)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    replaced = bool(room.program_ingest_secret_hash)
    path = floor_path(room, room.event.slug)
    if replaced:
        # The previous credential may still hold an authenticated session.
        await disconnect_before_change(path)
    async with get_session() as session:
        apply_issued_secret(await session.get(Room, room_id), issued)
    if replaced:
        await disconnect_after_change(path)
    logger.info("program ingest credential %s room_id=%s", "rotated" if replaced else "issued", room_id)
    return JSONResponse(
        {
            "secret": issued.secret,
            "hint": issued.hint,
            "created_at": iso(issued.created_at),
            "expires_at": iso(issued.expires_at),
            "ingest_url": ingest_url(room, room.event.slug),
        },
        headers=NO_STORE,
    )


@router.post(f"{ROOM_PREFIX}/credential/revoke", dependencies=[Depends(require_event_owner)])
async def program_ingest_revoke_credential(event_id: int, room_id: int) -> JSONResponse:
    room = await load_room(event_id, room_id)
    path = floor_path(room, room.event.slug)
    await disconnect_before_change(path)
    async with get_session() as session:
        revoke_ingest_secret(await session.get(Room, room_id))
    await disconnect_after_change(path)
    logger.info("program ingest credential revoked room_id=%s", room_id)
    return JSONResponse({"revoked": True}, headers=NO_STORE)


@router.post(f"{ROOM_PREFIX}/sync-offset", dependencies=[Depends(require_event_owner)])
async def program_ingest_set_sync_offset(request: Request, event_id: int, room_id: int) -> RedirectResponse:
    form = await request.form()
    try:
        offset_ms = int(str(form.get("program_sync_offset_ms", "0")).strip() or "0")
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Sync offset must be an integer.")
    if not 0 <= offset_ms <= MAX_SYNC_OFFSET_MS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Sync offset must be between 0 and {MAX_SYNC_OFFSET_MS} ms.",
        )
    await load_room(event_id, room_id)
    async with get_session() as session:
        (await session.get(Room, room_id)).program_sync_offset_ms = offset_ms
    return room_page(request, event_id, room_id)
