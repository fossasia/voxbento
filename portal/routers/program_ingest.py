from __future__ import annotations

import hmac
import ipaddress
import json
import logging
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from portal import program_relay
from portal.auth import require_event_owner
from portal.booth_identity import make_mediamtx_path
from portal.config import settings
from portal.database import get_event_by_id, get_room_by_id, get_session
from portal.program_ingest import (
    MediaAuth,
    authorize_publish,
    close_sender_session,
    configure_source,
    health,
    open_sender_session,
    room_lock,
)

router = APIRouter()
logger = logging.getLogger(__name__)


class IngestChange(BaseModel):
    action: Literal["enable", "rotate", "revoke", "disable", "sync"]
    sync_offset_ms: int = Field(default=0, ge=0, le=120000)
    # Only read on "enable": the transport the new credential is bound to.
    mode: Literal["whip", "rtmps_relay"] = "whip"


class RelayDestination(BaseModel):
    server_url: str = Field(min_length=1, max_length=255)
    stream_key: str = Field(min_length=1, max_length=128, repr=False)


class SenderSessionRequest(BaseModel):
    # Omitted keeps the room's saved YouTube sync delay, so a sender (re)start never resets it.
    sync_offset_ms: int | None = Field(default=None, ge=0, le=120000)


def _rtmps_server(event_slug: str, room_id: int) -> str:
    # OBS joins "Server" and "Stream Key" with "/", so the key carries the floor path segment.
    base = settings.program_rtmps_public_base.rstrip("/")
    return f"{base}/{event_slug}/{room_id}" if base else ""


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != settings.public_base_url.rstrip("/"):
        raise HTTPException(403, "Origin mismatch")


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
        relay_mode = enabled and room.program_ingest_mode == program_relay.RELAY_MODE
        relay = program_relay.relay_health.get(room_id) if relay_mode else None
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
                "mode": room.program_ingest_mode,
                "relay_available": settings.program_ingest_enabled and settings.program_relay_enabled,
                "rtmps_server": _rtmps_server(event.slug, room_id),
                # Write-only: only the non-secret server URL and the change time are ever returned.
                "destination": {
                    "configured": bool(room.program_relay_server and room.program_relay_key_enc),
                    "server_url": room.program_relay_server,
                    "updated_at": room.program_relay_updated_at.isoformat() if room.program_relay_updated_at else None,
                },
                "health": {
                    "ingest": {
                        "state": (current.ingest_state if current else "waiting") if enabled else "disabled",
                        "reason": current.ingest_reason if enabled and current else "",
                    },
                    "relay": {
                        "state": (relay.state if relay else "idle") if relay_mode else "disabled",
                        "reason": relay.reason if relay else "",
                    },
                    "processing": {
                        "state": (current.processing_state if current else "idle") if enabled else "disabled",
                        "reason": current.processing_reason if enabled and current else "",
                    },
                },
            },
            headers={"Cache-Control": "no-store"},
        )


@router.post("/admin/events/{event_id}/rooms/{room_id}/program-ingest", dependencies=[Depends(require_event_owner)])
async def ingest_change(request: Request, event_id: int, room_id: int, change: IngestChange) -> JSONResponse:
    _check_origin(request)
    if change.action in {"enable", "rotate"} and not settings.program_ingest_enabled:
        raise HTTPException(409, "Program ingest must first be enabled by the server operator")
    if change.action == "enable" and change.mode == program_relay.RELAY_MODE and not settings.program_relay_enabled:
        raise HTTPException(409, "YouTube relay mode must first be enabled by the server operator")
    secret = None
    relay_mode = False
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
                room.program_session_secret_hash = None
                room.program_session_secret_expires_at = None
                # Clear the reservation too, so a failed cleanup kick cannot leave the
                # session permitted by reconcile's live-session check.
                room.program_session_id = None
            else:
                try:
                    secret = await configure_source(
                        room, event, change.action, room.program_sync_offset_ms, change.mode
                    )
                except httpx.HTTPError:
                    raise HTTPException(
                        502, "Could not confirm floor source shutdown; no credential was issued. Retry."
                    ) from None
            relay_mode = room.program_ingest_mode == program_relay.RELAY_MODE

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
    if secret and relay_mode:
        body["stream_key"] = f"floor?key={secret}"
    if cleanup_pending:
        body["cleanup_pending"] = True
    return JSONResponse(
        body,
        status_code=202 if cleanup_pending else 200,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


SENDER_PATH = "/admin/events/{event_id}/rooms/{room_id}/program-sender"


@router.post(SENDER_PATH, dependencies=[Depends(require_event_owner)])
async def sender_session_start(
    request: Request, event_id: int, room_id: int, body: SenderSessionRequest
) -> JSONResponse:
    _check_origin(request)
    if not settings.program_ingest_enabled:
        raise HTTPException(409, "Program ingest must first be enabled by the server operator")
    async with room_lock(room_id):
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            event = await get_event_by_id(session, event_id)
            if not room or room.event_id != event_id or not event:
                raise HTTPException(404, "Room not found")
            try:
                offset_ms = room.program_sync_offset_ms if body.sync_offset_ms is None else body.sync_offset_ms
                secret = await open_sender_session(room, event, offset_ms, settings.program_sender_session_ttl_secs)
            except httpx.HTTPError:
                raise HTTPException(
                    502, "Could not confirm floor availability; no session was issued. Retry."
                ) from None
            endpoint = f"{settings.public_base_url.rstrip('/')}/{make_mediamtx_path(event.slug, room_id, 'floor')}/whip"
    return JSONResponse(
        {"secret": secret, "endpoint": endpoint, "expires_in": settings.program_sender_session_ttl_secs},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.delete(SENDER_PATH, dependencies=[Depends(require_event_owner)])
async def sender_session_stop(
    request: Request,
    event_id: int,
    room_id: int,
    x_program_session: str | None = Header(default=None),
) -> JSONResponse:
    _check_origin(request)
    async with room_lock(room_id):
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            event = await get_event_by_id(session, event_id)
            if not room or room.event_id != event_id or not event:
                raise HTTPException(404, "Room not found")
            try:
                await close_sender_session(room, event, x_program_session)
            except httpx.HTTPError:
                logger.warning("program_ingest sender_close_cleanup_pending room_id=%s", room_id)
    return JSONResponse({"stopped": True}, headers={"Cache-Control": "no-store"})


RELAY_PATH = "/admin/events/{event_id}/rooms/{room_id}/program-relay"


@router.put(RELAY_PATH, dependencies=[Depends(require_event_owner)])
async def relay_destination_set(
    request: Request, event_id: int, room_id: int, destination: RelayDestination
) -> JSONResponse:
    """Store or rotate the YouTube destination. The stream key is write-only from here on."""
    _check_origin(request)
    if not settings.program_relay_enabled:
        raise HTTPException(409, "YouTube relay mode must first be enabled by the server operator")
    async with room_lock(room_id):
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            if not room or room.event_id != event_id:
                raise HTTPException(404, "Room not found")
            try:
                program_relay.store_destination(room, destination.server_url, destination.stream_key)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            except RuntimeError:
                raise HTTPException(409, "The server encryption key is not configured; ask the operator.") from None
            server_url = room.program_relay_server
    # A running relay restarts on the new destination at the next reconcile (fingerprint change).
    logger.info("program_relay destination_set room_id=%s", room_id)
    return JSONResponse({"configured": True, "server_url": server_url}, headers={"Cache-Control": "no-store"})


@router.delete(RELAY_PATH, dependencies=[Depends(require_event_owner)])
async def relay_destination_clear(request: Request, event_id: int, room_id: int) -> JSONResponse:
    """Revoke the stored YouTube destination and stop the room's relay."""
    _check_origin(request)
    cleanup_pending = False
    async with room_lock(room_id):
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            if not room or room.event_id != event_id:
                raise HTTPException(404, "Room not found")
            # Commit the revocation first; a failed stop is retried by the reconciler.
            program_relay.clear_destination(room)
        try:
            await program_relay.stop_relay(room_id)
        except httpx.HTTPError:
            cleanup_pending = True
            logger.warning("program_relay clear_cleanup_pending room_id=%s", room_id)
    logger.info("program_relay destination_cleared room_id=%s", room_id)
    return JSONResponse(
        {"configured": False, "cleanup_pending": cleanup_pending},
        status_code=202 if cleanup_pending else 200,
        headers={"Cache-Control": "no-store"},
    )
