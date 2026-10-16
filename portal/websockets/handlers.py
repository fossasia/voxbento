from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from portal.auth import WSAuthError, resolve_booth_role, resolve_ws_auth
from portal.websockets.booth_protocol import booth_protocol
from portal.websockets.manager import (
    Session,
    listener_manager,
    manager,
    tts_manager,
)

_log = logging.getLogger(__name__)


router = APIRouter()


@router.websocket("/ws/booth/{booth_id}")
async def ws_booth(websocket: WebSocket, booth_id: str) -> None:
    try:
        payload = await resolve_ws_auth(websocket, booth_id)
    except WSAuthError:
        return

    if payload and payload.get("role") == "listener":
        await websocket.close(code=4003)
        return

    ws_granted_role = await resolve_booth_role(payload, booth_id)
    await websocket.accept()

    from portal.database import get_booth_language_name

    language_name = await get_booth_language_name(booth_id)

    session = Session(
        booth_id=booth_id,
        participant_id=None,
        language=language_name,
        channel_id=f"{booth_id}-audio",
        granted_role=ws_granted_role,
    )
    manager.add(websocket, session)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "booth:error", "message": "Invalid JSON."}))
                continue

            await booth_protocol.dispatch(websocket, session, data)
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(websocket)
        await booth_protocol.handle_disconnect(session)


@router.websocket("/ws/captions/{booth_id}")
async def ws_captions(websocket: WebSocket, booth_id: str) -> None:
    """WebSocket endpoint for live captions. Listener tokens are limited to their own event"""
    try:
        await resolve_ws_auth(websocket, booth_id)
    except WSAuthError:
        return
    await websocket.accept()
    listener_manager.add(websocket, booth_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        listener_manager.remove(websocket, booth_id)


@router.websocket("/ws/tts/{room_id}/{language_code}/{booth_id}")
async def ws_tts(websocket: WebSocket, room_id: int, language_code: str, booth_id: str) -> None:
    await websocket.accept()
    tts_manager.add(websocket, room_id, language_code, booth_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        tts_manager.remove(websocket, room_id, language_code, booth_id)
