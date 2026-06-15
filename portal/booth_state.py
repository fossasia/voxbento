from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from portal.booth_identity import (
    BoothInstance,
    make_booth_id,
    make_mediamtx_path,
    parse_booth_id,
    validate_event_slug,
    validate_instance,
    validate_language_code,
)

ParticipantRole = Literal[
    'super_admin',
    'event_owner',
    'room_coordinator',
    'interpreter',
]


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


@dataclass
class Participant:
    participant_id: str
    display_name: str
    role: ParticipantRole
    language: str
    channel_id: str
    mic_active: bool = False
    ingest_connected: bool = False
    connected: bool = True
    joined_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)


@dataclass
class ChatMessage:
    message_id: str
    sender_id: str
    sender_name: str
    body: str
    sent_at: str = field(default_factory=utc_now_iso)


@dataclass
class Booth:
    booth_id: str
    event_slug: str
    language_code: str
    language: str
    channel_id: str
    instance: BoothInstance = 'primary'
    mediamtx_path: str = ''
    room_id: int | None = None
    active_interpreter_id: str | None = None
    handoff_state: str = 'idle'
    handoff_initiator_id: str | None = None
    broadcast_unlocked: bool = False
    participants: dict[str, Participant] = field(default_factory=dict)
    chat_messages: list[ChatMessage] = field(default_factory=list)
    ingest_status: str = 'disconnected'

    def __post_init__(self) -> None:
        if self.event_slug and self.language_code:
            if not self.mediamtx_path:
                self.mediamtx_path = make_mediamtx_path(self.event_slug, self.language_code)
            if not self.channel_id:
                self.channel_id = self.mediamtx_path

    def as_public_dict(self) -> dict:
        return {
            'booth_id': self.booth_id,
            'event_slug': self.event_slug,
            'language_code': self.language_code,
            'instance': self.instance,
            'mediamtx_path': self.mediamtx_path,
            'room_id': self.room_id,
            'language': self.language,
            'channel_id': self.channel_id,
            'active_interpreter_id': self.active_interpreter_id,
            'handoff_state': self.handoff_state,
            'handoff_initiator_id': self.handoff_initiator_id,
            'broadcast_unlocked': self.broadcast_unlocked,
            'ingest_status': self.ingest_status,
            'participants': [asdict(p) for p in self.participants.values()],
            'chat_messages': [asdict(m) for m in self.chat_messages[-100:]],
        }


def _pick_next_interpreter(booth: Booth) -> str | None:
    """Return the next active-interpreter-eligible participant ID, or None.

    Any role with BOOTH_GO_LIVE permission qualifies: interpreter,
    room_coordinator, event_owner, super_admin.  Mission control no longer
    calls booth:join (silent observer mode), so these roles will only be
    present when the participant is actually in the interpreter booth.
    """
    for p in booth.participants.values():
        if p.role in ('interpreter', 'room_coordinator', 'event_owner', 'super_admin'):
            return p.participant_id
    return None


class BoothRegistry:
    def __init__(self) -> None:
        self._booths: dict[str, Booth] = {}
        self._lock = asyncio.Lock()

    def _get_or_create_booth(
        self,
        booth_id: str,
        language: str,
        channel_id: str,
        room_id: int | None = None,
    ) -> Booth:
        """Return existing booth or create one. Caller must hold self._lock.

        ``language``, ``channel_id``, and ``room_id`` are only used when
        *creating* the booth; they are treated as immutable once set so that
        a later request from a different client cannot silently change the
        booth's canonical values.

        The ``booth_id`` is parsed into ``event_slug`` and ``language_code``
        using :func:`parse_booth_id`.  If parsing fails (legacy free-form ID),
        the booth is still created with empty identity fields so existing
        callers continue to work during the migration window.
        """
        booth = self._booths.get(booth_id)
        if booth is None:
            try:
                event_slug, language_code = parse_booth_id(booth_id)
            except ValueError:
                event_slug = ''
                language_code = ''
            booth = Booth(
                booth_id=booth_id,
                event_slug=event_slug,
                language_code=language_code,
                language=language,
                channel_id=channel_id,
                room_id=room_id,
            )
            self._booths[booth_id] = booth
        return booth

    async def create_booth(
        self,
        event_slug: str,
        language_code: str,
        language: str,
        channel_id: str = '',
        instance: BoothInstance = 'primary',
        room_id: int | None = None,
    ) -> dict:
        """Create a booth using validated identity coordinates.

        This is the preferred entry point for new code.  The booth ID,
        MediaMTX path, and default ``channel_id`` are derived automatically
        from the coordinates.  When no explicit ``channel_id`` is given it
        defaults to the MediaMTX path (``{event_slug}/{language_code}``).

        ``room_id`` is an optional foreign key to an Eventyay Room.  It is
        nullable and has no effect on booth identity — it exists to support
        future Eventyay integration.

        Raises ``ValueError`` if the slug or language code is invalid, or if
        a booth with the same ID already exists.
        """
        slug = validate_event_slug(event_slug)
        code = validate_language_code(language_code)
        inst = validate_instance(instance)
        booth_id = make_booth_id(slug, code)
        mtx_path = make_mediamtx_path(slug, code)

        async with self._lock:
            if booth_id in self._booths:
                raise ValueError(f"Booth '{booth_id}' already exists.")
            booth = Booth(
                booth_id=booth_id,
                event_slug=slug,
                language_code=code,
                language=language,
                channel_id=channel_id or mtx_path,
                instance=inst,
                room_id=room_id,
            )
            self._booths[booth_id] = booth
            return booth.as_public_dict()

    async def snapshot(
        self,
        booth_id: str,
        language: str,
        channel_id: str,
        room_id: int | None = None,
    ) -> dict:
        async with self._lock:
            return self._get_or_create_booth(booth_id, language, channel_id, room_id=room_id).as_public_dict()

    async def join_participant(
        self,
        booth_id: str,
        display_name: str,
        role: ParticipantRole,
        language: str,
        channel_id: str,
        participant_id: str | None = None,
        room_id: int | None = None,
    ) -> tuple[Participant, dict]:
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id, room_id=room_id)
            participant = Participant(
                participant_id=participant_id or uuid4().hex,
                display_name=display_name.strip() or 'Interpreter',
                role=role,
                language=language.strip() or booth.language,
                channel_id=channel_id.strip() or booth.channel_id,
            )
            booth.participants[participant.participant_id] = participant
            # Auto-assign active slot to any role that can go live.
            # Mission control now uses silent-observer mode (no booth:join),
            # so only real interpreter-booth participants appear here.
            if participant.role in ('interpreter', 'room_coordinator', 'event_owner', 'super_admin') and booth.active_interpreter_id is None:
                booth.active_interpreter_id = participant.participant_id
            return participant, booth.as_public_dict()

    async def leave_participant(self, booth_id: str, participant_id: str, language: str, channel_id: str) -> dict:
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            participant = booth.participants.pop(participant_id, None)
            if participant is None:
                return booth.as_public_dict()
            if booth.active_interpreter_id == participant_id:
                booth.active_interpreter_id = _pick_next_interpreter(booth)
                booth.handoff_state = 'pending' if booth.active_interpreter_id else 'idle'
            if not booth.participants:
                booth.ingest_status = 'disconnected'
                booth.handoff_state = 'idle'
            return booth.as_public_dict()

    async def set_broadcast_unlocked(
        self,
        booth_id: str,
        unlocked: bool,
        language: str,
        channel_id: str,
    ) -> dict:
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            booth.broadcast_unlocked = unlocked
            return booth.as_public_dict()

    async def set_active_interpreter(
        self,
        booth_id: str,
        requester_id: str,
        target_id: str,
        language: str,
        channel_id: str,
    ) -> dict:
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            requester = booth.participants.get(requester_id)
            target = booth.participants.get(target_id)
            if requester is None or target is None:
                raise ValueError('Requester or target participant does not exist in this booth.')
            if target.role not in ('interpreter', 'room_coordinator', 'event_owner', 'super_admin'):
                raise ValueError('Only interpreters or admins can be set active.')
            requester_is_admin = requester.role in ('room_coordinator', 'event_owner', 'super_admin')
            requester_is_active = booth.active_interpreter_id == requester_id
            requester_is_target = requester_id == target_id
            if not requester_is_admin and not requester_is_active and not requester_is_target:
                raise PermissionError('Only coordinators/admins or the active interpreter can reassign another interpreter.')
            booth.active_interpreter_id = target_id
            booth.handoff_state = 'completed'
            for p in booth.participants.values():
                p.ingest_connected = p.participant_id == target_id and p.ingest_connected
                p.mic_active = p.participant_id == target_id and p.mic_active
                p.updated_at = utc_now_iso()
            booth.ingest_status = (
                'connected' if any(p.ingest_connected for p in booth.participants.values()) else 'disconnected'
            )
            return booth.as_public_dict()

    async def initiate_handoff(
        self,
        booth_id: str,
        requester_id: str,
        language: str,
        channel_id: str,
    ) -> dict:
        """Begin a mic handoff negotiation.

        If the requester is the active interpreter the state becomes
        ``'offered'`` (Active offers the mic).  If the requester is a
        passive interpreter the state becomes ``'requested'`` (Passive
        asks for the mic).  A handoff can only be initiated from the
        ``'idle'`` state.
        """
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            requester = booth.participants.get(requester_id)
            if requester is None:
                raise ValueError('Requester is not in this booth.')
            if requester.role not in ('interpreter', 'room_coordinator', 'event_owner', 'super_admin'):
                raise PermissionError('Only interpreters and coordinators can initiate handoffs.')
            if booth.handoff_state != 'idle':
                raise ValueError('A handoff is already in progress.')
            # Need at least one other participant who can take over
            other_interpreters = [
                p for p in booth.participants.values()
                if p.role in ('interpreter', 'room_coordinator', 'event_owner', 'super_admin')
                and p.participant_id != requester_id
            ]
            if not other_interpreters:
                raise ValueError('No other interpreter in the booth to hand off to.')
            is_active = booth.active_interpreter_id == requester_id
            booth.handoff_state = 'offered' if is_active else 'requested'
            booth.handoff_initiator_id = requester_id
            return booth.as_public_dict()

    async def accept_handoff(
        self,
        booth_id: str,
        acceptor_id: str,
        language: str,
        channel_id: str,
    ) -> dict:
        """Complete a pending mic handoff.

        The acceptor must be the *other* side of the negotiation:
        - If ``handoff_state == 'offered'`` the acceptor must be a passive
          interpreter (they click TAKE OVER to accept).
        - If ``handoff_state == 'requested'`` the acceptor must be the
          active interpreter (they click PASS MIC to yield).

        On success the active interpreter flips and the handoff resets to
        ``'idle'``.
        """
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            acceptor = booth.participants.get(acceptor_id)
            if acceptor is None:
                raise ValueError('Acceptor is not in this booth.')
            if booth.handoff_state == 'idle':
                raise ValueError('No handoff is in progress.')
            initiator_id = booth.handoff_initiator_id
            if acceptor_id == initiator_id:
                raise ValueError('The initiator cannot accept their own handoff.')

            if booth.handoff_state == 'offered':
                # Active offered → passive accepts → passive becomes active
                if booth.active_interpreter_id == acceptor_id:
                    raise ValueError('Active interpreter cannot accept an offer they did not initiate.')
                new_active = acceptor_id
            elif booth.handoff_state == 'requested':
                # Passive requested → active accepts (yields) → requester becomes active
                if booth.active_interpreter_id != acceptor_id:
                    raise ValueError('Only the active interpreter can yield the mic.')
                new_active = initiator_id
            else:
                raise ValueError(f'Unexpected handoff state: {booth.handoff_state}')

            # Flip active interpreter
            booth.active_interpreter_id = new_active
            booth.handoff_state = 'idle'
            booth.handoff_initiator_id = None

            # Reset ingest/mic flags: only the new active keeps them
            for p in booth.participants.values():
                p.ingest_connected = p.participant_id == new_active and p.ingest_connected
                p.mic_active = p.participant_id == new_active and p.mic_active
                p.updated_at = utc_now_iso()
            booth.ingest_status = (
                'connected' if any(p.ingest_connected for p in booth.participants.values()) else 'disconnected'
            )
            return booth.as_public_dict()

    async def cancel_handoff(
        self,
        booth_id: str,
        requester_id: str,
        language: str,
        channel_id: str,
    ) -> dict:
        """Cancel an in-progress handoff (only the initiator can cancel)."""
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            if booth.handoff_state == 'idle':
                return booth.as_public_dict()
            if booth.handoff_initiator_id != requester_id:
                raise PermissionError('Only the handoff initiator can cancel.')
            booth.handoff_state = 'idle'
            booth.handoff_initiator_id = None
            return booth.as_public_dict()

    async def add_chat_message(
        self,
        booth_id: str,
        sender_id: str,
        body: str,
        language: str,
        channel_id: str,
    ) -> tuple[dict, dict]:
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            sender = booth.participants.get(sender_id)
            if sender is None:
                raise ValueError('Sender is not registered in booth.')
            message = ChatMessage(
                message_id=uuid4().hex,
                sender_id=sender_id,
                sender_name=sender.display_name,
                body=body.strip(),
            )
            if not message.body:
                raise ValueError('Message body cannot be empty.')
            booth.chat_messages.append(message)
            if len(booth.chat_messages) > 500:
                booth.chat_messages = booth.chat_messages[-500:]
            return asdict(message), booth.as_public_dict()

    async def update_participant_state(
        self,
        booth_id: str,
        participant_id: str,
        language: str,
        channel_id: str,
        *,
        mic_active: bool | None = None,
        ingest_connected: bool | None = None,
        connected: bool | None = None,
    ) -> dict:
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            participant = booth.participants.get(participant_id)
            if participant is None:
                raise ValueError('Participant does not exist in booth.')
            wants_publisher_state = mic_active is True or ingest_connected is True
            if wants_publisher_state and participant.role != 'interpreter':
                raise PermissionError('Only interpreter role can publish audio.')
            if wants_publisher_state and booth.active_interpreter_id != participant_id:
                raise PermissionError('Only the active interpreter can mark mic or ingest active.')
            if mic_active is not None:
                participant.mic_active = mic_active
            if ingest_connected is not None:
                participant.ingest_connected = ingest_connected
            if connected is not None:
                participant.connected = connected
            participant.updated_at = utc_now_iso()
            booth.ingest_status = (
                'connected' if any(p.ingest_connected for p in booth.participants.values()) else 'disconnected'
            )
            return booth.as_public_dict()

    async def check_publish_permission(
        self,
        booth_id: str,
        participant_id: str,
        language: str,
        channel_id: str,
    ) -> None:
        """Raise PermissionError if participant may not publish audio.

        Checks two conditions (Layer 1 enforcement):
        1. Participant must have the ``interpreter`` role.
        2. Participant must be the booth's active interpreter.
        """
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            participant = booth.participants.get(participant_id)
            if participant is None:
                raise ValueError('Participant does not exist in booth.')
            if participant.role != 'interpreter':
                raise PermissionError('Only interpreter role can publish audio.')
            if booth.active_interpreter_id != participant_id:
                raise PermissionError('Only the active interpreter can publish audio.')

    async def is_active_interpreter(self, booth_id: str, participant_id: str, language: str, channel_id: str) -> bool:
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            return booth.active_interpreter_id == participant_id

    async def list_booths_for_event(self, event_slug: str) -> list[dict]:
        """Return public snapshots of all booths belonging to *event_slug*."""
        async with self._lock:
            return [
                booth.as_public_dict()
                for booth in self._booths.values()
                if booth.event_slug == event_slug
            ]

    async def get_booth(self, booth_id: str) -> dict | None:
        """Return the public dict for an existing booth, or None."""
        async with self._lock:
            booth = self._booths.get(booth_id)
            return booth.as_public_dict() if booth is not None else None

    def get_booth_sync(self, booth_id: str) -> Booth | None:
        """Return the raw Booth dataclass without locking.

        Safe for read-only admin panel access (snapshot of volatile state).
        """
        return self._booths.get(booth_id)

    async def get_booth_for_event(self, event_slug: str, language_code: str) -> dict | None:
        """Return the booth for a specific event + language, or None.

        Unlike :meth:`snapshot`, this never auto-creates a booth.
        """
        booth_id = make_booth_id(event_slug, language_code)
        return await self.get_booth(booth_id)

    async def validate_booth_event(self, booth_id: str, expected_event: str) -> None:
        """Raise PermissionError if *booth_id* does not belong to *expected_event*.

        Used by event-scoped API endpoints to prevent cross-event access.
        """
        try:
            event_slug, _ = parse_booth_id(booth_id)
        except ValueError:
            event_slug = ''
        if event_slug != expected_event:
            raise PermissionError(
                f"Booth '{booth_id}' does not belong to event '{expected_event}'."
            )

    async def set_ingest_status(self, booth_id: str, status: str, language: str, channel_id: str) -> dict:
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            booth.ingest_status = status
            return booth.as_public_dict()
