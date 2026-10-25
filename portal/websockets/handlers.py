from __future__ import annotations

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from portal.auth import (
    WSAuthError,
    booth_session_key,
    decode_token,
    is_listener_token,
    resolve_booth_role,
    resolve_ws_auth,
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
    await websocket.accept()

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
    token = websocket.query_params.get("token", "")
    is_anonymous = not bool(token)
    if token:
        try:
            payload = decode_token(token)
            if not (payload.get("is_admin") or payload.get("admin")):
                token_event = payload.get("event_slug", "")
                booth_event: str | None = None
                try:
                    from portal.booth_identity import parse_booth_id

                    booth_event, _, _ = parse_booth_id(booth_id)
                except ValueError:
                    if "/" in booth_id:
                        booth_event = booth_id.split("/")[0]
                    elif "-" in booth_id:
                        booth_event = booth_id.rsplit("-", 1)[0]

                if not token_event or booth_event != token_event:
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
        await websocket.accept()
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


async def _authorize_tts(websocket: WebSocket, room_id: int, language_code: str, booth_id: str) -> None:
    """Authorize a TTS WebSocket subscription before accept.

    The subscription key is (room_id, target language, source booth_id).
    The target language may differ from the source booth language for
    translated audio, so only the room coordinate must match exactly.
    Credentials must still be scoped to the same event and room.
    """
    from urllib.parse import urlparse

    import jwt

    from portal.auth import WSAuthError, decode_token, get_booth_session, resolve_booth_role
    from portal.booth_identity import parse_booth_id, validate_language_code
    from portal.config import settings
    from portal.database import get_event_by_slug, get_session

    try:
        event_slug, actual_room, actual_lang = parse_booth_id(booth_id)
    except ValueError:
        raise WSAuthError("Invalid booth_id format.")

    if room_id != actual_room:
        raise WSAuthError("Mismatched room.")

    try:
        target_lang = validate_language_code(language_code)
    except ValueError:
        raise WSAuthError("Invalid language_code.")

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
        if is_listener_token(payload):
            if not token_event or token_event != event_slug:
                raise WSAuthError("Listener token event_slug does not match.")
            return

        token_lang = payload.get("language_code", "")
        token_room = payload.get("room_id")
        if token_event and token_lang:
            if (
                token_event != event_slug
                or (token_room is not None and str(token_room) != str(actual_room))
                or (token_lang != actual_lang and token_lang != target_lang)
            ):
                raise WSAuthError("Participant token scope does not match.")
            return

        raise WSAuthError("Token scope is required.")

    # For requests without a token that rely on cookies, check Origin strictly
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
            (ws_scheme, websocket.url.netloc),
        }
        if (parsed_origin.scheme, parsed_origin.netloc) not in allowed_origins:
            raise WSAuthError("Origin mismatch.")

    # 2. JWT in cookie
    payload = get_booth_session(websocket)
    if payload:
        if payload.get("is_admin") or payload.get("admin"):
            return

        token_event = payload.get("event_slug", "")
        if is_listener_token(payload):
            if not token_event or token_event != event_slug:
                raise WSAuthError("Listener token event_slug does not match.")
            return

        token_lang = payload.get("language_code", "")
        token_room = payload.get("room_id")
        if token_event and token_lang:
            if (
                token_event != event_slug
                or (token_room is not None and str(token_room) != str(actual_room))
                or (token_lang != actual_lang and token_lang != target_lang)
            ):
                raise WSAuthError("Participant token scope does not match.")
            return

        if payload.get("user"):
            user_payload = {k: v for k, v in payload.items() if k != "role"}
            user_role = await resolve_booth_role(user_payload, booth_id)
            if not user_role and target_lang != actual_lang:
                target_booth_id = f"{event_slug}-{actual_room}-{target_lang}"
                user_role = await resolve_booth_role(user_payload, target_booth_id)
            if user_role:
                return
            if not websocket.cookies.get(f"listener_code_{event_slug}"):
                raise WSAuthError("Token scope is required.")
        else:
            raise WSAuthError("Token scope is required.")

    # 3. Listener code cookie
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
    """TTS WebSocket endpoint with pre-accept authorization."""
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
