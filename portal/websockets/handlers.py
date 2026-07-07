from __future__ import annotations

import json

from sqlalchemy import select

from portal.auth import can_perform_role
from portal.booth_identity import parse_booth_id
from portal.database import get_session as get_db_session
from portal.globals import booths
from portal.models import DBBooth, Event, Room
from portal.websockets.manager import listener_manager, manager


async def _handle_join(ws, session, data):
    display_name = data.get("display_name", "Interpreter")
    role = data.get("role", "interpreter")
    language = data.get("language", "English")
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
        participant, state = await booths.join_participant(
            booth_id=session.booth_id,
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
    session.participant_id = participant.participant_id
    session.language = language
    session.channel_id = channel_id
    if not state.get("broadcast_unlocked"):
        try:
            _slug, _lang = parse_booth_id(session.booth_id)
            async with get_db_session() as _db:
                _db_booth = await _db.scalar(
                    select(DBBooth).join(Event).where(Event.slug == _slug, DBBooth.language_code == _lang)
                )
                if _db_booth and _db_booth.broadcast_unlocked:
                    state = await booths.set_broadcast_unlocked(session.booth_id, True, language, channel_id)
        except Exception as e:
            import logging

            logging.getLogger(__name__).warning(f"Failed to set broadcast unlocked: {e}")
    await ws.send_text(
        json.dumps({"type": "booth:joined", "participant_id": participant.participant_id, "state": state})
    )
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


async def _handle_leave(session):
    if not session.participant_id:
        return
    state = await booths.leave_participant(
        session.booth_id, session.participant_id, session.language, session.channel_id
    )
    session.participant_id = None
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


async def _handle_chat(ws, session, data):
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth before sending messages."}))
        return
    body = data.get("body", "")
    try:
        message, state = await booths.add_chat_message(
            session.booth_id, session.participant_id, body, session.language, session.channel_id
        )
    except ValueError as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
    await manager.broadcast(session.booth_id, {"type": "booth:chat", "message": message})
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


async def _handle_set_active(ws, session, data):
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    target_id = data.get("target_id")
    if not target_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Missing target_id."}))
        return
    snap = await booths.snapshot(session.booth_id, session.language, session.channel_id)
    previous_active = snap.get("active_interpreter_id")
    try:
        state = await booths.set_active_interpreter(
            session.booth_id, session.participant_id, target_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
    if previous_active and previous_active != target_id:
        pass
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


async def _handle_update_state(ws, session, data):
    if not session.participant_id:
        return
    try:
        state = await booths.update_participant_state(
            session.booth_id,
            session.participant_id,
            session.language,
            session.channel_id,
            mic_active=data.get("mic_active"),
            ingest_connected=data.get("ingest_connected"),
            connected=data.get("connected"),
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


async def _handle_set_broadcast_unlocked(ws, session, data):
    if session.granted_role not in ("room_coordinator", "event_owner", "super_admin"):
        await ws.send_text(
            json.dumps({"type": "booth:error", "message": "Only Room Coordinators can manage broadcast lock."})
        )
        return
    unlocked = bool(data.get("unlocked"))
    try:
        event_slug, language_code = parse_booth_id(session.booth_id)
        async with get_db_session() as db:
            stmt = (
                select(DBBooth)
                .join(Room)
                .join(Event)
                .where(Event.slug == event_slug, DBBooth.language_code == language_code)
            )
            result = await db.execute(stmt)
            db_booth = result.scalar_one_or_none()
            if db_booth:
                db_booth.broadcast_unlocked = unlocked
                await db.commit()
    except Exception as e:
        import logging

        logging.getLogger(__name__).error(f"Error persisting broadcast lock: {e}")
    try:
        state = await booths.set_broadcast_unlocked(session.booth_id, unlocked, session.language, session.channel_id)
        await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})
        await listener_manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})
    except Exception as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def _handle_initiate_handoff(ws, session, _data):
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        state = await booths.initiate_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


async def _handle_accept_handoff(ws, session, _data):
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        state = await booths.accept_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


async def _handle_cancel_handoff(ws, session, _data):
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        state = await booths.cancel_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})
