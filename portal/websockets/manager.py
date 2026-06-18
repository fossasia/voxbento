import json
import logging
from dataclasses import dataclass

from fastapi import WebSocket
from sqlalchemy import select

from portal.auth import can_perform_role
from portal.booth_identity import parse_booth_id
from portal.database import get_session as get_db_session
from portal.globals import booths
from portal.models import DBBooth, Event, Room


@dataclass
class Session:
    booth_id: str
    participant_id: str | None
    language: str
    channel_id: str
    # Role derived from the bearer cookie at WS connection time — never from client data.
    granted_role: str | None = None


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}
        # Keyed by id(ws) to avoid __hash__ issues with WebSocket objects
        self._sessions: dict[int, Session] = {}

    def add(self, ws: WebSocket, session: Session) -> None:
        self._rooms.setdefault(session.booth_id, set()).add(ws)
        self._sessions[id(ws)] = session

    def remove(self, ws: WebSocket) -> Session | None:
        session = self._sessions.pop(id(ws), None)
        if session:
            room = self._rooms.get(session.booth_id, set())
            room.discard(ws)
            if not room:
                self._rooms.pop(session.booth_id, None)
        return session

    def get_session(self, ws: WebSocket) -> Session | None:
        return self._sessions.get(id(ws))

    async def broadcast(self, booth_id: str, message: dict) -> None:
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in list(self._rooms.get(booth_id, set())):
            try:
                await ws.send_text(payload)
            except (RuntimeError, OSError):
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


class ListenerConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}

    def add(self, ws: WebSocket, booth_id: str) -> None:
        self._rooms.setdefault(booth_id, set()).add(ws)

    def remove(self, ws: WebSocket, booth_id: str) -> None:
        room = self._rooms.get(booth_id, set())
        room.discard(ws)
        if not room:
            self._rooms.pop(booth_id, None)

    async def broadcast(self, booth_id: str, message: dict) -> None:
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in list(self._rooms.get(booth_id, set())):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws, booth_id)


class TTSConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}

    def _get_key(self, room_id: int, language_code: str) -> str:
        return f"{room_id}-{language_code}"

    def add(self, ws: WebSocket, room_id: int, language_code: str) -> None:
        key = self._get_key(room_id, language_code)
        self._rooms.setdefault(key, set()).add(ws)

    def remove(self, ws: WebSocket, room_id: int, language_code: str) -> None:
        key = self._get_key(room_id, language_code)
        room = self._rooms.get(key, set())
        room.discard(ws)
        if not room:
            self._rooms.pop(key, None)

    async def broadcast_audio(self, room_id: int, language_code: str, audio_bytes: bytes) -> None:
        key = self._get_key(room_id, language_code)
        dead: list[WebSocket] = []
        for ws in list(self._rooms.get(key, set())):
            try:
                await ws.send_bytes(audio_bytes)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws, room_id, language_code)


manager = ConnectionManager()
listener_manager = ListenerConnectionManager()
tts_manager = TTSConnectionManager()


async def broadcast_transcription(booth_id: str, payload: str | dict):
    if isinstance(payload, str):
        # Legacy support and error handling
        text = payload
        if text.startswith("[Server overloaded") or text.startswith("[Transcription provider failed"):
            await manager.broadcast(booth_id, {"type": "booth:error", "message": text})
            booth = booths.get_booth_sync(booth_id)
            if booth:
                booth.ingest_status = "overloaded"
                await manager.broadcast(booth_id, {"type": "booth:state", "state": booth.as_public_dict()})
        else:
            msg = {"type": "caption", "status": "final", "text": text}
            await listener_manager.broadcast(booth_id, msg)
    else:
        # Aggregator payload
        await listener_manager.broadcast(booth_id, payload)


async def _handle_join(ws: WebSocket, session: Session, data: dict) -> None:
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


async def _handle_leave(session: Session) -> None:
    if not session.participant_id:
        return
    state = await booths.leave_participant(
        session.booth_id, session.participant_id, session.language, session.channel_id
    )
    session.participant_id = None
    await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})


async def _handle_chat(ws: WebSocket, session: Session, data: dict) -> None:
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


async def _handle_set_active(ws: WebSocket, session: Session, data: dict) -> None:
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


async def _handle_update_state(ws: WebSocket, session: Session, data: dict) -> None:
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


async def _handle_set_broadcast_unlocked(ws: WebSocket, session: Session, data: dict) -> None:
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
        logging.getLogger(__name__).error(f"Error persisting broadcast lock: {e}")
    try:
        state = await booths.set_broadcast_unlocked(session.booth_id, unlocked, session.language, session.channel_id)
        await manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})
        await listener_manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})
    except Exception as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def _handle_initiate_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
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


async def _handle_accept_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
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


async def _handle_cancel_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
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
