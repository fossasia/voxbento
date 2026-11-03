from __future__ import annotations

import json
import struct
from dataclasses import dataclass

from fastapi import WebSocket, WebSocketDisconnect


@dataclass
class Session:
    booth_id: str
    participant_id: str | None
    language: str | None
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
            except (WebSocketDisconnect, RuntimeError, OSError):
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


class ListenerConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}

    def add(self, ws: WebSocket, booth_id: str) -> None:
        self._rooms.setdefault(booth_id, set()).add(ws)

    def has_listeners(self, booth_id: str) -> bool:
        return bool(self._rooms.get(booth_id, set()))

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

    def _get_key(self, room_id: int, language_code: str, booth_id: str) -> str:
        return f"{room_id}-{language_code}-{booth_id}"

    def add(self, ws: WebSocket, room_id: int, language_code: str, booth_id: str) -> None:
        key = self._get_key(room_id, language_code, booth_id)
        self._rooms.setdefault(key, set()).add(ws)

    def remove(self, ws: WebSocket, room_id: int, language_code: str, booth_id: str) -> None:
        key = self._get_key(room_id, language_code, booth_id)
        room = self._rooms.get(key, set())
        room.discard(ws)
        if not room:
            self._rooms.pop(key, None)

    def has_listeners(self, room_id: int, language_code: str, booth_id: str) -> bool:
        key = self._get_key(room_id, language_code, booth_id)
        return bool(self._rooms.get(key, set()))

    async def broadcast_bundle(
        self,
        room_id: int,
        language_code: str,
        booth_id: str,
        audio_bytes: bytes,
        segment_id: str,
        seq: int,
        caption: str = "",
        translation: str = "",
        error: str | None = None,
        audio_pending: bool | None = None,
    ) -> None:
        key = self._get_key(room_id, language_code, booth_id)
        from portal.transcription.timing import timing_metadata

        header = {
            "segment_id": segment_id,
            "seq": seq,
            "caption": caption,
            "translation": translation,
            "error": error,
            "audio_pending": (not audio_bytes and error is None) if audio_pending is None else audio_pending,
            **timing_metadata(),
        }
        header_bytes = json.dumps(header).encode("utf-8")
        # Frame: [1-byte version][4-byte length L][L-bytes header][audio_bytes]
        frame = struct.pack(">BI", 1, len(header_bytes)) + header_bytes + audio_bytes

        dead: list[WebSocket] = []
        for ws in list(self._rooms.get(key, set())):
            try:
                await ws.send_bytes(frame)
            except Exception:
                dead.append(ws)
        if dead:
            for ws in dead:
                self.remove(ws, room_id, language_code, booth_id)


manager = ConnectionManager()
listener_manager = ListenerConnectionManager()
tts_manager = TTSConnectionManager()
