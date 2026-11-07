from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import WebSocket, WebSocketDisconnect

import portal.webhooks.worker as _wh_worker
from portal.auth import can_perform_role
from portal.booth_identity import booth_id_to_mediamtx_path
from portal.booth_publish import TICKET_TTL_SECS, issue_ticket
from portal.globals import booths
from portal.roles import ALL_ROLES, INTERPRETER, can_set_active


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


_PROCESS_START_TIME: datetime = utc_now()

WS_CLOSE_REVOKED: int = 4003
REASON_MEMBERSHIP_REMOVED: str = "Membership removed"
REASON_ACCOUNT_DEACTIVATED: str = "Account deactivated"
REASON_AUTH_REVOKED: str = "Authorization revoked"
REASON_SESSION_EXPIRED: str = "Session authorization expired"
REASON_INVITE_REVOKED: str = "Invitation revoked"


@dataclass
class Session:
    booth_id: str
    participant_id: str | None
    language: str | None
    channel_id: str
    # Role derived from the bearer cookie at WS connection time — never from client data.
    granted_role: str | None = None
    user_id: int | None = None
    invite_token_id: str | int | None = None
    revoked: bool = False
    # Key of the session that opened the socket (see portal.auth.booth_session_key); recorded at join.
    session_key: str | None = None
    issued_at: datetime = field(default_factory=utc_now)


class ConnectionManager:
    def __init__(
        self,
        *,
        max_epochs: int = 10000,
        cache_ttl_seconds: float = 5.0,
        close_timeout_seconds: float = 2.0,
    ) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}
        # Keyed by id(ws) to avoid __hash__ issues with WebSocket objects
        self._sessions: dict[int, Session] = {}
        # Bounded LRU caches for user-wide and booth-specific revocation epochs.
        # Cleaned up / bounded to max_epochs so memory cannot grow without bound.
        self._max_epochs = max_epochs
        self._user_epochs: OrderedDict[int, int] = OrderedDict()
        self._booth_epochs: OrderedDict[tuple[int, str], int] = OrderedDict()
        # Injected batched membership checker: (booth_id, user_ids) -> set[int]
        self.membership_checker: Callable[[str, set[int]], Awaitable[set[int] | dict[int, str | None]]] | None = None
        self.token_checker: Callable[[str, set[str | int]], Awaitable[set[str | int]]] | None = None
        # Cache for allowed user_ids and token_ids per booth: booth_id -> (monotonic_timestamp, allowed_ids)
        self.cache_ttl_seconds = cache_ttl_seconds
        self.close_timeout_seconds = close_timeout_seconds
        self._allowed_cache: dict[str, tuple[float, set[int]]] = {}
        self._token_allowed_cache: dict[str, tuple[float, set[str | int]]] = {}
        self._token_epochs: OrderedDict[str | int, int] = OrderedDict()
        self._token_booth_epochs: OrderedDict[tuple[str | int, str], int] = OrderedDict()

    def set_membership_checker(
        self, checker: Callable[[str, set[int]], Awaitable[set[int] | dict[int, str | None]]] | None
    ) -> None:
        """Inject an asynchronous batch membership checker."""
        self.membership_checker = checker

    def set_token_checker(
        self, checker: Callable[[str, set[str | int]], Awaitable[set[str | int]]] | None
    ) -> None:
        """Inject an asynchronous batch invite token checker."""
        self.token_checker = checker

    def token_revocation_epoch(self, token_id: str | int, booth_id: str | None = None) -> tuple[int, int]:
        """Return (token_wide_epoch, booth_specific_epoch) for this token and booth."""
        token_epoch = self._token_epochs.get(token_id, 0)
        booth_epoch = self._token_booth_epochs.get((token_id, booth_id), 0) if booth_id else 0
        return (token_epoch, booth_epoch)

    def _bump_token_epoch(self, token_id: str | int) -> None:
        val = self._token_epochs.get(token_id, 0) + 1
        self._token_epochs[token_id] = val
        self._token_epochs.move_to_end(token_id)
        if len(self._token_epochs) > self._max_epochs:
            self._token_epochs.popitem(last=False)

    def _bump_token_booth_epoch(self, token_id: str | int, booth_id: str) -> None:
        key = (token_id, booth_id)
        val = self._token_booth_epochs.get(key, 0) + 1
        self._token_booth_epochs[key] = val
        self._token_booth_epochs.move_to_end(key)
        if len(self._token_booth_epochs) > self._max_epochs:
            self._token_booth_epochs.popitem(last=False)

    def revoke_token_sessions(self, token_id: str | int, booth_id: str | None = None) -> list[WebSocket]:
        """Revoke authorization for all active sessions of an invite token (optionally scoped to a booth)."""
        if booth_id is None:
            self._bump_token_epoch(token_id)
            self._token_allowed_cache.clear()
        else:
            self._bump_token_booth_epoch(token_id, booth_id)
            self._token_allowed_cache.pop(booth_id, None)

        revoked: list[WebSocket] = []
        for room_sockets in list(self._rooms.values()):
            for ws in list(room_sockets):
                session = self._sessions.get(id(ws))
                if session is not None and session.invite_token_id is not None and str(session.invite_token_id) == str(token_id):
                    if booth_id is None or session.booth_id == booth_id:
                        session.granted_role = None
                        session.revoked = True
                        revoked.append(ws)
        return revoked

    async def close_token_sessions(
        self,
        token_id: str | int,
        booth_id: str | None = None,
        code: int = WS_CLOSE_REVOKED,
        reason: str = REASON_INVITE_REVOKED,
    ) -> None:
        """Revoke authorization and concurrently disconnect all matching active sessions of an invite token."""
        revoked_sockets = self.revoke_token_sessions(token_id, booth_id)
        if not revoked_sockets:
            return
        tasks = [self.close_session(ws, code=code, reason=reason) for ws in revoked_sockets]
        await asyncio.gather(*tasks, return_exceptions=True)

    def revocation_epoch(self, user_id: int, booth_id: str | None = None) -> tuple[int, int]:
        """Return (user_wide_epoch, booth_specific_epoch) for this user and booth."""
        user_epoch = self._user_epochs.get(user_id, 0)
        booth_epoch = self._booth_epochs.get((user_id, booth_id), 0) if booth_id else 0
        return (user_epoch, booth_epoch)

    def _bump_user_epoch(self, user_id: int) -> None:
        val = self._user_epochs.get(user_id, 0) + 1
        self._user_epochs[user_id] = val
        self._user_epochs.move_to_end(user_id)
        if len(self._user_epochs) > self._max_epochs:
            self._user_epochs.popitem(last=False)

    def _bump_booth_epoch(self, user_id: int, booth_id: str) -> None:
        key = (user_id, booth_id)
        val = self._booth_epochs.get(key, 0) + 1
        self._booth_epochs[key] = val
        self._booth_epochs.move_to_end(key)
        if len(self._booth_epochs) > self._max_epochs:
            self._booth_epochs.popitem(last=False)

    def add(self, ws: WebSocket, session: Session) -> None:
        self._rooms.setdefault(session.booth_id, set()).add(ws)
        self._sessions[id(ws)] = session

    def remove(self, ws: WebSocket) -> Session | None:
        session = self._sessions.pop(id(ws), None)
        if session:
            room = self._rooms.get(session.booth_id)
            if room is not None:
                room.discard(ws)
                if not room:
                    self._rooms.pop(session.booth_id, None)
        return session

    def get_session(self, ws: WebSocket) -> Session | None:
        return self._sessions.get(id(ws))

    def revoke_session(self, ws: WebSocket) -> None:
        """Revoke authorization for a connected socket so it receives no further broadcasts."""
        session = self._sessions.get(id(ws))
        if session is not None:
            session.granted_role = None
            session.revoked = True

    def _force_abort_transport(self, ws: WebSocket) -> None:
        """Force-abort the underlying transport if available on timeout/unresponsive socket."""
        try:
            transport = getattr(ws, "transport", None) or getattr(ws, "_transport", None)
            if transport is not None and hasattr(transport, "abort"):
                transport.abort()
            elif transport is not None and hasattr(transport, "close"):
                transport.close()
        except Exception:
            pass

    async def close_session(
        self,
        ws: WebSocket,
        code: int = WS_CLOSE_REVOKED,
        reason: str = REASON_AUTH_REVOKED,
        timeout: float | None = None,
    ) -> None:
        """Revoke authorization and disconnect the socket within a bounded timeout."""
        self.revoke_session(ws)
        t = timeout if timeout is not None else self.close_timeout_seconds
        try:
            try:
                await asyncio.wait_for(ws.close(code=code, reason=reason), timeout=t)
            except asyncio.TimeoutError:
                logger.warning("Timed out after %ss closing booth session (code=%d, reason=%s)", t, code, reason)
                self._force_abort_transport(ws)
            except (WebSocketDisconnect, RuntimeError, OSError):
                # Already closing or the peer vanished; revocation above still holds.
                logger.debug("Booth session closed before revocation could notify it", exc_info=True)
            except Exception:
                logger.exception("Unexpected error closing booth session")
                self._force_abort_transport(ws)
        finally:
            self.remove(ws)

    def revoke_user_sessions(self, user_id: int, booth_id: str | None = None) -> list[WebSocket]:
        """Revoke authorization for all active sessions of a user (optionally scoped to a booth)."""
        # Bump first and unconditionally: a socket may not be registered yet,
        # and ws_booth compares this counter around manager.add().
        if booth_id is None:
            self._bump_user_epoch(user_id)
            self._allowed_cache.clear()
        else:
            self._bump_booth_epoch(user_id, booth_id)
            self._allowed_cache.pop(booth_id, None)

        revoked: list[WebSocket] = []
        for room_sockets in list(self._rooms.values()):
            for ws in list(room_sockets):
                session = self._sessions.get(id(ws))
                if session is not None and session.user_id == user_id:
                    if booth_id is None or session.booth_id == booth_id:
                        session.granted_role = None
                        session.revoked = True
                        revoked.append(ws)
        return revoked

    async def close_user_sessions(
        self,
        user_id: int,
        booth_id: str | None = None,
        code: int = WS_CLOSE_REVOKED,
        reason: str = REASON_AUTH_REVOKED,
    ) -> None:
        """Revoke authorization and concurrently disconnect all matching active sessions of a user."""
        revoked_sockets = self.revoke_user_sessions(user_id, booth_id)
        if not revoked_sockets:
            return
        tasks = [self.close_session(ws, code=code, reason=reason) for ws in revoked_sockets]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def close_booth_sessions(
        self,
        booth_id: str,
        code: int = WS_CLOSE_REVOKED,
        reason: str = REASON_MEMBERSHIP_REMOVED,
    ) -> None:
        """Revoke authorization and disconnect all active sockets in a booth (e.g. booth deletion)."""
        self._allowed_cache.pop(booth_id, None)
        room_sockets = list(self._rooms.get(booth_id, set()))
        if not room_sockets:
            return
        for ws in room_sockets:
            self.revoke_session(ws)
        tasks = [self.close_session(ws, code=code, reason=reason) for ws in room_sockets]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def broadcast(self, booth_id: str, message: dict) -> None:
        """Send *message* to booth members authorized for *booth_id*.

        Sockets are added to the room at WebSocket-connect time so late
        joiners observe membership changes, but delivery is limited to
        sockets whose session carries a supported role granted for this booth.
        A socket with no booth role (unauthenticated, holding only another
        booth's invitation, an unsupported role, or revoked) stays connected
        yet receives nothing.
        """
        room_snapshot = list(self._rooms.get(booth_id, set()))
        if not room_snapshot:
            return

        candidates: list[tuple[WebSocket, Session]] = []
        user_ids_to_check: set[int] = set()
        tokens_to_check: set[str | int] = set()

        for ws in room_snapshot:
            session = self._sessions.get(id(ws))
            if session is None or session.revoked or session.granted_role not in ALL_ROLES:
                continue
            candidates.append((ws, session))
            if session.user_id is not None:
                user_ids_to_check.add(session.user_id)
            elif session.invite_token_id is not None:
                tokens_to_check.add(session.invite_token_id)

        if not candidates:
            return

        # Pull layer: recheck current membership using one batched DB query
        allowed_user_ids: set[int] | None = None
        user_check_failed = False
        if self.membership_checker is not None and user_ids_to_check:
            now_mono = time.monotonic()
            cached = self._allowed_cache.get(booth_id)
            if (
                cached is not None
                and (now_mono - cached[0] < self.cache_ttl_seconds)
                and user_ids_to_check.issubset(cached[1])
            ):
                allowed_user_ids = cached[1]
            else:
                try:
                    res = await self.membership_checker(booth_id, user_ids_to_check)
                    if isinstance(res, dict):
                        allowed_user_ids = {uid for uid, r in res.items() if r in ALL_ROLES and r is not None}
                    else:
                        allowed_user_ids = set(res)
                    self._allowed_cache[booth_id] = (now_mono, allowed_user_ids)
                except Exception:
                    # Withhold this message but keep the sockets: a database blip must not
                    # disconnect every interpreter in the booth. Removals are pushed directly.
                    logger.exception("Membership check failed for booth %s; withholding message", booth_id)
                    user_check_failed = True

        # Pull layer: recheck current invite tokens using one batched DB query
        allowed_token_ids: set[str | int] | None = None
        token_check_failed = False
        if self.token_checker is not None and tokens_to_check:
            now_mono = time.monotonic()
            cached_tokens = self._token_allowed_cache.get(booth_id)
            if (
                cached_tokens is not None
                and (now_mono - cached_tokens[0] < self.cache_ttl_seconds)
                and tokens_to_check.issubset(cached_tokens[1])
            ):
                allowed_token_ids = cached_tokens[1]
            else:
                try:
                    res = await self.token_checker(booth_id, tokens_to_check)
                    allowed_token_ids = set(res)
                    self._token_allowed_cache[booth_id] = (now_mono, allowed_token_ids)
                except Exception:
                    logger.exception("Token check failed for booth %s; withholding message", booth_id)
                    token_check_failed = True

        valid_recipients: list[WebSocket] = []
        stale_user_sockets: list[WebSocket] = []
        stale_token_sockets: list[WebSocket] = []

        allowed_token_strs = {str(t) for t in allowed_token_ids} if allowed_token_ids is not None else None

        for ws, session in candidates:
            if (session.user_id is not None and user_check_failed) or (
                session.user_id is None and session.invite_token_id is not None and token_check_failed
            ):
                continue
            if session.user_id is not None and allowed_user_ids is not None:
                if session.user_id not in allowed_user_ids:
                    session.granted_role = None
                    session.revoked = True
                    stale_user_sockets.append(ws)
                    continue
            elif session.invite_token_id is not None and allowed_token_strs is not None:
                if str(session.invite_token_id) not in allowed_token_strs:
                    session.granted_role = None
                    session.revoked = True
                    stale_token_sockets.append(ws)
                    continue
            valid_recipients.append(ws)

        if stale_user_sockets:
            for ws in stale_user_sockets:
                self.revoke_session(ws)

            async def _close_stale_users(sockets: list[WebSocket]) -> None:
                await asyncio.gather(
                    *[
                        self.close_session(ws, code=WS_CLOSE_REVOKED, reason=REASON_MEMBERSHIP_REMOVED)
                        for ws in sockets
                    ],
                    return_exceptions=True,
                )

            asyncio.create_task(_close_stale_users(stale_user_sockets))

        if stale_token_sockets:
            for ws in stale_token_sockets:
                self.revoke_session(ws)

            async def _close_stale_tokens(sockets: list[WebSocket]) -> None:
                await asyncio.gather(
                    *[
                        self.close_session(ws, code=WS_CLOSE_REVOKED, reason=REASON_INVITE_REVOKED)
                        for ws in sockets
                    ],
                    return_exceptions=True,
                )

            asyncio.create_task(_close_stale_tokens(stale_token_sockets))

        if not valid_recipients:
            return

        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in valid_recipients:
            try:
                await ws.send_text(payload)
            except (WebSocketDisconnect, RuntimeError, OSError):
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


# The only booth fields public listeners receive. The full booth state also
# carries participants and the interpreters' private chat, which must stay
# inside the booth.
_LISTENER_STATE_KEYS = ("booth_id", "event_slug", "room_id", "language_code", "broadcast_unlocked", "ingest_status")


def listener_booth_state(state: dict) -> dict:
    """Return the listener-safe subset of a booth state dict."""
    return {key: state.get(key) for key in _LISTENER_STATE_KEYS}


def _current_listener_state(booth_id: str, fallback: dict | None = None) -> dict | None:
    """Listener-safe status read from the registry now, or from ``fallback`` if the booth is gone."""
    booth = booths.get_booth_sync(booth_id)
    if booth is not None:
        return listener_booth_state(booth.as_public_dict())
    return listener_booth_state(fallback) if fallback is not None else None


@dataclass
class _StateLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Operations holding or waiting on ``lock``.
    users: int = 0


class ListenerConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}
        # Last listener state sent per booth, so updates that only touch
        # interpreter-side fields (mic, chat, handoff) don't fan out to every listener.
        self._last_state: dict[str, dict] = {}
        # Serializes status sends per booth. Combined with reading the registry
        # inside the lock, overlapping updates can't deliver an older status last.
        # Entries only live while an operation holds or waits on them, so arbitrary
        # booth IDs from the captions endpoint don't accumulate.
        self._state_locks: dict[str, _StateLock] = {}
        self._anon_rooms: dict[str, set[WebSocket]] = {}

    def add(self, ws: WebSocket, booth_id: str, *, is_anonymous: bool = False) -> None:
        self._rooms.setdefault(booth_id, set()).add(ws)
        if is_anonymous:
            self._anon_rooms.setdefault(booth_id, set()).add(ws)

    def count_anonymous(self, booth_id: str) -> int:
        return len(self._anon_rooms.get(booth_id, set()))

    def try_reserve_anonymous(self, ws: WebSocket, booth_id: str, max_anon: int) -> bool:
        if max_anon <= 0:
            return False
        anon_set = self._anon_rooms.get(booth_id)
        if anon_set is not None and len(anon_set) >= max_anon:
            return False
        if anon_set is None:
            anon_set = set()
            self._anon_rooms[booth_id] = anon_set
        anon_set.add(ws)
        return True

    def cancel_reservation(self, ws: WebSocket, booth_id: str) -> None:
        anon_set = self._anon_rooms.get(booth_id, set())
        anon_set.discard(ws)
        if not anon_set:
            self._anon_rooms.pop(booth_id, None)

    def has_listeners(self, booth_id: str) -> bool:
        return bool(self._rooms.get(booth_id, set()))

    def remove(self, ws: WebSocket, booth_id: str) -> None:
        room = self._rooms.get(booth_id, set())
        room.discard(ws)
        if not room:
            self._rooms.pop(booth_id, None)
            self._last_state.pop(booth_id, None)

        anon_set = self._anon_rooms.get(booth_id, set())
        anon_set.discard(ws)
        if not anon_set:
            self._anon_rooms.pop(booth_id, None)

    @asynccontextmanager
    async def _state_lock(self, booth_id: str) -> AsyncIterator[None]:
        # No await between lookup and increment, so every operation that overlaps
        # an existing holder or waiter for this booth shares the same lock.
        entry = self._state_locks.setdefault(booth_id, _StateLock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._state_locks[booth_id]

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

    async def broadcast_booth_state(self, booth_id: str, state: dict) -> None:
        """Send listeners the booth's live/locked status if it changed since the last send.

        ``state`` is only used if the booth has left the registry; otherwise the
        newest status is read under the booth's lock.
        """
        async with self._state_lock(booth_id):
            if not self.has_listeners(booth_id):
                return
            listener_state = _current_listener_state(booth_id, state)
            if self._last_state.get(booth_id) == listener_state:
                return
            self._last_state[booth_id] = listener_state
            await self.broadcast(booth_id, {"type": "booth:state", "state": listener_state})

    async def send_booth_state(self, ws: WebSocket, booth_id: str) -> None:
        """Send one newly connected listener the booth's current status, if the booth exists."""
        async with self._state_lock(booth_id):
            listener_state = _current_listener_state(booth_id)
            if listener_state is not None:
                await ws.send_text(json.dumps({"type": "booth:state", "state": listener_state}))


logger = logging.getLogger(__name__)


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


async def broadcast_booth_state(booth_id: str, state: dict) -> None:
    """Send the full state to the booth and the listener-safe status to its listeners."""
    await manager.broadcast(booth_id, {"type": "booth:state", "state": state})
    await listener_manager.broadcast_booth_state(booth_id, state)


async def broadcast_transcription(booth_id: str, payload: str | dict):
    if isinstance(payload, str):
        # Legacy support and error handling
        text = payload
        if text.startswith("[Server overloaded") or text.startswith("[Transcription provider failed"):
            await manager.broadcast(booth_id, {"type": "booth:error", "message": text})
            booth = booths.get_booth_sync(booth_id)
            if booth:
                booth.ingest_status = "overloaded"
                await broadcast_booth_state(booth_id, booth.as_public_dict())
        else:
            msg = {"type": "caption", "status": "final", "text": text}
            await listener_manager.broadcast(booth_id, msg)
    else:
        # Aggregator payload
        await listener_manager.broadcast(booth_id, payload)


async def _handle_join(ws: WebSocket, session: Session, data: dict) -> None:
    """Handle a client joining a booth session.

    Validates client role and language parameters, initializes participant state,
    and broadcasts state updates to listeners.
    """
    display_name = data.get("display_name", "Interpreter")
    role = data.get("role", INTERPRETER)
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
            # Only this socket's own record may be re-claimed by ID.
            reclaim=participant_id is not None and participant_id == session.participant_id,
            joined_by=session.session_key,
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
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
    await broadcast_booth_state(session.booth_id, state)

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


async def _handle_leave(session: Session) -> None:
    if not session.participant_id:
        return
    state = await booths.leave_participant(
        session.booth_id, session.participant_id, session.language, session.channel_id
    )
    session.participant_id = None
    await broadcast_booth_state(session.booth_id, state)


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
    await broadcast_booth_state(session.booth_id, state)


async def _handle_set_active(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Join the booth first."}))
        return
    target_id = data.get("target_id")
    if not target_id:
        await ws.send_text(json.dumps({"type": "booth:error", "message": "Missing target_id."}))
        return
    try:
        state = await booths.set_active_interpreter(
            session.booth_id, session.participant_id, target_id, session.language, session.channel_id
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({"type": "booth:error", "message": str(exc)}))
        return
    await broadcast_booth_state(session.booth_id, state)


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
    await broadcast_booth_state(session.booth_id, state)


async def _handle_request_publish_ticket(ws: WebSocket, session: Session, data: dict) -> None:
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


async def _handle_set_broadcast_unlocked(ws: WebSocket, session: Session, data: dict) -> None:
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
    try:
        state = await booths.set_broadcast_unlocked(session.booth_id, unlocked, session.language, session.channel_id)
        # Locking the booth must immediately stop everything tied to it: the
        # transcription worker (which pulls audio independently of any browser)
        # is torn down here so captions stop even if the interpreter's client is
        # unresponsive. Publisher mic/ingest state is already reset inside
        # set_broadcast_unlocked.
        if not unlocked:
            try:
                from portal.transcription.worker import stop_transcription_worker

                await stop_transcription_worker(session.booth_id)
            except Exception as e:
                logging.getLogger(__name__).warning(f"Failed to stop transcription on lock: {e}")
        await broadcast_booth_state(session.booth_id, state)
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
    await broadcast_booth_state(session.booth_id, state)


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
    await broadcast_booth_state(session.booth_id, state)


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
    await broadcast_booth_state(session.booth_id, state)
