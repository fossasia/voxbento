from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from portal.auth import (
    WSAuthError,
    booth_session_key,
    decode_token,
    is_listener_token,
    listener_scope_matches,
    resolve_booth_role,
    resolve_ws_auth,
    ws_bearer_subprotocol,
    ws_credential,
)
from portal.globals import booths
from portal.websockets.manager import (
    Session,
    _handle_accept_handoff,
    _handle_cancel_handoff,
    _handle_chat,
    _handle_initiate_handoff,
    _handle_join,
    _handle_leave,
    _handle_request_publish_ticket,
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

    if is_listener_token(payload):
        await websocket.close(code=4003)
        return

    ws_granted_role = await resolve_booth_role(payload, booth_id)
    await websocket.accept(subprotocol=ws_bearer_subprotocol(websocket))

    from portal.database import get_booth_language_name

    language_name = await get_booth_language_name(booth_id)

    session = Session(
        booth_id=booth_id,
        participant_id=None,
        language=language_name,
        channel_id=f"{booth_id}-audio",
        granted_role=ws_granted_role,
        session_key=booth_session_key(payload),
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

            if not isinstance(data, dict):
                await websocket.send_text(
                    json.dumps({"type": "booth:error", "message": "Message must be a JSON object."})
                )
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
            elif msg_type == "booth:request-publish-ticket":
                await _handle_request_publish_ticket(websocket, session, data)
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
    # The listener page offers its token as a "bearer.<token>" subprotocol; ?token= still works.
    token = ws_credential(websocket)
    is_anonymous = not bool(token)
    if token:
        try:
            payload = decode_token(token)
            if not (payload.get("is_admin") or payload.get("admin")):
                if not listener_scope_matches(payload.get("event_slug", ""), booth_id):
                    await websocket.close(code=4003)
                    return
        except Exception:
            await websocket.close(code=4001)
            return
    else:
        from portal.config import settings

        max_anon = getattr(settings, "max_anonymous_caption_listeners_per_booth", 500)
        if not listener_manager.try_reserve_anonymous(websocket, booth_id, max_anon):
            await websocket.close(code=1008)
            return

    accepted = False
    try:
        await websocket.accept(subprotocol=ws_bearer_subprotocol(websocket))
        accepted = True
    finally:
        if not accepted and is_anonymous:
            listener_manager.cancel_reservation(websocket, booth_id)

    listener_manager.add(websocket, booth_id, is_anonymous=is_anonymous)

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        listener_manager.remove(websocket, booth_id)


@router.websocket("/ws/tts/{booth_id}")
async def ws_tts(websocket: WebSocket, booth_id: str) -> None:
    """WebSocket endpoint for an AI booth's synthesized audio (``{event_slug}-{room_id}-ai-{language_code}``).

    Authorized before accept by ``resolve_ws_auth``, the same check ``/ws/booth/{booth_id}`` uses.
    """
    try:
        await resolve_ws_auth(websocket, booth_id)
    except WSAuthError:
        return
    await websocket.accept(subprotocol=ws_bearer_subprotocol(websocket))
    tts_manager.add(websocket, booth_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        tts_manager.remove(websocket, booth_id)
