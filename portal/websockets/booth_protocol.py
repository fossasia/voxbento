from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi import WebSocket

from portal.auth import can_perform_role

if TYPE_CHECKING:
    from portal.booth_state import BoothRegistry, Participant
    from portal.websockets.manager import Session

_log = logging.getLogger(__name__)


class BoothProtocolCoordinator:
    """Coordinates incoming WebSocket protocol frames and booth state transitions.

    Decouples transport (handlers.py) and connection tracking (manager.py)
    from booth session domain logic (booth_state.py).
    """

    def __init__(self, registry: BoothRegistry | None = None) -> None:
        self._registry = registry

    @property
    def booths(self) -> BoothRegistry:
        if self._registry is not None:
            return self._registry
        from portal.globals import booths

        return booths

    async def dispatch(self, ws: WebSocket, session: Session, data: dict) -> None:
        """Route incoming decoded message dictionary to the appropriate protocol handler."""
        msg_type = data.get("type", "")
        if msg_type == "booth:join":
            await self.handle_join(ws, session, data)
        elif msg_type == "booth:leave":
            await self.handle_leave(session)
        elif msg_type == "booth:chat":
            await self.handle_chat(ws, session, data)
        elif msg_type == "booth:set-active":
            await self.handle_set_active(ws, session, data)
        elif msg_type == "booth:update-state":
            await self.handle_update_state(ws, session, data)
        elif msg_type == "booth:set-broadcast-unlocked":
            await self.handle_set_broadcast_unlocked(ws, session, data)
        elif msg_type == "booth:initiate-handoff":
            await self.handle_initiate_handoff(ws, session, data)
        elif msg_type == "booth:accept-handoff":
            await self.handle_accept_handoff(ws, session, data)
        elif msg_type == "booth:cancel-handoff":
            await self.handle_cancel_handoff(ws, session, data)
        else:
            await ws.send_text(json.dumps({"type": "booth:error", "message": f"Unknown message type: {msg_type}"}))

    async def handle_disconnect(self, session: Session) -> None:
        """Clean up participant state on disconnect and publish updated state."""
        if session.participant_id:
            await self.booths.leave_participant(
                session.booth_id,
                session.participant_id,
                session.language or "",
                session.channel_id,
            )
            session.participant_id = None

    async def handle_join(self, ws: WebSocket, session: Session, data: dict) -> None:
        """Handle a client joining a booth session.

        Validates client role and language parameters, initializes participant state,
        sends booth:joined to the joining socket, and triggers coordinated broadcast
        of the new booth state.
        """
        display_name = data.get("display_name", "Interpreter")
        role = data.get("role", "interpreter")
        language = data.get("language") or session.language
        if not language:
            await ws.send_text(
                json.dumps(
                    {"type": "booth:error", "message": "Language must be specified or configured for this booth."}
                )
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
                await self.booths.validate_booth_event(session.booth_id, client_event)
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

        async def _send_joined(participant: Participant, state: dict) -> None:
            await ws.send_text(
                json.dumps({"type": "booth:joined", "participant_id": participant.participant_id, "state": state})
            )

        try:
            participant, state = await self.booths.join_participant(
                booth_id=session.booth_id,
                display_name=display_name,
                role=role,
                language=language,
                channel_id=channel_id,
                participant_id=participant_id,
                room_id=room_id,
                before_broadcast=_send_joined,
            )
        except (ValueError, PermissionError) as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
            return

        session.participant_id = participant.participant_id
        session.language = language
        session.channel_id = channel_id

        if role == "interpreter":
            import portal.webhooks.worker as _wh_worker

            await _wh_worker.enqueue_webhook(
                "booth.interpreter.joined",
                {
                    "booth_id": session.booth_id,
                    "participant_id": participant.participant_id,
                    "display_name": display_name,
                    "language": language,
                },
            )

    async def handle_leave(self, session: Session) -> None:
        """Handle a client voluntarily leaving the booth."""
        if not session.participant_id:
            return
        await self.booths.leave_participant(
            session.booth_id,
            session.participant_id,
            session.language or "",
            session.channel_id,
        )
        session.participant_id = None

    async def handle_chat(self, ws: WebSocket, session: Session, data: dict) -> None:
        """Handle sending a chat message to the booth."""
        if not session.participant_id:
            await ws.send_text(
                json.dumps({"type": "booth:error", "message": "Join the booth before sending messages."})
            )
            return
        body = data.get("body", "")
        try:
            await self.booths.add_chat_message(
                session.booth_id,
                session.participant_id,
                body,
                session.language or "",
                session.channel_id,
            )
        except ValueError as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
            return

    async def handle_set_active(self, ws: WebSocket, session: Session, data: dict) -> None:
        """Handle designating the active interpreter."""
        if not session.participant_id:
            await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
            return
        target_id = data.get("target_id")
        if not target_id:
            await ws.send_text(json.dumps({"type": "booth:error", "message": "Missing target_id."}))
            return
        try:
            await self.booths.set_active_interpreter(
                session.booth_id,
                session.participant_id,
                target_id,
                session.language or "",
                session.channel_id,
            )
        except (ValueError, PermissionError) as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
            return

    async def handle_update_state(self, ws: WebSocket, session: Session, data: dict) -> None:
        """Handle participant flag updates (mic_active, ingest_connected, connected)."""
        if not session.participant_id:
            return
        try:
            await self.booths.update_participant_state(
                session.booth_id,
                session.participant_id,
                session.language or "",
                session.channel_id,
                mic_active=data.get("mic_active"),
                ingest_connected=data.get("ingest_connected"),
                connected=data.get("connected"),
            )
        except (ValueError, PermissionError) as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
            return

    async def handle_set_broadcast_unlocked(self, ws: WebSocket, session: Session, data: dict) -> None:
        """Handle managing the booth broadcast lock (Room Coordinators and admins only)."""
        if session.granted_role not in ("room_coordinator", "event_owner", "super_admin"):
            await ws.send_text(
                json.dumps({"type": "booth:error", "message": "Only Room Coordinators can manage broadcast lock."})
            )
            return
        if not session.language:
            await ws.send_text(
                json.dumps(
                    {"type": "booth:error", "message": "Language must be specified or configured for this booth."}
                )
            )
            return
        unlocked = bool(data.get("unlocked"))
        try:
            await self.booths.set_broadcast_unlocked(session.booth_id, unlocked, session.language, session.channel_id)
        except Exception as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))

    async def handle_initiate_handoff(self, ws: WebSocket, session: Session, _data: dict) -> None:
        """Handle initiating a mic handoff negotiation."""
        if not session.participant_id:
            await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
            return
        try:
            await self.booths.initiate_handoff(
                session.booth_id,
                session.participant_id,
                session.language or "",
                session.channel_id,
            )
        except (ValueError, PermissionError) as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
            return

    async def handle_accept_handoff(self, ws: WebSocket, session: Session, _data: dict) -> None:
        """Handle accepting a pending mic handoff."""
        if not session.participant_id:
            await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
            return
        try:
            await self.booths.accept_handoff(
                session.booth_id,
                session.participant_id,
                session.language or "",
                session.channel_id,
            )
        except (ValueError, PermissionError) as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
            return

    async def handle_cancel_handoff(self, ws: WebSocket, session: Session, _data: dict) -> None:
        """Handle cancelling an in-progress mic handoff."""
        if not session.participant_id:
            await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
            return
        try:
            await self.booths.cancel_handoff(
                session.booth_id,
                session.participant_id,
                session.language or "",
                session.channel_id,
            )
        except (ValueError, PermissionError) as exc:
            await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
            return


booth_protocol = BoothProtocolCoordinator()
