from __future__ import annotations

import json
import logging

from fastapi import WebSocket

from portal.booth_state import BoothRegistry, Participant, ParticipantRole
from portal.globals import booths
from portal.websockets.manager import (
    ConnectionManager,
    ListenerConnectionManager,
    Session,
    listener_manager,
    manager,
)


class BoothCoordinator:
    def __init__(
        self,
        booths: BoothRegistry,
        manager: ConnectionManager,
        listener_manager: ListenerConnectionManager,
    ) -> None:
        self._booths = booths
        self._manager = manager
        self._listener_manager = listener_manager

    async def join_participant(
        self,
        websocket: WebSocket,
        session: Session,
        display_name: str,
        role: ParticipantRole,
        language: str,
        channel_id: str,
        participant_id: str | None,
        room_id: int | None,
    ) -> Participant:
        participant, state = await self._booths.join_participant(
            booth_id=session.booth_id,
            display_name=display_name,
            role=role,
            language=language,
            channel_id=channel_id,
            participant_id=participant_id,
            room_id=room_id,
        )
        session.participant_id = participant.participant_id
        session.language = language
        session.channel_id = channel_id

        try:
            await websocket.send_text(
                json.dumps({"type": "booth:joined", "participant_id": participant.participant_id, "state": state})
            )
        finally:
            await self._broadcast_state(session.booth_id, state)

        return participant

    async def leave_participant(self, session: Session) -> None:
        if not session.participant_id:
            return
        state = await self._booths.leave_participant(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
        session.participant_id = None
        await self._broadcast_state(session.booth_id, state)

    async def add_chat_message(self, session: Session, body: str) -> None:
        if not session.participant_id:
            raise ValueError("Participant has not joined the booth.")
        message, state = await self._booths.add_chat_message(
            session.booth_id, session.participant_id, body, session.language, session.channel_id
        )
        try:
            await self._manager.broadcast(session.booth_id, {"type": "booth:chat", "message": message})
        finally:
            await self._broadcast_state(session.booth_id, state)

    async def set_active_interpreter(self, session: Session, target_id: str) -> None:
        if not session.participant_id:
            raise ValueError("Participant has not joined the booth.")
        state = await self._booths.set_active_interpreter(
            session.booth_id, session.participant_id, target_id, session.language, session.channel_id
        )
        await self._broadcast_state(session.booth_id, state)

    async def update_participant_state(
        self,
        session: Session,
        *,
        mic_active: bool | None,
        ingest_connected: bool | None,
        connected: bool | None,
    ) -> None:
        if not session.participant_id:
            raise ValueError("Participant has not joined the booth.")
        state = await self._booths.update_participant_state(
            session.booth_id,
            session.participant_id,
            session.language,
            session.channel_id,
            mic_active=mic_active,
            ingest_connected=ingest_connected,
            connected=connected,
        )
        await self._broadcast_state(session.booth_id, state)

    async def set_broadcast_unlocked(self, session: Session, unlocked: bool) -> None:
        state = await self._booths.set_broadcast_unlocked(
            session.booth_id, unlocked, session.language, session.channel_id
        )
        if not unlocked:
            try:
                from portal.transcription.worker import stop_transcription_worker

                await stop_transcription_worker(session.booth_id)
            except Exception as exc:
                logging.getLogger(__name__).warning("Failed to stop transcription on lock: %s", exc)
        try:
            await self._broadcast_state(session.booth_id, state)
        finally:
            await self._listener_manager.broadcast(session.booth_id, {"type": "booth:state", "state": state})

    async def initiate_handoff(self, session: Session) -> None:
        if not session.participant_id:
            raise ValueError("Participant has not joined the booth.")
        state = await self._booths.initiate_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
        await self._broadcast_state(session.booth_id, state)

    async def accept_handoff(self, session: Session) -> None:
        if not session.participant_id:
            raise ValueError("Participant has not joined the booth.")
        state = await self._booths.accept_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
        await self._broadcast_state(session.booth_id, state)

    async def cancel_handoff(self, session: Session) -> None:
        if not session.participant_id:
            raise ValueError("Participant has not joined the booth.")
        state = await self._booths.cancel_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
        await self._broadcast_state(session.booth_id, state)

    async def sync_state(self, websocket: WebSocket, booth_id: str) -> None:
        state = await self._booths.get_booth(booth_id)
        if state is not None:
            await websocket.send_text(json.dumps({"type": "booth:state", "state": state}))

    async def broadcast_transcription(self, booth_id: str, payload: str | dict) -> None:
        if isinstance(payload, str):
            if payload.startswith("[Server overloaded") or payload.startswith("[Transcription provider failed"):
                try:
                    await self._manager.broadcast(booth_id, {"type": "booth:error", "message": payload})
                finally:
                    state = await self._booths.mark_ingest_overloaded(booth_id)
                    if state is not None:
                        await self._broadcast_state(booth_id, state)
            else:
                await self._listener_manager.broadcast(
                    booth_id, {"type": "caption", "status": "final", "text": payload}
                )
        else:
            await self._listener_manager.broadcast(booth_id, payload)

    async def _broadcast_state(self, booth_id: str, state: dict) -> None:
        message = {"type": "booth:state", "state": state}
        await self._manager.broadcast(booth_id, message)


booth_coordinator = BoothCoordinator(booths, manager, listener_manager)
