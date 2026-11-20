"""Booth WebSocket protocol: one handler per client message type.

``handlers.py`` authenticates and decodes frames, then looks the message type up in
``BOOTH_HANDLERS``. Handlers validate input and call ``BoothRegistry``, which publishes the
resulting state itself (see ``BoothRegistry.subscribe``), so they never broadcast state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

from fastapi import WebSocket

import portal.webhooks.worker as _wh_worker
from portal.auth import can_perform_role
from portal.booth_identity import booth_id_to_mediamtx_path, parse_booth_id
from portal.booth_publish import TICKET_TTL_SECS, issue_ticket
from portal.booth_state import Participant
from portal.config import settings
from portal.globals import booths
from portal.roles import INTERPRETER, can_set_active
from portal.tasks import track_task
from portal.websockets.manager import REASON_SUPERSEDED, WS_CLOSE_SUPERSEDED, Session, manager

logger = logging.getLogger(__name__)


async def handle_join(ws: WebSocket, session: Session, data: dict) -> None:
    """Handle a client joining a booth session.

    Validates client role and language parameters, initializes participant state,
    and publishes the new state through the registry.
    """
    display_name = data.get("display_name", "Interpreter")
    role = data.get("role", INTERPRETER)
    # Check these before they are used: a non-string role breaks the role lookup, and a falsy
    # non-string language (null, 0, false) would be replaced by the booth's language below.
    for name in ("role", "language"):
        if name in data and not isinstance(data[name], str):
            await ws.send_text(json.dumps({"type": "booth:error", "message": f"{name} must be a string."}))
            return
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
    # The booth ID names the room. A malformed one would create a booth with no
    # identity or MediaMTX path, so it is rejected whether or not room_id is sent.
    try:
        _, booth_room_id, _ = parse_booth_id(session.booth_id)
    except ValueError:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Invalid booth_id format."}))
        return
    room_id = data.get("room_id")
    # int() would turn 1.5, true or "1" into 1 (and fail on Infinity), so require a JSON integer.
    if room_id is not None and (isinstance(room_id, bool) or not isinstance(room_id, int)):
        await ws.send_text(json.dumps({"type": "booth:error", "message": "room_id must be an integer."}))
        return
    # A different room would give the booth another room's stream path if this
    # join is the one that creates it (#975).
    if room_id is not None and room_id != booth_room_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "room_id does not match this booth."}))
        return
    async def announce_join(participant: Participant, state: dict) -> None:
        """Bind the participant to this socket and answer it, before the state goes to the booth."""
        for old_ws in manager.detach_participant(session.booth_id, participant.participant_id, keep=ws):
            track_task(
                asyncio.create_task(manager.close_session(old_ws, code=WS_CLOSE_SUPERSEDED, reason=REASON_SUPERSEDED))
            )
        session.participant_id = participant.participant_id
        session.language = language
        session.channel_id = channel_id
        # Broadcast on/off is a live, in-memory state owned exclusively by Mission
        # Control (the Go Live / Stop toggle). Joining or reloading the booth must
        # never change it, so we deliberately do NOT re-apply any persisted value
        # here — doing so would flip an interpreter live (or Mission Control to
        # "Stop") simply because they reconnected.
        await ws.send_text(
            json.dumps({"type": "booth:joined", "participant_id": participant.participant_id, "state": state})
        )

    try:
        participant, _state = await booths.join_participant(
            booth_id=session.booth_id,
            display_name=display_name,
            role=role,
            language=language,
            channel_id=channel_id,
            participant_id=participant_id,
            room_id=room_id,
            # Only this socket's own record may be re-claimed by ID.
            reclaim=participant_id is not None and participant_id == session.participant_id,
            joined_by=session.session_key,
            user_id=session.user_id,
            invite_public_id=str(session.invite_token_id) if session.invite_token_id is not None else None,
            connection_id=session.connection_id,
            before_publish=announce_join,
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return

    if role == INTERPRETER:
        await _wh_worker.enqueue_webhook(
            "booth.interpreter.joined",
            {
                "booth_id": session.booth_id,
                "participant_id": participant.participant_id,
                "display_name": display_name,
                "language": language,
            },
        )


async def handle_leave(_ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        return
    await booths.leave_participant(
        session.booth_id,
        session.participant_id,
        session.language,
        session.channel_id,
        connection_id=session.connection_id,
    )
    session.participant_id = None


def schedule_participant_expiry(booth_id: str, participant_id: str, connection_id: str | None, delay: float) -> None:
    """Remove the participant after *delay* seconds unless they reconnect first."""

    async def expire() -> None:
        await asyncio.sleep(delay)
        await booths.expire_participant(booth_id, participant_id, connection_id)

    track_task(asyncio.create_task(expire(), name=f"booth-expire-{booth_id}"))


async def release_participant(session: Session, *, immediate: bool) -> None:
    """Handle the socket of a joined participant closing (TD-11).

    A dropped connection keeps the participant, and the active slot they hold,
    for ``booth_reconnect_grace_seconds`` so the same session can reclaim them;
    their WHIP audio goes to MediaMTX directly and keeps playing meanwhile.
    *immediate* (access revoked or changed) removes them at once.
    """
    participant_id = session.participant_id
    if not participant_id:
        return
    session.participant_id = None
    grace = settings.booth_reconnect_grace_seconds
    if immediate or grace <= 0:
        await booths.leave_participant(
            session.booth_id, participant_id, session.language, session.channel_id, connection_id=session.connection_id
        )
    else:
        async def start_grace(_state: dict) -> None:
            # Started at commit, so a slow subscriber cannot stretch the grace period.
            schedule_participant_expiry(session.booth_id, participant_id, session.connection_id, grace)

        # Does nothing when another socket holds the participant now.
        await booths.disconnect_participant(
            session.booth_id, participant_id, session.connection_id, before_publish=start_grace
        )


async def handle_chat(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth before sending messages."}))
        return
    body = data.get("body", "")
    try:
        await booths.add_chat_message(
            session.booth_id, session.participant_id, body, session.language, session.channel_id
        )
    except ValueError as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def handle_set_active(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    target_id = data.get("target_id")
    if not isinstance(target_id, str) or not target_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Missing target_id."}))
        return
    try:
        await booths.set_active_interpreter(
            session.booth_id, session.participant_id, target_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def handle_update_state(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        return
    try:
        await booths.update_participant_state(
            session.booth_id,
            session.participant_id,
            session.language,
            session.channel_id,
            mic_active=data.get("mic_active"),
            ingest_connected=data.get("ingest_connected"),
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def handle_request_publish_ticket(ws: WebSocket, session: Session, data: dict) -> None:
    """Issue a single-use WHIP ticket to the active interpreter (#783).

    Always answers with ``booth:publish-ticket`` so the client can match the reply
    to its pending request; a refusal carries ``error`` instead of ``ticket``.
    """
    reply: dict = {"type": "booth:publish-ticket"}
    if not session.participant_id:
        reply["error"] = "Join the booth before going live."
    else:
        try:
            await booths.check_publish_permission(
                session.booth_id, session.participant_id, session.language, session.channel_id
            )
            booth = booths.get_booth_sync(session.booth_id)
            if booth is None or not booth.broadcast_unlocked:
                raise PermissionError("Broadcast is stopped by Mission Control.")
            # Bound to the server's booth, never to the client-supplied channel_id.
            path = booth_id_to_mediamtx_path(session.booth_id)
        except (ValueError, PermissionError) as exc:
            reply["error"] = str(exc)
        else:
            reply["ticket"] = issue_ticket(session.booth_id, session.participant_id, path)
            reply["expires_in"] = int(TICKET_TTL_SECS)
    await ws.send_text(json.dumps(reply))


async def handle_set_broadcast_unlocked(ws: WebSocket, session: Session, data: dict) -> None:
    """Handle setting broadcast lock state for a booth session.

    Requires room coordinator or administrator privileges and a configured
    session language.
    """
    if not can_set_active(session.granted_role):
        await ws.send_text(
            json.dumps({"type": "booth:error", "message": "Only Room Coordinators can manage broadcast lock."})
        )
        return
    if not session.language:
        await ws.send_text(
            json.dumps({"type": "booth:error", "message": "Language must be specified or configured for this booth."})
        )
        return
    unlocked = data.get("unlocked")
    # bool() would turn "false" into True and a missing field into False, so a
    # malformed message could put the booth live or stop it by accident.
    if not isinstance(unlocked, bool):
        await ws.send_text(json.dumps({"type": "booth:error", "message": "unlocked must be a boolean."}))
        return
    async def stop_transcription(_state: dict) -> None:
        # Locking the booth must immediately stop everything tied to it: the
        # transcription worker (which pulls audio independently of any browser)
        # is torn down here so captions stop even if the interpreter's client is
        # unresponsive. It runs before the state is delivered, so a slow socket
        # cannot keep captions flowing. Publisher mic/ingest state is already
        # reset inside set_broadcast_unlocked.
        if unlocked:
            return
        try:
            from portal.transcription.worker import stop_transcription_worker

            await stop_transcription_worker(session.booth_id)
        except Exception as e:
            logger.warning("Failed to stop transcription on lock: %s", e)

    try:
        await booths.set_broadcast_unlocked(
            session.booth_id, unlocked, session.language, session.channel_id, before_publish=stop_transcription
        )
    except Exception as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def handle_initiate_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        await booths.initiate_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def handle_accept_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        await booths.accept_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


async def handle_cancel_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    try:
        await booths.cancel_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))


BoothHandler = Callable[[WebSocket, Session, dict], Awaitable[None]]

BOOTH_HANDLERS: dict[str, BoothHandler] = {
    "booth:join": handle_join,
    "booth:leave": handle_leave,
    "booth:chat": handle_chat,
    "booth:set-active": handle_set_active,
    "booth:update-state": handle_update_state,
    "booth:request-publish-ticket": handle_request_publish_ticket,
    "booth:set-broadcast-unlocked": handle_set_broadcast_unlocked,
    "booth:initiate-handoff": handle_initiate_handoff,
    "booth:accept-handoff": handle_accept_handoff,
    "booth:cancel-handoff": handle_cancel_handoff,
}
