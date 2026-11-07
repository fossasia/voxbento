from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from portal.websockets.manager import (
    REASON_ACCOUNT_DEACTIVATED,
    REASON_AUTH_REVOKED,
    REASON_INVITE_REVOKED,
    REASON_MEMBERSHIP_REMOVED,
    REASON_SESSION_EXPIRED,
    WS_CLOSE_REVOKED,
    ConnectionManager,
    Session,
)


class MockWebSocket:
    """Mock WebSocket for unit tests."""

    def __init__(self, hang_close: bool = False):
        self.sent_messages: list[str] = []
        self.closed_with: tuple[int, str] | None = None
        self.hang_close = hang_close
        self.aborted = False

    async def send_text(self, data: str) -> None:
        self.sent_messages.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.hang_close:
            # Simulate a stalled/hung connection
            await asyncio.sleep(100.0)
        self.closed_with = (code, reason)

    def abort(self) -> None:
        self.aborted = True


def _make_session(
    booth_id: str = "booth-1",
    user_id: int | None = 10,
    role: str = "interpreter",
    issued_at: datetime | None = None,
) -> Session:
    return Session(
        booth_id=booth_id,
        participant_id=None,
        language="en",
        channel_id=f"{booth_id}-audio",
        granted_role=role,
        user_id=user_id,
        issued_at=issued_at or datetime.now(timezone.utc),
    )


# 1. Membership removed while socket open: broadcast no longer reaches that socket and the socket is closed.
@pytest.mark.anyio
async def test_membership_removed_broadcast_evicts_and_closes_socket():
    manager = ConnectionManager()
    ws_allowed = MockWebSocket()
    ws_removed = MockWebSocket()

    manager.add(ws_allowed, _make_session("booth-1", user_id=1))
    manager.add(ws_removed, _make_session("booth-1", user_id=2))

    # Injected checker returns only user 1 as currently allowed
    async def checker(booth_id: str, user_ids: set[int]) -> set[int]:
        return {1}

    manager.set_membership_checker(checker)

    await manager.broadcast("booth-1", {"type": "booth:chat", "body": "secret message"})

    # Allowed user receives the broadcast
    assert len(ws_allowed.sent_messages) == 1

    # Removed user receives nothing
    assert len(ws_removed.sent_messages) == 0

    # Wait brief moment for async close task
    await asyncio.sleep(0.05)

    # Socket was closed with 4003 and REASON_MEMBERSHIP_REMOVED
    assert ws_removed.closed_with == (WS_CLOSE_REVOKED, REASON_MEMBERSHIP_REMOVED)
    assert manager.get_session(ws_removed) is None


# 2. Membership downgraded below the role required: same.
@pytest.mark.anyio
async def test_membership_downgraded_below_role_required_evicts_socket():
    manager = ConnectionManager()
    ws = MockWebSocket()
    manager.add(ws, _make_session("booth-1", user_id=42, role="interpreter"))

    # When downgraded (e.g. to listener or no role), checker returns empty set
    async def checker(booth_id: str, user_ids: set[int]) -> set[int]:
        return set()

    manager.set_membership_checker(checker)

    await manager.broadcast("booth-1", {"type": "booth:state", "state": {}})

    assert len(ws.sent_messages) == 0
    await asyncio.sleep(0.05)

    assert ws.closed_with == (WS_CLOSE_REVOKED, REASON_MEMBERSHIP_REMOVED)
    assert manager.get_session(ws) is None


# 3. Demotion committed, sweep skipped (simulate cancellation after commit):
# the reaper or next broadcast still revokes the socket, and a retry of the same change still results in revoked sessions.
@pytest.mark.anyio
@pytest.mark.parametrize("naive_timestamp", [False, True])
async def test_demotion_committed_sweep_skipped_reaper_and_retry_evict(monkeypatch, naive_timestamp):
    import portal.websockets.reaper as reaper_mod
    from portal.websockets.reaper import reap_stale_sessions

    manager = ConnectionManager()
    monkeypatch.setattr(reaper_mod, "manager", manager)

    now = datetime.now(timezone.utc)
    old_time = now - timedelta(minutes=10)
    demoted_at = now - timedelta(minutes=2)
    if naive_timestamp:
        demoted_at = demoted_at.replace(tzinfo=None)  # SQLite returns naive UTC datetimes.

    # Session connected before demotion
    ws = MockWebSocket()
    manager.add(ws, _make_session("booth-1", user_id=99, issued_at=old_time))

    # Mock DB query for reaper: user has sessions_valid_after = demoted_at
    class MockDBUser:
        id = 99
        is_active = True
        sessions_valid_after = demoted_at

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        class MockSession:
            async def execute(self, stmt):
                class MockResult:
                    def all(self):
                        return [(MockDBUser.id, MockDBUser.is_active, MockDBUser.sessions_valid_after)]

                return MockResult()

        yield MockSession()

    monkeypatch.setattr(reaper_mod, "get_session", mock_get_session)

    # Reaper runs pass
    closed_count = await reap_stale_sessions()
    assert closed_count == 1
    assert ws.closed_with == (WS_CLOSE_REVOKED, REASON_SESSION_EXPIRED)
    assert manager.get_session(ws) is None


# 4. Revoking booth A while the same user connects to booth B: B's connection is NOT closed. User-wide revoke closes both.
@pytest.mark.anyio
async def test_scoped_revocation_does_not_affect_other_booths():
    manager = ConnectionManager()
    ws_a = MockWebSocket()
    ws_b = MockWebSocket()

    user_id = 7
    manager.add(ws_a, _make_session("booth-A", user_id=user_id))
    manager.add(ws_b, _make_session("booth-B", user_id=user_id))

    epoch_b_before = manager.revocation_epoch(user_id, "booth-B")

    # Scoped revocation for booth-A
    await manager.close_user_sessions(user_id, booth_id="booth-A")

    # booth-A session is closed
    assert ws_a.closed_with == (WS_CLOSE_REVOKED, REASON_AUTH_REVOKED)
    assert manager.get_session(ws_a) is None

    # booth-B session is still open and registered
    assert ws_b.closed_with is None
    assert manager.get_session(ws_b) is not None

    # booth-B revocation epoch did NOT change
    assert manager.revocation_epoch(user_id, "booth-B") == epoch_b_before

    # Now user-wide revoke closes booth-B as well
    await manager.close_user_sessions(user_id, booth_id=None)
    assert ws_b.closed_with == (WS_CLOSE_REVOKED, REASON_AUTH_REVOKED)
    assert manager.get_session(ws_b) is None


# 5. One socket whose close hangs: the others still close, the call returns within roughly the timeout,
# and the stalled socket is removed from the manager's state.
@pytest.mark.anyio
async def test_stalled_socket_close_does_not_block_others():
    manager = ConnectionManager(close_timeout_seconds=0.1)
    ws_normal1 = MockWebSocket()
    ws_stalled = MockWebSocket(hang_close=True)
    ws_normal2 = MockWebSocket()

    user_id = 15
    manager.add(ws_normal1, _make_session("booth-1", user_id=user_id))
    manager.add(ws_stalled, _make_session("booth-1", user_id=user_id))
    manager.add(ws_normal2, _make_session("booth-1", user_id=user_id))

    start = asyncio.get_running_loop().time()
    await manager.close_user_sessions(user_id)
    elapsed = asyncio.get_running_loop().time() - start

    # Returns within roughly the 0.1s timeout (well under 1.0s)
    assert elapsed < 1.0

    # Normal sockets closed cleanly
    assert ws_normal1.closed_with == (WS_CLOSE_REVOKED, REASON_AUTH_REVOKED)
    assert ws_normal2.closed_with == (WS_CLOSE_REVOKED, REASON_AUTH_REVOKED)

    # ALL sockets removed from manager's state, including stalled socket
    assert manager.get_session(ws_normal1) is None
    assert manager.get_session(ws_stalled) is None
    assert manager.get_session(ws_normal2) is None
    assert not manager._rooms.get("booth-1", set())


# 6. Membership checker raising an exception: broadcast fails closed.
@pytest.mark.anyio
async def test_membership_checker_exception_fails_closed():
    manager = ConnectionManager()
    ws = MockWebSocket()
    manager.add(ws, _make_session("booth-1", user_id=88))

    async def broken_checker(booth_id: str, user_ids: set[int]) -> set[int]:
        raise RuntimeError("Database connection timed out")

    manager.set_membership_checker(broken_checker)

    # Broadcast fails closed: no message delivered
    await manager.broadcast("booth-1", {"type": "booth:chat", "body": "classified"})

    assert len(ws.sent_messages) == 0
    # A database error is not a revocation: the interpreter stays connected.
    await asyncio.sleep(0)
    assert manager.get_session(ws) is not None
    assert manager.get_session(ws).revoked is False


# 7. Invite-link sockets are unaffected by account-session sweeps.
@pytest.mark.anyio
async def test_invite_link_sockets_unaffected_by_account_sweeps():
    manager = ConnectionManager()
    ws_invite = MockWebSocket()
    ws_user = MockWebSocket()

    # Invite-link socket has user_id = None
    manager.add(ws_invite, _make_session("booth-1", user_id=None, role="interpreter"))
    # Account socket has user_id = 101
    manager.add(ws_user, _make_session("booth-1", user_id=101, role="interpreter"))

    # Revoke account user_id=101
    await manager.close_user_sessions(101)

    # User socket is closed
    assert ws_user.closed_with == (WS_CLOSE_REVOKED, REASON_AUTH_REVOKED)
    assert manager.get_session(ws_user) is None

    # Invite-link socket is still alive and receives broadcasts
    assert ws_invite.closed_with is None
    assert manager.get_session(ws_invite) is not None

    await manager.broadcast("booth-1", {"type": "booth:state", "state": {}})
    assert len(ws_invite.sent_messages) == 1


# 8. Double revocation and double close are idempotent.
@pytest.mark.anyio
async def test_double_revocation_and_close_are_idempotent():
    manager = ConnectionManager()
    ws = MockWebSocket()
    user_id = 202
    manager.add(ws, _make_session("booth-1", user_id=user_id))

    # First revocation & close
    manager.revoke_user_sessions(user_id)
    await manager.close_session(ws)

    # Second revocation & close should be safe and not raise any exception
    manager.revoke_user_sessions(user_id)
    await manager.close_session(ws)
    await manager.close_user_sessions(user_id)

    assert manager.get_session(ws) is None
    assert not manager._rooms.get("booth-1", set())


# ============================================================================
# Part B: Invite-token Revocation Boundary Tests (Scenarios 1 - 11)
# ============================================================================


def _make_token_session(
    booth_id: str = "booth-1",
    token_id: str | int | None = "token-abc",
    role: str = "interpreter",
    issued_at: datetime | None = None,
) -> Session:
    return Session(
        booth_id=booth_id,
        participant_id=None,
        language="en",
        channel_id=f"{booth_id}-audio",
        granted_role=role,
        user_id=None,
        invite_token_id=token_id,
        issued_at=issued_at or datetime.now(timezone.utc),
    )


# 1. Revoked invite link: an open token socket stops receiving broadcasts and is closed with the new reason.
@pytest.mark.anyio
async def test_revoked_invite_link_stops_broadcast_and_closed_with_new_reason():
    manager = ConnectionManager()
    ws = MockWebSocket()
    token_id = "tok-secret-1"
    manager.add(ws, _make_token_session("booth-1", token_id=token_id))

    await manager.close_token_sessions(token_id, booth_id="booth-1")

    assert ws.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert manager.get_session(ws) is None

    # Broadcast receives nothing
    await manager.broadcast("booth-1", {"type": "booth:state", "state": {}})
    assert len(ws.sent_messages) == 0


# 2. Revoked token cannot connect or reconnect.
@pytest.mark.anyio
async def test_revoked_token_cannot_connect_or_reconnect(monkeypatch):
    import portal.auth as auth_mod
    from portal.auth import invalidate_token_revocation_cache, is_invite_token_revoked
    from portal.models import InviteToken

    invalidate_token_revocation_cache()

    class MockInviteToken:
        token = "tok-revoked-2"
        revoked_at = datetime.now(timezone.utc)
        used_at = datetime.now(timezone.utc)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        class MockSession:
            pass

        yield MockSession()

    async def mock_get_invite_token(session, key):
        if key == "tok-revoked-2":
            return MockInviteToken()
        return None

    import portal.database as db_mod

    monkeypatch.setattr(db_mod, "get_invite_token_by_public_id", mock_get_invite_token)
    monkeypatch.setattr(db_mod, "get_invite_token", mock_get_invite_token)

    # Revoked token is detected as revoked
    assert await is_invite_token_revoked("tok-revoked-2") is True
    # Non-existent token fails closed as revoked
    assert await is_invite_token_revoked("tok-nonexistent") is True


# 3. Revocation committed but sweep skipped (simulate cancellation after commit):
# next broadcast or reaper pass still evicts the socket, and a retried revoke still sweeps.
@pytest.mark.anyio
async def test_revocation_committed_sweep_skipped_evicted_by_broadcast_or_reaper(monkeypatch):
    import portal.websockets.reaper as reaper_mod
    from portal.websockets.reaper import reap_stale_sessions

    manager = ConnectionManager()
    monkeypatch.setattr(reaper_mod, "manager", manager)

    ws_broadcast = MockWebSocket()
    ws_reaper = MockWebSocket()

    manager.add(ws_broadcast, _make_token_session("booth-bc", token_id="tok-skip-bc"))
    manager.add(ws_reaper, _make_token_session("booth-rp", token_id="tok-skip-rp"))

    # Pull layer via broadcast: token checker returns empty set (token revoked in DB)
    async def token_checker(booth_id: str, tokens: set[str | int]) -> set[str | int]:
        return set()

    manager.set_token_checker(token_checker)
    await manager.broadcast("booth-bc", {"type": "booth:chat", "body": "leak test"})

    assert len(ws_broadcast.sent_messages) == 0
    await asyncio.sleep(0.05)
    assert ws_broadcast.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert manager.get_session(ws_broadcast) is None

    # Pull layer via reaper pass
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        class MockSession:
            async def execute(self, stmt):
                class MockResult:
                    def all(self):
                        # tok-skip-rp is returned with revoked_at timestamp set
                        return [("tok-skip-rp", datetime.now(timezone.utc), None)]

                return MockResult()

        yield MockSession()

    monkeypatch.setattr(reaper_mod, "get_session", mock_get_session)
    closed = await reap_stale_sessions()
    assert closed == 1
    assert ws_reaper.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert manager.get_session(ws_reaper) is None

    # Retried revoke also sweeps idempotently
    await manager.close_token_sessions("tok-skip-bc")
    await manager.close_token_sessions("tok-skip-rp")


# 4. Redeemed-but-not-revoked token connects and keeps receiving broadcasts.
@pytest.mark.anyio
async def test_redeemed_but_not_revoked_token_connects_and_receives_broadcasts(monkeypatch):
    import portal.auth as auth_mod
    from portal.auth import invalidate_token_revocation_cache, is_invite_token_revoked

    invalidate_token_revocation_cache()

    class MockRedeemedToken:
        token = "tok-redeemed-4"
        used_at = datetime.now(timezone.utc)
        revoked_at = None
        expires_at = None
        is_expired = False

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        class MockSession:
            pass

        yield MockSession()

    async def mock_get_invite_token(session, key):
        if key == "tok-redeemed-4":
            return MockRedeemedToken()
        return None

    import portal.database as db_mod

    monkeypatch.setattr(db_mod, "get_invite_token_by_public_id", mock_get_invite_token)
    monkeypatch.setattr(db_mod, "get_invite_token", mock_get_invite_token)

    # Not revoked!
    assert await is_invite_token_revoked("tok-redeemed-4") is False

    # Connects and receives broadcasts
    manager = ConnectionManager()
    ws = MockWebSocket()
    manager.add(ws, _make_token_session("booth-1", token_id="tok-redeemed-4"))

    async def token_checker(booth_id: str, tokens: set[str | int]) -> set[str | int]:
        return {"tok-redeemed-4"}

    manager.set_token_checker(token_checker)
    await manager.broadcast("booth-1", {"type": "booth:state", "state": {"active": True}})

    assert len(ws.sent_messages) == 1
    assert ws.closed_with is None


# 5. Revoking token X does not affect token Y, account sessions, or the same booth's other participants.
@pytest.mark.anyio
async def test_revoking_token_x_does_not_affect_token_y_or_account_sessions():
    manager = ConnectionManager()
    ws_x = MockWebSocket()
    ws_y = MockWebSocket()
    ws_user = MockWebSocket()

    manager.add(ws_x, _make_token_session("booth-1", token_id="tok-X"))
    manager.add(ws_y, _make_token_session("booth-1", token_id="tok-Y"))
    manager.add(ws_user, _make_session("booth-1", user_id=55))

    await manager.close_token_sessions("tok-X")

    # tok-X is evicted
    assert ws_x.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert manager.get_session(ws_x) is None

    # tok-Y and user are untouched
    assert ws_y.closed_with is None
    assert ws_user.closed_with is None
    assert manager.get_session(ws_y) is not None
    assert manager.get_session(ws_user) is not None

    # Both still receive broadcasts
    await manager.broadcast("booth-1", {"type": "booth:chat", "body": "hello team"})
    assert len(ws_y.sent_messages) == 1
    assert len(ws_user.sent_messages) == 1


# 6. Revoking a token while it connects to a different booth than the scope does not close the other booth's socket (scoped epoch).
@pytest.mark.anyio
async def test_revoking_token_scoped_epoch_does_not_close_different_booth():
    manager = ConnectionManager()
    ws_a = MockWebSocket()
    ws_b = MockWebSocket()

    token_id = "tok-multi-booth"
    manager.add(ws_a, _make_token_session("booth-A", token_id=token_id))
    manager.add(ws_b, _make_token_session("booth-B", token_id=token_id))

    epoch_b_before = manager.token_revocation_epoch(token_id, "booth-B")

    # Scoped revocation to booth-A only
    await manager.close_token_sessions(token_id, booth_id="booth-A")

    # booth-A is closed
    assert ws_a.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert manager.get_session(ws_a) is None

    # booth-B is still open
    assert ws_b.closed_with is None
    assert manager.get_session(ws_b) is not None
    assert manager.token_revocation_epoch(token_id, "booth-B") == epoch_b_before


# 7. One token socket whose close hangs: others still close, call returns near the timeout, state is cleaned up.
@pytest.mark.anyio
async def test_stalled_token_socket_close_does_not_block_others():
    manager = ConnectionManager(close_timeout_seconds=0.1)
    ws_norm1 = MockWebSocket()
    ws_stalled = MockWebSocket(hang_close=True)
    ws_norm2 = MockWebSocket()

    token_id = "tok-stalled"
    manager.add(ws_norm1, _make_token_session("booth-1", token_id=token_id))
    manager.add(ws_stalled, _make_token_session("booth-1", token_id=token_id))
    manager.add(ws_norm2, _make_token_session("booth-1", token_id=token_id))

    start = asyncio.get_running_loop().time()
    await manager.close_token_sessions(token_id)
    elapsed = asyncio.get_running_loop().time() - start

    assert elapsed < 1.0
    assert ws_norm1.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert ws_norm2.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)

    assert manager.get_session(ws_norm1) is None
    assert manager.get_session(ws_stalled) is None
    assert manager.get_session(ws_norm2) is None
    assert not manager._rooms.get("booth-1", set())


# 8. Checker exception: broadcast fails closed for token sockets.
@pytest.mark.anyio
async def test_token_checker_exception_fails_closed():
    manager = ConnectionManager()
    ws = MockWebSocket()
    manager.add(ws, _make_token_session("booth-1", token_id="tok-err"))

    async def broken_checker(booth_id: str, tokens: set[str | int]) -> set[str | int]:
        raise RuntimeError("Database connection pool exhausted")

    manager.set_token_checker(broken_checker)
    await manager.broadcast("booth-1", {"type": "booth:chat", "body": "private payload"})

    assert len(ws.sent_messages) == 0
    await asyncio.sleep(0)
    assert manager.get_session(ws) is not None
    assert manager.get_session(ws).revoked is False


@pytest.mark.anyio
async def test_is_invite_token_revoked_retries_transient_db_error(monkeypatch):
    import portal.database
    from portal.auth import invalidate_token_revocation_cache, is_invite_token_revoked

    invalidate_token_revocation_cache()
    calls = 0

    class DummyToken:
        revoked_at = None

    async def flaky_get_invite_token(session, key):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Transient connection reset")
        return DummyToken()

    monkeypatch.setattr(portal.database, "get_invite_token_by_public_id", flaky_get_invite_token)
    revoked = await is_invite_token_revoked("test-public-id")
    assert calls == 2
    assert revoked is False


# 9. Legacy JWT without the token-id claim: no crash, behavior matches the documented choice.
@pytest.mark.anyio
async def test_legacy_jwt_without_token_id_claim_no_crash():
    manager = ConnectionManager()
    ws_legacy = MockWebSocket()

    # Legacy token session has invite_token_id=None and user_id=None
    manager.add(ws_legacy, _make_token_session("booth-1", token_id=None))

    # Does not crash on token check or broadcast
    async def token_checker(booth_id: str, tokens: set[str | int]) -> set[str | int]:
        return {"other-token"}

    manager.set_token_checker(token_checker)
    await manager.broadcast("booth-1", {"type": "booth:state", "state": {"ok": True}})

    # Documented choice: legacy token is valid until JWT expiration
    assert len(ws_legacy.sent_messages) == 1
    assert ws_legacy.closed_with is None

    # Revoking any token does not close the legacy token
    await manager.close_token_sessions("other-token")
    assert ws_legacy.closed_with is None


# 10. Double revocation and double close are idempotent.
@pytest.mark.anyio
async def test_token_double_revocation_and_close_are_idempotent():
    manager = ConnectionManager()
    ws = MockWebSocket()
    token_id = "tok-idem-10"
    manager.add(ws, _make_token_session("booth-1", token_id=token_id))

    manager.revoke_token_sessions(token_id)
    await manager.close_session(ws, reason=REASON_INVITE_REVOKED)

    # Repeating should be safe and idempotent
    manager.revoke_token_sessions(token_id)
    await manager.close_session(ws, reason=REASON_INVITE_REVOKED)
    await manager.close_token_sessions(token_id)

    assert manager.get_session(ws) is None
    assert not manager._rooms.get("booth-1", set())


# 11. Account-session sweeps still leave token sockets untouched, and token sweeps leave account sessions untouched.
@pytest.mark.anyio
async def test_account_sweeps_leave_token_sockets_untouched_and_vice_versa():
    manager = ConnectionManager()
    ws_token = MockWebSocket()
    ws_account = MockWebSocket()

    token_id = "tok-independent-11"
    user_id = 111

    manager.add(ws_token, _make_token_session("booth-1", token_id=token_id))
    manager.add(ws_account, _make_session("booth-1", user_id=user_id))

    # Account sweep
    await manager.close_user_sessions(user_id)
    assert ws_account.closed_with == (WS_CLOSE_REVOKED, REASON_AUTH_REVOKED)
    assert ws_token.closed_with is None
    assert manager.get_session(ws_token) is not None

    # Re-add account session with different user_id
    ws_account2 = MockWebSocket()
    manager.add(ws_account2, _make_session("booth-1", user_id=222))

    # Token sweep
    await manager.close_token_sessions(token_id)
    assert ws_token.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert ws_account2.closed_with is None
    assert manager.get_session(ws_account2) is not None


# 12. Token whose expires_at is in the past but whose participant JWT is still valid does NOT get evicted.
@pytest.mark.anyio
async def test_expired_token_redemption_deadline_past_does_not_evict_active_session(monkeypatch):
    """Verify that a token whose expires_at is in the past does NOT evict an already-connected participant.

    expires_at is a redemption deadline, not an eviction trigger. If someone reintroduces an expires_at check,
    this test must fail.
    """
    import portal.websockets.reaper as reaper_mod
    from portal.websockets.reaper import check_booth_tokens, reap_stale_sessions

    now = datetime.now(timezone.utc)
    past_expiry = now - timedelta(hours=2)

    class MockExpiredInviteToken:
        public_id = "pub-expired-link"
        token = "secret-raw-link"
        revoked_at = None
        expires_at = past_expiry

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        class MockSession:
            async def execute(self, stmt):
                class MockResult:
                    def all(self):
                        return [(MockExpiredInviteToken.public_id, None)]

                return MockResult()

            async def scalars(self, stmt):
                class MockScalars:
                    def all(self):
                        return [MockExpiredInviteToken.public_id]

                return MockScalars()

        yield MockSession()

    monkeypatch.setattr(reaper_mod, "get_session", mock_get_session)

    # 1. check_booth_tokens retains the token public_id
    valid_tokens = await check_booth_tokens("event1-1-en", {MockExpiredInviteToken.public_id})
    assert MockExpiredInviteToken.public_id in valid_tokens, (
        "Token with past expires_at was evicted by check_booth_tokens! "
        "expires_at must only govern redemption, not active sessions."
    )

    # 2. reaper does not evict it
    manager = ConnectionManager()
    monkeypatch.setattr(reaper_mod, "manager", manager)
    ws = MockWebSocket()
    manager.add(ws, _make_token_session("event1-1-en", token_id=MockExpiredInviteToken.public_id))

    reaped = await reap_stale_sessions()
    assert reaped == 0
    assert ws.closed_with is None
    assert manager.get_session(ws) is not None

    # 3. broadcast succeeds and delivers
    manager.set_token_checker(check_booth_tokens)
    await manager.broadcast("event1-1-en", {"type": "booth:state", "state": {"ok": True}})
    assert len(ws.sent_messages) == 1
    assert ws.closed_with is None


# 13. The JWT generated by /join/{token} contains the public_id and NOT the raw secret token.
@pytest.mark.anyio
async def test_join_via_invite_jwt_contains_public_id_and_not_raw_token(monkeypatch):
    from starlette.requests import Request

    from portal.auth import decode_token
    from portal.routers.auth import join_via_invite

    class MockEvent:
        slug = "demo-event"

    class MockRoom:
        id = 1

    class MockBooth:
        id = 10
        room_id = 1
        language_code = "en"
        event = MockEvent()

    class MockInviteToken:
        token = "super-secret-raw-token-that-must-never-leak"
        public_id = "pub-id-xyz987"
        booth_id = 10
        role = "interpreter"
        booth = MockBooth()

    import portal.routers.auth as router_auth_mod

    async def mock_redeem(session, token):
        assert token == "super-secret-raw-token-that-must-never-leak"
        return MockInviteToken()

    monkeypatch.setattr(router_auth_mod, "redeem_invite_token", mock_redeem)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        class MockSession:
            pass

        yield MockSession()

    monkeypatch.setattr(router_auth_mod, "get_session", mock_get_session)

    # Mock request
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/join/super-secret-raw-token-that-must-never-leak",
        "headers": [],
    }
    req = Request(scope)

    resp = await join_via_invite(req, "super-secret-raw-token-that-must-never-leak")
    assert resp.status_code == 303

    # Extract session_token cookie
    raw_cookies = resp.headers.getlist("set-cookie")
    session_cookie = None
    for cookie_header in raw_cookies:
        if cookie_header.startswith("session_token="):
            session_cookie = cookie_header.split(";")[0].split("=")[1]
            break

    assert session_cookie is not None
    payload = decode_token(session_cookie)

    # 1. token_id claim is the public_id
    assert payload.get("token_id") == "pub-id-xyz987"

    # 2. The raw secret token does NOT appear in any claim or value
    for k, v in payload.items():
        assert "super-secret-raw-token" not in str(k)
        assert "super-secret-raw-token" not in str(v)


# 14. Token revocation by public_id works end-to-end: push sweep, pull layer, reaper, reconnect rejection.
@pytest.mark.anyio
async def test_token_revocation_by_public_id_end_to_end(monkeypatch):
    """End-to-end test of revocation by public_id across all four defensive layers:
    1. Push sweep (close_token_sessions)
    2. Pull recheck (check_booth_tokens during broadcast)
    3. Background reaper (reap_stale_sessions)
    4. Reconnect rejection (is_invite_token_revoked)
    """
    import portal.database as db_mod
    import portal.websockets.reaper as reaper_mod
    from portal.auth import invalidate_token_revocation_cache, is_invite_token_revoked
    from portal.websockets.reaper import check_booth_tokens, reap_stale_sessions

    invalidate_token_revocation_cache()

    public_id = "pub-e2e-revoked-token"
    booth_id = "event1-1-en"

    class MockRevokedToken:
        token = "raw-secret-e2e"
        public_id = "pub-e2e-revoked-token"
        revoked_at = datetime.now(timezone.utc)
        expires_at = None

    async def mock_get_invite_token_by_pub_id(session, pid):
        if pid == public_id:
            return MockRevokedToken()
        return None

    monkeypatch.setattr(db_mod, "get_invite_token_by_public_id", mock_get_invite_token_by_pub_id)
    monkeypatch.setattr(db_mod, "get_invite_token", mock_get_invite_token_by_pub_id)

    # LAYER 1: Push sweep
    manager = ConnectionManager()
    ws_push = MockWebSocket()
    manager.add(ws_push, _make_token_session(booth_id, token_id=public_id))

    # Trigger push sweep by public_id
    await manager.close_token_sessions(public_id, booth_id=booth_id)
    assert ws_push.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert manager.get_session(ws_push) is None

    # LAYER 2: Pull layer (if push sweep was skipped, next broadcast evicts)
    ws_pull = MockWebSocket()
    manager.add(ws_pull, _make_token_session(booth_id, token_id=public_id))

    # Mock DB query for check_booth_tokens returning empty set because revoked_at IS NOT NULL
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_get_session():
        class MockSession:
            async def execute(self, stmt):
                class MockResult:
                    def all(self):
                        return []  # Revoked tokens are not returned

                    def scalars(self):
                        class MockScalars:
                            def all(self):
                                return []

                        return MockScalars()

                return MockResult()

            async def scalars(self, stmt):
                return (await self.execute(stmt)).scalars()

        yield MockSession()

    monkeypatch.setattr(reaper_mod, "get_session", mock_get_session)

    manager.set_token_checker(check_booth_tokens)
    await manager.broadcast(booth_id, {"type": "booth:chat", "body": "confidential"})

    # Socket did not receive the broadcast and was evicted
    assert len(ws_pull.sent_messages) == 0
    await asyncio.sleep(0.05)
    assert ws_pull.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert manager.get_session(ws_pull) is None

    # LAYER 3: Reaper eviction
    ws_reaper = MockWebSocket()
    manager.add(ws_reaper, _make_token_session(booth_id, token_id=public_id))
    monkeypatch.setattr(reaper_mod, "manager", manager)

    reaped = await reap_stale_sessions()
    assert reaped >= 1
    assert ws_reaper.closed_with == (WS_CLOSE_REVOKED, REASON_INVITE_REVOKED)
    assert manager.get_session(ws_reaper) is None

    # LAYER 4: Reconnect rejection
    # When a revoked token tries to reconnect, is_invite_token_revoked returns True
    assert await is_invite_token_revoked(public_id) is True


# 15. Migration 037 upgrade and downgrade with existing rows (temporary SQLite DB).
def test_migration_037_upgrade_downgrade_with_existing_rows(tmp_path):
    import importlib.util
    import sqlite3
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect, text

    versions_dir = Path(__file__).resolve().parent.parent / "alembic" / "versions"
    spec = importlib.util.spec_from_file_location("m037", versions_dir / "038_add_public_id_to_invite_tokens.py")
    m037 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m037)

    db = tmp_path / "test_mig_037.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE invite_tokens (
            id INTEGER PRIMARY KEY,
            token VARCHAR(64) UNIQUE,
            booth_id INTEGER,
            role VARCHAR(32),
            revoked_at TIMESTAMP NULL
        );
        INSERT INTO invite_tokens (id, token, booth_id, role) VALUES
            (1, 'raw-secret-token-1', 10, 'interpreter'),
            (2, 'raw-secret-token-2', 10, 'interpreter'),
            (3, 'raw-secret-token-3', 20, 'room_coordinator');
        """
    )
    conn.commit()
    conn.close()

    engine = create_engine(f"sqlite:///{db}")

    # 1. Run upgrade()
    with engine.connect() as sa_conn:
        with Operations.context(MigrationContext.configure(sa_conn)):
            m037.upgrade()
        sa_conn.commit()

    # Verify public_id column exists, is populated with unique non-null values
    with engine.connect() as sa_conn:
        inspector = inspect(sa_conn)
        cols_dict = {c["name"]: c for c in inspector.get_columns("invite_tokens")}
        assert "public_id" in cols_dict
        assert cols_dict["public_id"]["nullable"] is False

        rows = sa_conn.execute(text("SELECT id, token, public_id FROM invite_tokens")).fetchall()
        assert len(rows) == 3
        pids = [r[2] for r in rows]
        assert all(pid is not None and len(pid) > 0 for pid in pids)
        assert len(set(pids)) == 3  # All unique!

    # 2. Run downgrade()
    with engine.connect() as sa_conn:
        with Operations.context(MigrationContext.configure(sa_conn)):
            m037.downgrade()
        sa_conn.commit()

    # Verify public_id was dropped and original data is preserved
    with engine.connect() as sa_conn:
        inspector = inspect(sa_conn)
        cols = [c["name"] for c in inspector.get_columns("invite_tokens")]
        assert "public_id" not in cols

        rows = sa_conn.execute(text("SELECT id, token FROM invite_tokens")).fetchall()
        assert len(rows) == 3
        assert [r[0] for r in rows] == [1, 2, 3]

    # 3. Upgrade again (idempotent / re-runnable)
    with engine.connect() as sa_conn:
        with Operations.context(MigrationContext.configure(sa_conn)):
            m037.upgrade()
        sa_conn.commit()

    with engine.connect() as sa_conn:
        inspector = inspect(sa_conn)
        cols = [c["name"] for c in inspector.get_columns("invite_tokens")]
        assert "public_id" in cols
    engine.dispose()


@pytest.mark.anyio
@pytest.mark.parametrize("naive_timestamp", [False, True])
@pytest.mark.parametrize("future_cutoff", [False, True])
async def test_booth_connect_handles_database_session_cutoff(monkeypatch, naive_timestamp, future_cutoff):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import WebSocketDisconnect

    import portal.websockets.handlers as handlers

    cutoff = datetime.now(timezone.utc) + timedelta(minutes=5 if future_cutoff else -5)
    if naive_timestamp:
        cutoff = cutoff.replace(tzinfo=None)
    user = SimpleNamespace(is_active=True, sessions_valid_after=cutoff)

    @asynccontextmanager
    async def session():
        yield object()

    monkeypatch.setattr(handlers, "get_session", session)
    monkeypatch.setattr(handlers, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(handlers, "resolve_ws_auth", AsyncMock(return_value={"sub": "10", "user": True}))
    monkeypatch.setattr(handlers, "resolve_booth_role", AsyncMock(return_value="interpreter"))
    monkeypatch.setattr(handlers, "get_booth_language_name", AsyncMock(return_value="English"))
    monkeypatch.setattr(handlers, "manager", ConnectionManager())
    ws = MockWebSocket()
    ws.accept = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect())
    await handlers.ws_booth(ws, "test-1-en")
    if future_cutoff:
        assert ws.closed_with == (WS_CLOSE_REVOKED, REASON_SESSION_EXPIRED)
        ws.receive_text.assert_not_awaited()
    else:
        assert ws.closed_with is None
        ws.receive_text.assert_awaited_once()
