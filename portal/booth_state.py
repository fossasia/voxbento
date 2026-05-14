from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Literal
from uuid import uuid4

ParticipantRole = Literal['interpreter', 'coordinator', 'listener']


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
    language: str
    channel_id: str
    active_interpreter_id: str | None = None
    handoff_state: str = 'idle'
    participants: dict[str, Participant] = field(default_factory=dict)
    chat_messages: list[ChatMessage] = field(default_factory=list)
    ingest_status: str = 'disconnected'

    def as_public_dict(self) -> dict:
        return {
            'booth_id': self.booth_id,
            'language': self.language,
            'channel_id': self.channel_id,
            'active_interpreter_id': self.active_interpreter_id,
            'handoff_state': self.handoff_state,
            'ingest_status': self.ingest_status,
            'participants': [asdict(participant) for participant in self.participants.values()],
            'chat_messages': [asdict(message) for message in self.chat_messages[-100:]],
        }


class BoothRegistry:
    def __init__(self) -> None:
        self._booths: dict[str, Booth] = {}
        self._lock = RLock()

    def get_or_create_booth(self, booth_id: str, language: str, channel_id: str) -> Booth:
        with self._lock:
            booth = self._booths.get(booth_id)
            if booth is not None:
                return booth
            booth = Booth(booth_id=booth_id, language=language, channel_id=channel_id)
            self._booths[booth_id] = booth
            return booth

    def snapshot(self, booth_id: str, language: str, channel_id: str) -> dict:
        with self._lock:
            booth = self.get_or_create_booth(booth_id, language, channel_id)
            return booth.as_public_dict()

    def join_participant(
        self,
        booth_id: str,
        display_name: str,
        role: ParticipantRole,
        language: str,
        channel_id: str,
        participant_id: str | None = None,
    ) -> tuple[Participant, dict]:
        with self._lock:
            booth = self.get_or_create_booth(booth_id, language, channel_id)
            participant = Participant(
                participant_id=participant_id or uuid4().hex,
                display_name=display_name.strip() or 'Interpreter',
                role=role,
                language=language.strip() or booth.language,
                channel_id=channel_id.strip() or booth.channel_id,
            )
            booth.participants[participant.participant_id] = participant
            if participant.role == 'interpreter' and booth.active_interpreter_id is None:
                booth.active_interpreter_id = participant.participant_id
            return participant, booth.as_public_dict()

    def leave_participant(self, booth_id: str, participant_id: str, language: str, channel_id: str) -> dict:
        with self._lock:
            booth = self.get_or_create_booth(booth_id, language, channel_id)
            participant = booth.participants.pop(participant_id, None)
            if participant is None:
                return booth.as_public_dict()
            if booth.active_interpreter_id == participant_id:
                booth.active_interpreter_id = self._pick_next_interpreter(booth)
                booth.handoff_state = 'pending' if booth.active_interpreter_id else 'idle'
            if not booth.participants:
                booth.ingest_status = 'disconnected'
                booth.handoff_state = 'idle'
            return booth.as_public_dict()

    def set_active_interpreter(
        self,
        booth_id: str,
        requester_id: str,
        target_id: str,
        language: str,
        channel_id: str,
    ) -> dict:
        with self._lock:
            booth = self.get_or_create_booth(booth_id, language, channel_id)
            requester = booth.participants.get(requester_id)
            target = booth.participants.get(target_id)
            if requester is None or target is None:
                raise ValueError('Requester or target participant does not exist in this booth.')
            if target.role != 'interpreter':
                raise ValueError('Only participants with interpreter role can be set active.')
            requester_is_coordinator = requester.role == 'coordinator'
            requester_is_active_interpreter = booth.active_interpreter_id == requester_id
            requester_is_target = requester_id == target_id
            if not requester_is_coordinator and not requester_is_active_interpreter and not requester_is_target:
                raise PermissionError('Only coordinators or the active interpreter can reassign another interpreter.')
            booth.active_interpreter_id = target_id
            booth.handoff_state = 'completed'
            for participant in booth.participants.values():
                participant.ingest_connected = participant.participant_id == target_id and participant.ingest_connected
                participant.mic_active = participant.participant_id == target_id and participant.mic_active
                participant.updated_at = utc_now_iso()
            booth.ingest_status = 'connected' if any(p.ingest_connected for p in booth.participants.values()) else 'disconnected'
            return booth.as_public_dict()

    def add_chat_message(
        self,
        booth_id: str,
        sender_id: str,
        body: str,
        language: str,
        channel_id: str,
    ) -> tuple[dict, dict]:
        with self._lock:
            booth = self.get_or_create_booth(booth_id, language, channel_id)
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

    def update_participant_state(
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
        with self._lock:
            booth = self.get_or_create_booth(booth_id, language, channel_id)
            participant = booth.participants.get(participant_id)
            if participant is None:
                raise ValueError('Participant does not exist in booth.')
            wants_publisher_state = mic_active is True or ingest_connected is True
            if wants_publisher_state and booth.active_interpreter_id != participant_id:
                raise PermissionError('Only the active interpreter can mark mic or ingest active.')
            if mic_active is not None:
                participant.mic_active = mic_active
            if ingest_connected is not None:
                participant.ingest_connected = ingest_connected
            if connected is not None:
                participant.connected = connected
            participant.updated_at = utc_now_iso()
            booth.ingest_status = 'connected' if any(p.ingest_connected for p in booth.participants.values()) else 'disconnected'
            return booth.as_public_dict()

    def is_active_interpreter(self, booth_id: str, participant_id: str, language: str, channel_id: str) -> bool:
        with self._lock:
            booth = self.get_or_create_booth(booth_id, language, channel_id)
            return booth.active_interpreter_id == participant_id

    def set_ingest_status(self, booth_id: str, status: str, language: str, channel_id: str) -> dict:
        with self._lock:
            booth = self.get_or_create_booth(booth_id, language, channel_id)
            booth.ingest_status = status
            return booth.as_public_dict()

    @staticmethod
    def _pick_next_interpreter(booth: Booth) -> str | None:
        for participant in booth.participants.values():
            if participant.role == 'interpreter':
                return participant.participant_id
        return None
