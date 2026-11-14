from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from portal.attendance import AttendanceTracker
from portal.booth_identity import (
    BoothInstance,
    make_booth_id,
    make_mediamtx_path,
    parse_booth_id,
    validate_event_slug,
    validate_instance,
    validate_language_code,
)
from portal.roles import Role, can_go_live, can_set_active, role_label
from portal.tasks import track_task

# Booth roles come from portal.roles, the one place roles are defined (#757).
ParticipantRole = Role

# Every booth:state carries recent chat messages, so keep each one small.
CHAT_MESSAGE_MAX_LENGTH = 1000

# Every booth:state carries each participant's id, name, language and channel, so keep them small.
# The name and language limits match User.display_name and DBBooth.language_name; real channel
# ids are MediaMTX paths built from an event slug of at most 64 characters, and the server's
# participant ids are 32-character hex strings.
DISPLAY_NAME_MAX_LENGTH = 200
LANGUAGE_MAX_LENGTH = 100
CHANNEL_ID_MAX_LENGTH = 200
PARTICIPANT_ID_MAX_LENGTH = 64


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
    # Key of the session that joined (see portal.auth.booth_session_key). Server-only.
    joined_by: str | None = field(default=None, repr=False)
    # Who this is, for attendance records (#1215). Server-only.
    user_id: int | None = field(default=None, repr=False)
    invite_public_id: str | None = field(default=None, repr=False)
    # The socket that currently holds this record. A reconnect that reclaims the
    # record replaces it, so a late disconnect from the old socket changes nothing. Server-only.
    connection_id: str | None = field(default=None, repr=False)


# Participant fields that never leave the server.
_SERVER_ONLY_FIELDS = ("joined_by", "user_id", "invite_public_id", "connection_id")


def participant_public_dict(participant: Participant) -> dict:
    """A participant as sent to browsers, with what their role allows.

    Clients use ``can_go_live`` / ``can_set_active`` instead of comparing role
    names, so roles stay defined only in portal.roles.
    """
    public = asdict(participant)
    for server_only in _SERVER_ONLY_FIELDS:
        del public[server_only]
    return {
        **public,
        "role_label": role_label(participant.role),
        "can_go_live": can_go_live(participant.role),
        "can_set_active": can_set_active(participant.role),
    }


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
    instance: BoothInstance = "primary"
    mediamtx_path: str = ""
    room_id: int | None = None
    active_interpreter_id: str | None = None
    handoff_state: str = "idle"
    handoff_initiator_id: str | None = None
    broadcast_unlocked: bool = False
    participants: dict[str, Participant] = field(default_factory=dict)
    chat_messages: list[ChatMessage] = field(default_factory=list)
    ingest_status: str = "disconnected"

    def __post_init__(self) -> None:
        if self.event_slug and self.language_code and self.room_id is not None:
            if not self.mediamtx_path:
                self.mediamtx_path = make_mediamtx_path(self.event_slug, self.room_id, self.language_code)
            if not self.channel_id:
                self.channel_id = self.mediamtx_path

    def refresh_ingest_status(self) -> None:
        """Set ``ingest_status`` from whether any participant is still publishing."""
        self.ingest_status = (
            "connected" if any(p.ingest_connected for p in self.participants.values()) else "disconnected"
        )

    def live_record(self) -> dict | None:
        """What must survive a portal restart, or None when nothing here is live.

        Mission Control's Go Live switch and who holds the active slot. Only an
        active interpreter with a session key is kept: that key is what lets the
        same person take the slot back after the restart, and nobody else.
        """
        active = self.participants.get(self.active_interpreter_id) if self.active_interpreter_id else None
        if active is not None and not active.joined_by:
            active = None
        if not self.broadcast_unlocked and active is None:
            return None
        return {
            "booth_id": self.booth_id,
            "language": self.language,
            "channel_id": self.channel_id,
            "room_id": self.room_id,
            "broadcast_unlocked": self.broadcast_unlocked,
            "active_participant_id": active.participant_id if active else None,
            "active_session_key": active.joined_by if active else None,
            "active_display_name": active.display_name if active else None,
            "active_role": str(active.role) if active else None,
            "active_user_id": active.user_id if active else None,
            "active_invite_public_id": active.invite_public_id if active else None,
            "active_mic_active": bool(active and active.mic_active),
            "active_ingest_connected": bool(active and active.ingest_connected),
        }

    def as_public_dict(self) -> dict:
        return {
            "booth_id": self.booth_id,
            "event_slug": self.event_slug,
            "language_code": self.language_code,
            "instance": self.instance,
            "mediamtx_path": self.mediamtx_path,
            "room_id": self.room_id,
            "language": self.language,
            "channel_id": self.channel_id,
            "active_interpreter_id": self.active_interpreter_id,
            "handoff_state": self.handoff_state,
            "handoff_initiator_id": self.handoff_initiator_id,
            "broadcast_unlocked": self.broadcast_unlocked,
            "ingest_status": self.ingest_status,
            "participants": [participant_public_dict(p) for p in self.participants.values()],
            "chat_messages": [asdict(m) for m in self.chat_messages[-100:]],
        }


def _pick_next_interpreter(booth: Booth) -> str | None:
    """Return the next active-interpreter-eligible participant ID, or None.

    Any role that can go live qualifies (see portal.roles).  Mission control no longer
    calls booth:join (silent observer mode), so these roles will only be
    present when the participant is actually in the interpreter booth.
    """
    for p in booth.participants.values():
        # Someone whose socket dropped gets the slot back on reconnect if it is still free.
        if can_go_live(p.role) and p.connected:
            return p.participant_id
    return None


class BoothRegistry:
    def __init__(self, attendance: AttendanceTracker | None = None) -> None:
        self._booths: dict[str, Booth] = {}
        self._lock = asyncio.Lock()
        # Told about every participant change, under the lock (#1215).
        self.attendance = attendance or AttendanceTracker()
        # Booths whose live record may have changed since portal.booth_recovery last saved it.
        self._dirty: set[str] = set()
        self._dirty_event = asyncio.Event()

    def _changed(self, booth: Booth) -> None:
        """Record a change to *booth*'s participants or live state. Caller holds the lock."""
        self.attendance.observe(booth)
        self._mark_dirty(booth.booth_id)

    def _mark_dirty(self, booth_id: str) -> None:
        self._dirty.add(booth_id)
        self._dirty_event.set()

    async def wait_for_changes(self) -> None:
        """Return once a booth's live record may have changed since the last ``take_changes``."""
        await self._dirty_event.wait()

    async def take_changes(self) -> dict[str, dict | None]:
        """Live records of the booths changed since the last call; None for a booth with nothing live."""
        async with self._lock:
            self._dirty_event.clear()
            changed = {}
            for booth_id in self._dirty:
                booth = self._booths.get(booth_id)
                changed[booth_id] = booth.live_record() if booth is not None else None
            self._dirty.clear()
            return changed

    def requeue_changes(self, booth_ids: set[str]) -> None:
        """Save these booths again on the next pass (after a failed save)."""
        for booth_id in booth_ids:
            self._mark_dirty(booth_id)

    def booth_ids(self) -> list[str]:
        return list(self._booths)

    async def restore(self, records: list[dict]) -> list[tuple[str, str]]:
        """Rebuild booths from the live records saved before a restart.

        Each active interpreter comes back as a disconnected placeholder that only
        the same session can reclaim (see ``join_participant``). Returns
        ``(booth_id, participant_id)`` for each placeholder, so the caller can
        let it expire if nobody reclaims it.
        """
        placeholders = []
        async with self._lock:
            for record in records:
                booth = self._get_or_create_booth(
                    record["booth_id"], record["language"], record["channel_id"], room_id=record["room_id"]
                )
                booth.broadcast_unlocked = bool(record["broadcast_unlocked"])
                participant_id = record.get("active_participant_id")
                session_key = record.get("active_session_key")
                role = record.get("active_role")
                if participant_id and session_key and role and can_go_live(role):
                    booth.participants[participant_id] = Participant(
                        participant_id=participant_id,
                        display_name=record.get("active_display_name") or "Interpreter",
                        role=role,
                        language=booth.language,
                        channel_id=booth.channel_id,
                        mic_active=bool(record.get("active_mic_active")),
                        ingest_connected=bool(record.get("active_ingest_connected")),
                        connected=False,
                        joined_by=session_key,
                        user_id=record.get("active_user_id"),
                        invite_public_id=record.get("active_invite_public_id"),
                    )
                    booth.active_interpreter_id = participant_id
                    placeholders.append((booth.booth_id, participant_id))
                booth.refresh_ingest_status()
                self.attendance.observe(booth)
                # Saved again so the recovery heartbeat keeps these rows current.
                self._mark_dirty(booth.booth_id)
        return placeholders

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
                event_slug, parsed_room_id, language_code = parse_booth_id(booth_id)
                room_id = room_id if room_id is not None else parsed_room_id
            except ValueError:
                event_slug = ""
                language_code = ""
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
        room_id: int,
        channel_id: str = "",
        instance: BoothInstance = "primary",
    ) -> dict:
        """Create a booth using validated identity coordinates.

        This is the preferred entry point for new code.  The booth ID,
        MediaMTX path, and default ``channel_id`` are derived automatically
        from the coordinates.  When no explicit ``channel_id`` is given it
        defaults to the MediaMTX path (``{event_slug}/{language_code}``).

        ``room_id`` is the Eventyay Room and is part of the booth ID, so it
        is required.

        Raises ``ValueError`` if the slug, room ID or language code is
        invalid, or if a booth with the same ID already exists.
        """
        slug = validate_event_slug(event_slug)
        code = validate_language_code(language_code)
        inst = validate_instance(instance)
        booth_id = make_booth_id(slug, room_id, code)
        mtx_path = make_mediamtx_path(slug, room_id, code)

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
        *,
        reclaim: bool = False,
        joined_by: str | None = None,
        user_id: int | None = None,
        invite_public_id: str | None = None,
        connection_id: str | None = None,
    ) -> tuple[Participant, dict]:
        """Add a participant, auto-assigning the active slot if it is free.

        An ID already present in the booth is taken back, with its active slot and
        live flags, when ``reclaim`` says the caller's own socket holds it or when
        the same session (``joined_by``) joined it: a reconnect after a network
        drop or a portal restart. Anyone else naming it gets a fresh ID instead of
        taking over that record, e.g. the active interpreter's.
        ``joined_by`` records the session that joined, for checks made outside the
        socket; ``connection_id`` names the socket that now holds the record.
        """
        fields = [
            ("display_name", display_name, DISPLAY_NAME_MAX_LENGTH),
            ("language", language, LANGUAGE_MAX_LENGTH),
            ("channel_id", channel_id, CHANNEL_ID_MAX_LENGTH),
        ]
        if participant_id is not None:
            fields.append(("participant_id", participant_id, PARTICIPANT_ID_MAX_LENGTH))
        for name, value, max_length in fields:
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string.")
            if len(value) > max_length:
                raise ValueError(f"{name} must not exceed {max_length} characters.")
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id, room_id=room_id)
            existing = booth.participants.get(participant_id) if participant_id else None
            if existing is not None:
                same_session = joined_by is not None and existing.joined_by == joined_by
                if reclaim or same_session:
                    self._reclaim(booth, existing, display_name, role, language, channel_id, connection_id,
                                  user_id=user_id, invite_public_id=invite_public_id)
                    self._changed(booth)
                    return existing, booth.as_public_dict()
                participant_id = None
            participant = Participant(
                participant_id=participant_id or uuid4().hex,
                display_name=display_name.strip() or "Interpreter",
                role=role,
                language=language.strip() or booth.language,
                channel_id=channel_id.strip() or booth.channel_id,
                joined_by=joined_by,
                user_id=user_id,
                invite_public_id=invite_public_id,
                connection_id=connection_id,
            )
            booth.participants[participant.participant_id] = participant
            # Auto-assign active slot to any role that can go live.
            # Mission control now uses silent-observer mode (no booth:join),
            # so only real interpreter-booth participants appear here.
            if (
                can_go_live(participant.role)
                and booth.active_interpreter_id is None
            ):
                booth.active_interpreter_id = participant.participant_id
            self._changed(booth)
            return participant, booth.as_public_dict()

    @staticmethod
    def _reclaim(
        booth: Booth,
        participant: Participant,
        display_name: str,
        role: ParticipantRole,
        language: str,
        channel_id: str,
        connection_id: str | None,
        *,
        user_id: int | None,
        invite_public_id: str | None,
    ) -> None:
        """Hand *participant* to a new socket, keeping its active slot and live flags.

        The WHIP publish goes straight to MediaMTX, so an interpreter's audio can
        outlive their booth socket; keeping the flags keeps the booth showing it.
        """
        participant.display_name = display_name.strip() or participant.display_name
        participant.role = role
        participant.language = language.strip() or participant.language
        participant.channel_id = channel_id.strip() or participant.channel_id
        participant.connected = True
        participant.connection_id = connection_id
        participant.user_id = user_id if user_id is not None else participant.user_id
        participant.invite_public_id = invite_public_id or participant.invite_public_id
        participant.updated_at = utc_now_iso()
        if not can_go_live(role):
            # The session lost the right to go live while it was away.
            participant.mic_active = False
            participant.ingest_connected = False
            if booth.active_interpreter_id == participant.participant_id:
                booth.active_interpreter_id = _pick_next_interpreter(booth)
                booth.handoff_state = "idle"
                booth.handoff_initiator_id = None
            booth.refresh_ingest_status()
        elif booth.active_interpreter_id is None:
            booth.active_interpreter_id = participant.participant_id

    async def leave_participant(
        self,
        booth_id: str,
        participant_id: str,
        language: str,
        channel_id: str,
        *,
        connection_id: str | None = None,
    ) -> dict:
        """Remove a participant now. With ``connection_id``, only if that socket still holds them."""
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            participant = booth.participants.get(participant_id)
            if participant is not None and (connection_id is None or participant.connection_id == connection_id):
                self._remove_participant(booth, participant_id)
            return booth.as_public_dict()

    async def disconnect_participant(self, booth_id: str, participant_id: str, connection_id: str) -> dict | None:
        """Mark a participant whose socket dropped as reconnecting, keeping their place.

        Returns the new state, or None when another socket already holds the record.
        """
        async with self._lock:
            booth = self._booths.get(booth_id)
            participant = booth.participants.get(participant_id) if booth else None
            if participant is None or participant.connection_id != connection_id:
                return None
            participant.connected = False
            participant.updated_at = utc_now_iso()
            # Their mic request or offer lapses: accepting it would hand the mic to someone not there.
            if booth.handoff_initiator_id == participant_id:
                booth.handoff_state = "idle"
                booth.handoff_initiator_id = None
            self._changed(booth)
            return booth.as_public_dict()

    async def expire_participant(self, booth_id: str, participant_id: str, connection_id: str | None) -> dict | None:
        """Remove a participant who did not reconnect in time.

        Returns the new state, or None when they came back (or left) meanwhile.
        """
        async with self._lock:
            booth = self._booths.get(booth_id)
            participant = booth.participants.get(participant_id) if booth else None
            if participant is None or participant.connected or participant.connection_id != connection_id:
                return None
            self._remove_participant(booth, participant_id)
            return booth.as_public_dict()

    async def remove_disconnected(
        self,
        *,
        user_id: int | None = None,
        invite_public_id: str | None = None,
        booth_id: str | None = None,
    ) -> list[tuple[str, dict]]:
        """Remove participants kept after their socket dropped, for a user or invite whose access ended.

        Covers records held for the reconnect grace and placeholders restored after a restart,
        which have no socket for revocation to close. Returns ``(booth_id, state)`` per changed booth.
        """
        if user_id is None and invite_public_id is None:
            return []
        changed: list[tuple[str, dict]] = []
        async with self._lock:
            for key, booth in list(self._booths.items()):
                if booth_id is not None and key != booth_id:
                    continue
                matches = [
                    p.participant_id
                    for p in booth.participants.values()
                    if not p.connected
                    and (
                        (user_id is not None and p.user_id == user_id)
                        or (invite_public_id is not None and p.invite_public_id == invite_public_id)
                    )
                ]
                for participant_id in matches:
                    self._remove_participant(booth, participant_id)
                if matches:
                    changed.append((key, booth.as_public_dict()))
        return changed

    def _remove_participant(self, booth: Booth, participant_id: str) -> None:
        """Take *participant_id* out of *booth* and hand on what they held. Caller holds the lock."""
        participant = booth.participants.pop(participant_id, None)
        if participant is not None:
            was_active = booth.active_interpreter_id == participant_id
            if was_active:
                booth.active_interpreter_id = _pick_next_interpreter(booth)
            # A handoff cannot complete once its initiator or the active interpreter has left.
            if was_active or participant_id == booth.handoff_initiator_id:
                booth.handoff_state = "idle"
                booth.handoff_initiator_id = None
            # The booth is no longer live if the participant who left was the one publishing.
            if participant.ingest_connected:
                booth.refresh_ingest_status()
        if not booth.participants:
            booth.ingest_status = "disconnected"
            booth.handoff_state = "idle"
        self._changed(booth)

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
            # Locking the booth (Mission Control "Stop") forces every publisher
            # offline so the room truly stops: mic + ingest flags are cleared and
            # the aggregate ingest status returns to disconnected. This keeps the
            # booth/Mission Control UI consistent even if a client is slow to tear
            # down its own WHIP publish.
            if not unlocked:
                for participant in booth.participants.values():
                    participant.mic_active = False
                    participant.ingest_connected = False
                    participant.updated_at = utc_now_iso()
                booth.ingest_status = "disconnected"
            self._changed(booth)

            # Enqueue webhook in background
            import asyncio

            import portal.webhooks.worker as _wh

            track_task(
                asyncio.create_task(
                    _wh.enqueue_webhook("session.status_changed", {"booth_id": booth_id, "is_active": unlocked})
                )
            )

            return booth.as_public_dict()

    async def set_active_interpreter(
        self,
        booth_id: str,
        requester_id: str,
        target_id: str,
        language: str,
        channel_id: str,
    ) -> dict:
        """Make *target_id* the active interpreter and take everyone else off air.

        Only roles that can set active (room coordinators, event owners,
        Admins) or the current active interpreter may do this. A passive
        interpreter cannot name themselves here: that would take the mic from
        whoever is live, so they have to use the handoff flow instead.

        Raises ``ValueError`` if either participant is missing or the target
        cannot go live, and ``PermissionError`` if the requester may not
        reassign.
        """
        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            requester = booth.participants.get(requester_id)
            target = booth.participants.get(target_id)
            if requester is None or target is None:
                raise ValueError("Requester or target participant does not exist in this booth.")
            if not can_go_live(target.role):
                raise ValueError("Only people who can go live can be set active.")
            requester_can_reassign = can_set_active(requester.role)
            requester_is_active = booth.active_interpreter_id == requester_id
            if not requester_can_reassign and not requester_is_active:
                raise PermissionError(
                    "Only room coordinators, event owners, Admins or the active interpreter can reassign another interpreter."
                )
            booth.active_interpreter_id = target_id
            # A direct reassignment supersedes any handoff in progress.
            booth.handoff_state = "idle"
            booth.handoff_initiator_id = None
            for p in booth.participants.values():
                p.ingest_connected = p.participant_id == target_id and p.ingest_connected
                p.mic_active = p.participant_id == target_id and p.mic_active
                p.updated_at = utc_now_iso()
            booth.refresh_ingest_status()
            self._changed(booth)
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
                raise ValueError("Requester is not in this booth.")
            if not can_go_live(requester.role):
                raise PermissionError("Only interpreters and coordinators can initiate handoffs.")
            if booth.handoff_state != "idle":
                raise ValueError("A handoff is already in progress.")
            # Need at least one other participant who can take over
            other_interpreters = [
                p
                for p in booth.participants.values()
                if can_go_live(p.role)
                and p.participant_id != requester_id
            ]
            if not other_interpreters:
                raise ValueError("No other interpreter in the booth to hand off to.")
            is_active = booth.active_interpreter_id == requester_id
            booth.handoff_state = "offered" if is_active else "requested"
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
                raise ValueError("Acceptor is not in this booth.")
            if booth.handoff_state == "idle":
                raise ValueError("No handoff is in progress.")
            initiator_id = booth.handoff_initiator_id
            if acceptor_id == initiator_id:
                raise ValueError("The initiator cannot accept their own handoff.")

            if booth.handoff_state == "offered":
                # Active offered → passive accepts → passive becomes active
                if booth.active_interpreter_id == acceptor_id:
                    raise ValueError("Active interpreter cannot accept an offer they did not initiate.")
                if not can_go_live(acceptor.role):
                    raise PermissionError("Only interpreters and coordinators can take over the mic.")
                new_active = acceptor_id
            elif booth.handoff_state == "requested":
                # Passive requested → active accepts (yields) → requester becomes active
                if booth.active_interpreter_id != acceptor_id:
                    raise ValueError("Only the active interpreter can yield the mic.")
                new_active = initiator_id
            else:
                raise ValueError(f"Unexpected handoff state: {booth.handoff_state}")
            incoming = booth.participants.get(new_active)
            if incoming is None or not incoming.connected:
                raise ValueError("That interpreter has disconnected; the mic stays where it is.")

            # Flip active interpreter
            booth.active_interpreter_id = new_active
            booth.handoff_state = "idle"
            booth.handoff_initiator_id = None

            # Reset ingest/mic flags: only the new active keeps them
            for p in booth.participants.values():
                p.ingest_connected = p.participant_id == new_active and p.ingest_connected
                p.mic_active = p.participant_id == new_active and p.mic_active
                p.updated_at = utc_now_iso()
            booth.refresh_ingest_status()
            self._changed(booth)
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
            if booth.handoff_state == "idle":
                return booth.as_public_dict()
            if booth.handoff_initiator_id != requester_id:
                raise PermissionError("Only the handoff initiator can cancel.")
            booth.handoff_state = "idle"
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
                raise ValueError("Sender is not registered in booth.")
            if not isinstance(body, str):
                raise ValueError("Message body must be a string.")
            message = ChatMessage(
                message_id=uuid4().hex,
                sender_id=sender_id,
                sender_name=sender.display_name,
                body=body.strip(),
            )
            if not message.body:
                raise ValueError("Message body cannot be empty.")
            if len(message.body) > CHAT_MESSAGE_MAX_LENGTH:
                raise ValueError(f"Message must not exceed {CHAT_MESSAGE_MAX_LENGTH} characters.")
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
                raise ValueError("Participant does not exist in booth.")
            # Reject non-boolean flags so a truthy value like 1 or "true" cannot slip
            # past the publisher permission checks below (which compare with `is True`).
            for name, value in (("mic_active", mic_active), ("ingest_connected", ingest_connected), ("connected", connected)):
                if value is not None and not isinstance(value, bool):
                    raise ValueError(f"{name} must be a boolean.")

            wants_publisher_state = mic_active is True or ingest_connected is True
            if wants_publisher_state and not can_go_live(participant.role):
                raise PermissionError("Your role does not permit publishing audio.")
            if wants_publisher_state and booth.active_interpreter_id != participant_id:
                raise PermissionError("Only the active interpreter can mark mic or ingest active.")
            if mic_active is not None:
                participant.mic_active = mic_active
            if ingest_connected is not None:
                participant.ingest_connected = ingest_connected
            if connected is not None:
                participant.connected = connected
            participant.updated_at = utc_now_iso()
            booth.refresh_ingest_status()
            self._changed(booth)
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
        1. Participant's role must carry BOOTH_GO_LIVE permission.
        2. Participant must be the booth's active interpreter.
        """

        async with self._lock:
            booth = self._get_or_create_booth(booth_id, language, channel_id)
            participant = booth.participants.get(participant_id)
            if participant is None:
                raise ValueError("Participant does not exist in booth.")
            if not can_go_live(participant.role):
                raise PermissionError("Your role does not permit publishing audio.")
            if booth.active_interpreter_id != participant_id:
                raise PermissionError("Only the active interpreter can publish audio.")

    async def can_stop_live(self, booth_id: str, session_keys: set[str]) -> bool:
        """True if nobody in the booth is live, or one of *session_keys* joined the live participant."""
        async with self._lock:
            booth = self._booths.get(booth_id)
            active = booth.participants.get(booth.active_interpreter_id) if booth else None
            return active is None or active.joined_by in session_keys

    async def list_booths_for_event(self, event_slug: str) -> list[dict]:
        """Return public snapshots of all booths belonging to *event_slug*."""
        async with self._lock:
            return [booth.as_public_dict() for booth in self._booths.values() if booth.event_slug == event_slug]

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

    async def get_booth_for_event(self, event_slug: str, room_id: int, language_code: str) -> dict | None:
        """Return the booth for a specific event + language, or None.

        Unlike :meth:`snapshot`, this never auto-creates a booth.
        """
        booth_id = make_booth_id(event_slug, room_id, language_code)
        return await self.get_booth(booth_id)

    async def remove_booth(self, event_slug: str, room_id: int, language_code: str) -> None:
        """Remove a booth state entirely.

        Removes from MediaMTX (if path matches), terminates any associated
        transcription worker, and removes the in-memory state.
        """
        booth_id = make_booth_id(event_slug, room_id, language_code)
        async with self._lock:
            self._booths.pop(booth_id, None)
            self.attendance.forget_booth(booth_id)
            self._mark_dirty(booth_id)

    async def validate_booth_event(self, booth_id: str, expected_event: str) -> None:
        """Raise PermissionError if *booth_id* does not belong to *expected_event*.

        Used by event-scoped API endpoints to prevent cross-event access.
        """
        try:
            event_slug, _, _ = parse_booth_id(booth_id)
        except ValueError:
            event_slug = ""
        if event_slug != expected_event:
            raise PermissionError(f"Booth '{booth_id}' does not belong to event '{expected_event}'.")
