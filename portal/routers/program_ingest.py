from __future__ import annotations

import hmac
import ipaddress
import json
import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from portal.auth import require_event_owner
from portal.booth_identity import make_mediamtx_path
from portal.config import settings
from portal.database import get_event_by_id, get_room_by_id, get_session
from portal.program_ingest import MediaAuth, authorize_publish, configure_source, health, room_lock

router = APIRouter()
logger = logging.getLogger(__name__)


class IngestChange(BaseModel):
    action: Literal["enable", "rotate", "revoke", "disable", "sync"]
    sync_offset_ms: int = Field(default=0, ge=0, le=120000)


@router.post("/internal/media-auth", include_in_schema=False)
async def media_auth(request: Request) -> Response:
    # Defence in depth; Caddy blocks this route and port 8000 is loopback-only.
    try:
        private = request.client is not None and ipaddress.ip_address(request.client.host).is_private
    except ValueError:
        private = False
    if not private:
        raise HTTPException(403, "Forbidden")
    hook_secret = request.query_params.get("key", "")
    expected_secret = settings.mediamtx_auth_hook_secret
    if expected_secret:
        if not hmac.compare_digest(hook_secret.encode(), expected_secret.encode()):
            raise HTTPException(401, "Publish authorization failed")
    elif not settings.debug:
        raise HTTPException(503, "Media authorization is not configured")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 8192:
            raise HTTPException(413, "Request too large")
    try:
        data = MediaAuth.model_validate(json.loads(body))
    except (ValueError, ValidationError):
        raise HTTPException(401, "Publish authorization failed") from None
    try:
        await authorize_publish(data)
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "Media control unavailable") from None
    return Response(status_code=204)


@router.get("/admin/events/{event_id}/rooms/{room_id}/program-ingest", dependencies=[Depends(require_event_owner)])
async def ingest_status(event_id: int, room_id: int) -> JSONResponse:
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        event = await get_event_by_id(session, event_id)
        if not room or room.event_id != event_id or not event:
            raise HTTPException(404, "Room not found")
        current = health.get(room_id)
        enabled = room.floor_source == "program_ingest"
        warnings = []
        if not room.floor_transcription_enabled:
            warnings.append("Enable floor transcription to generate captions.")
        if not room.floor_language_code or room.floor_transcription_provider == "none":
            warnings.append("Choose a floor source language and transcription provider.")
        return JSONResponse(
            {
                "available": settings.program_ingest_enabled,
                "enabled": enabled,
                "state": (current.state if current else "waiting") if enabled else "disabled",
                "reason": current.reason if enabled and current else "",
                "endpoint": f"{settings.public_base_url.rstrip('/')}/{make_mediamtx_path(event.slug, room_id, 'floor')}/whip",
                "credential_present": bool(room.program_key_hash),
                "expires_at": room.program_key_expires_at.isoformat() if room.program_key_expires_at else None,
                "last_connected_at": room.program_connected_at.isoformat() if room.program_connected_at else None,
                "last_disconnected_at": room.program_disconnected_at.isoformat()
                if room.program_disconnected_at
                else None,
                "codecs": current.codecs if current else [],
                "sync_offset_ms": room.program_sync_offset_ms,
                "warnings": warnings,
            },
            headers={"Cache-Control": "no-store"},
        )


@router.post("/admin/events/{event_id}/rooms/{room_id}/program-ingest", dependencies=[Depends(require_event_owner)])
async def ingest_change(request: Request, event_id: int, room_id: int, change: IngestChange) -> JSONResponse:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.public_base_url.rstrip("/"):
        raise HTTPException(403, "Origin mismatch")
    if change.action in {"enable", "rotate"} and not settings.program_ingest_enabled:
        raise HTTPException(409, "Program ingest must first be enabled by the server operator")
    secret = None
    cleanup_pending = False
    async with room_lock(room_id):
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            event = await get_event_by_id(session, event_id)
            if not room or room.event_id != event_id or not event:
                raise HTTPException(404, "Room not found")
            if change.action in {"rotate", "revoke"} and room.floor_source != "program_ingest":
                raise HTTPException(409, "Enable program ingest first")
            if change.action == "sync":
                room.program_sync_offset_ms = change.sync_offset_ms
            elif change.action == "revoke":
                # Commit invalidation before network cleanup. A failed kick must
                # never roll the old credential back into validity.
                room.program_key_hash = None
                room.program_key_expires_at = None
            else:
                try:
                    secret = await configure_source(room, event, change.action, room.program_sync_offset_ms)
                except httpx.HTTPError:
                    raise HTTPException(
                        502, "Could not confirm floor source shutdown; no credential was issued. Retry."
                    ) from None

        if change.action == "revoke":
            async with get_session() as session:
                room = await get_room_by_id(session, room_id)
                event = await get_event_by_id(session, event_id)
                if room is None or event is None:
                    raise HTTPException(404, "Room not found")
                try:
                    await configure_source(room, event, change.action, room.program_sync_offset_ms)
                except (httpx.HTTPError, HTTPException) as exc:
                    cleanup_pending = True
                    logger.warning(
                        "program_ingest revoke_cleanup_pending room_id=%s error=%s",
                        room_id,
                        type(exc).__name__,
                    )
    body = {"secret": secret}
    if cleanup_pending:
        body["cleanup_pending"] = True
    return JSONResponse(
        body,
        status_code=202 if cleanup_pending else 200,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
