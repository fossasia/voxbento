from __future__ import annotations

import json

from fastapi import WebSocket

import portal.webhooks.worker as _wh_worker
from portal.auth import can_perform_role
from portal.booth_coordinator import booth_coordinator
from portal.globals import booths
from portal.websockets.manager import Session


async def _handle_join(ws: WebSocket, session: Session, data: dict) -> None:
    """Handle a client joining a booth session.

    Validates client role and language parameters, then delegates the join operation.
    """
    display_name = data.get("display_name", "Interpreter")
    role = data.get("role", "interpreter")
    language = data.get("language") or session.language
    if not language:
        await ws.send_text(
            json.dumps({"type": "booth:error", "message": "Language must be specified or configured for this booth."})
        )
        return
    channel_id = data.get("channel_id", f"{session.booth_id}-audio")
    participant_id = data.get("participant_id")
    if session.granted_role is None:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "No role assigned for this session."}))
        return
    if not can_perform_role(session.granted_role, role):
        await ws.send_text(
            json.dumps(
                {
                    "type": "booth:error",
                    "message": f"Your assigned role ({session.granted_role}) does not permit joining as {role}.",
                }
            )
        )
        return
    client_event = data.get("event_slug")
    if client_event is not None:
        try:
            await booths.validate_booth_event(session.booth_id, client_event)
        except PermissionError as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
            return
    room_id = data.get("room_id")
    if room_id is not None:
        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            await ws.send_text(json.dumps({"type": "booth:error", "message": "room_id must be an integer."}))
            return
    try:
        participant = await booth_coordinator.join_participant(
            ws,
            session,
            display_name=display_name,
            role=role,
            language=language,
            channel_id=channel_id,
            participant_id=participant_id,
            room_id=room_id,
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
    if role == "interpreter":
        await _wh_worker.enqueue_webhook(
            "booth.interpreter.joined",
            {
                "booth_id": session.booth_id,
                "participant_id": participant.participant_id,
                "display_name": display_name,
                "language": language,
            },
        )


async def _handle_leave(session: Session) -> None:
    await booth_coordinator.leave_participant(session)


async def _handle_chat(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth before sending messages."}))
        return
    body = data.get("body", "")
    try:
        await booth_coordinator.add_chat_message(session, body)
    except ValueError as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return


async def _handle_set_active(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    target_id = data.get("target_id")
    if not target_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Missing target_id."}))
        return
    try:
        await booth_coordinator.set_active_interpreter(session, target_id)
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return


async def _handle_update_state(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        return
    try:
        await booth_coordinator.update_participant_state(
            session,
            mic_active=data.get("mic_active"),
            ingest_connected=data.get("ingest_connected"),
            connected=data.get("connected"),
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return


async def _handle_set_broadcast_unlocked(ws: WebSocket, session: Session, data: dict) -> None:
    """Handle setting broadcast lock state for a booth session.

    Requires room coordinator or administrator privileges and a configured
    session language.
    """
    if session.granted_role not in ("room_coordinator", "event_owner", "super_admin"):
        await ws.send_text(
            json.dumps({"type": "booth:error", "message": "Only Room Coordinators can manage broadcast lock."})
        )
        return
    if not session.language:
        await ws.send_text(
            json.dumps({"type": "booth:error", "message": "Language must be specified or configured for this booth."})
        )
        return
    unlocked = bool(data.get("unlocked"))
    try:
        await booth_coordinator.set_broadcast_unlocked(session, unlocked)
    except Exception as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def _handle_initiate_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        await booth_coordinator.initiate_handoff(session)
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return


async def _handle_accept_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        await booth_coordinator.accept_handoff(session)
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return


async def _handle_cancel_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        await booth_coordinator.cancel_handoff(session)
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
