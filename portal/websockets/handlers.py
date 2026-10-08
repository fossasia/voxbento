from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from portal.auth import WSAuthError, resolve_booth_role, resolve_ws_auth
from portal.globals import booths
from portal.websockets.manager import (
    Session,
    _handle_accept_handoff,
    _handle_cancel_handoff,
    _handle_chat,
    _handle_initiate_handoff,
    _handle_join,
    _handle_leave,
    _handle_set_active,
    _handle_set_broadcast_unlocked,
    _handle_update_state,
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

            msg_type = data.get("type", "")
            if msg_type == "booth:join":
                await _handle_join(websocket, session, data)
            elif msg_type == "booth:leave":
                await _handle_leave(session)
            elif msg_type == "booth:chat":
                await _handle_chat(websocket, session, data)
            elif msg_type == "booth:set-active":
                await _handle_set_active(websocket, session, data)
            elif msg_type == "booth:update-state":
                await _handle_update_state(websocket, session, data)
            elif msg_type == "booth:set-broadcast-unlocked":
                await _handle_set_broadcast_unlocked(websocket, session, data)
            elif msg_type == "booth:initiate-handoff":
                await _handle_initiate_handoff(websocket, session, data)
            elif msg_type == "booth:accept-handoff":
                await _handle_accept_handoff(websocket, session, data)
            elif msg_type == "booth:cancel-handoff":
                await _handle_cancel_handoff(websocket, session, data)
            else:
                await websocket.send_text(
                    json.dumps({"type": "booth:error", "message": f"Unknown message type: {msg_type}"})
                )
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(websocket)
        if session.participant_id:
            state = await booths.leave_participant(
                session.booth_id,
                session.participant_id,
                session.language,
                session.channel_id,
            )
            await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


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


async def _authorize_tts(websocket: WebSocket, room_id: int, language_code: str, booth_id: str) -> None:
    import jwt
    from urllib.parse import urlparse
    from portal.auth import decode_token, get_booth_session, WSAuthError
    from portal.booth_identity import parse_booth_id
    from portal.config import settings
    from portal.database import get_event_by_slug, get_session

    try:
        event_slug, actual_room, actual_lang = parse_booth_id(booth_id)
    except ValueError:
        raise WSAuthError("Invalid booth_id format.")

    if room_id != actual_room or language_code != actual_lang:
        raise WSAuthError("Mismatched room or language.")

    # 1. JWT in query param
    token = websocket.query_params.get("token", "")
    if token:
        try:
            payload = decode_token(token)
        except jwt.InvalidTokenError:
            raise WSAuthError("Invalid token.")

        if payload.get("is_admin") or payload.get("admin"):
            return

        token_event = payload.get("event_slug", "")
        if payload.get("role") == "listener":
            if not token_event or token_event != event_slug:
                raise WSAuthError("Listener token event_slug does not match.")
            return

        token_lang = payload.get("language_code", "")
        token_room = payload.get("room_id")
        if token_event and token_lang:
            if token_event != event_slug or token_lang != actual_lang or (token_room is not None and str(token_room) != str(actual_room)):
                raise WSAuthError("Participant token scope does not match.")
            return

        if payload.get("role") or payload.get("is_admin") or payload.get("admin"):
            return

    # 2. JWT in cookie
    payload = get_booth_session(websocket)
    if payload:
        if payload.get("is_admin") or payload.get("admin"):
            return

        token_event = payload.get("event_slug", "")
        if payload.get("role") == "listener":
            if not token_event or token_event != event_slug:
                raise WSAuthError("Listener token event_slug does not match.")
            return

        token_lang = payload.get("language_code", "")
        token_room = payload.get("room_id")
        if token_event and token_lang:
            if token_event != event_slug or token_lang != actual_lang or (token_room is not None and str(token_room) != str(actual_room)):
                raise WSAuthError("Participant token scope does not match.")
            return
            
        if payload.get("user") or payload.get("role"):
            return

    # 3. Listener code cookie
    # For requests without a token that rely on the cookie, check Origin strictly
    origin = websocket.headers.get("origin")
    if origin:
        parsed_origin = urlparse(origin)
        expected_pub = urlparse(settings.public_base_url)
        
        ws_scheme = websocket.url.scheme
        if ws_scheme == "ws":
            ws_scheme = "http"
        elif ws_scheme == "wss":
            ws_scheme = "https"
            
        allowed_origins = {
            (expected_pub.scheme, expected_pub.netloc),
            (ws_scheme, websocket.url.netloc)
        }
        if (parsed_origin.scheme, parsed_origin.netloc) not in allowed_origins:
            raise WSAuthError("Origin mismatch.")

    cookie_code = websocket.cookies.get(f"listener_code_{event_slug}")
    if cookie_code:
        async with get_session() as db_session:
            ev = await get_event_by_slug(db_session, event_slug)
            if not ev or not ev.listener_join_code or cookie_code != ev.listener_join_code:
                raise WSAuthError("Invalid listener join code.")
        return

    raise WSAuthError("Unauthorized.")


@router.websocket("/ws/tts/{room_id}/{language_code}/{booth_id}")
async def ws_tts(websocket: WebSocket, room_id: int, language_code: str, booth_id: str) -> None:
    try:
        await _authorize_tts(websocket, room_id, language_code, booth_id)
    except WSAuthError:
        await websocket.close(code=4003)
        return
        
    await websocket.accept()
    tts_manager.add(websocket, room_id, language_code, booth_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        tts_manager.remove(websocket, room_id, language_code, booth_id)
