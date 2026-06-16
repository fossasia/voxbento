import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from portal.auth import resolve_booth_role, verify_ws_token
from portal.booth_identity import make_booth_id as _make_booth_id
from portal.config import settings
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
)

router = APIRouter()


@router.websocket("/ws/booth/{booth_id}")
async def ws_booth(websocket: WebSocket, booth_id: str) -> None:
    try:
        await verify_ws_token(websocket)
    except ValueError:
        return

    # Derive granted_role from cookies at connect time — never trust client data.
    ws_cookies = websocket.cookies
    ws_session_payload: dict | None = None
    for cookie_name in ("admin_token", "user_token", "session_token"):
        raw = ws_cookies.get(cookie_name, "")
        if not raw:
            continue
        try:
            import jwt as _pyjwt

            ws_session_payload = _pyjwt.decode(
                raw,
                settings.effective_jwt_secret,
                algorithms=["HS256"],
            )
            break
        except _pyjwt.InvalidTokenError:
            continue
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(f"Failed to decode token: {e}")
            continue

    ws_granted_role = await resolve_booth_role(ws_session_payload, booth_id)

    # Validate invite/participant token scope: the token's (event_slug, language_code)
    # must match the booth being connected to.  This prevents a valid token for booth A
    # from being used to join booth B.
    if ws_session_payload is not None and "role" in ws_session_payload:
        token_event = ws_session_payload.get("event_slug", "")
        token_lang = ws_session_payload.get("language_code", "")
        if token_event and token_lang:
            try:
                expected_booth_id = _make_booth_id(token_event, token_lang)
                if expected_booth_id != booth_id:
                    await websocket.close(code=4003)
                    return
            except Exception:
                await websocket.close(code=4003)
                return

    await websocket.accept()
    session = Session(
        booth_id=booth_id,
        participant_id=None,
        language="English",
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
    await websocket.accept()
    listener_manager.add(websocket, booth_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        listener_manager.remove(websocket, booth_id)
