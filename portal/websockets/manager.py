from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import WebSocket

from portal.globals import booths


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


