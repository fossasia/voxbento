from __future__ import annotations

import json

import pytest
from starlette.websockets import WebSocket, WebSocketState

from portal.websockets.manager import ConnectionManager, Session


class MockWebSocket:
    """Mock WebSocket for manager unit tests."""

    def __init__(self):
        """Initialize mock WebSocket."""
        self.sent_messages = []
        self.closed_with = None

    async def send_text(self, data):
        """Record sent text message."""
        self.sent_messages.append(data)

    async def close(self, code=1000, reason=""):
        """Record close code and reason."""
        self.closed_with = (code, reason)


def _dropped_websocket() -> WebSocket:
    """A real Starlette WebSocket whose peer has gone away.

    The ASGI server raises OSError when sending to a closed connection, and
    Starlette turns that into WebSocketDisconnect.
    """

    async def receive():
        return {"type": "websocket.disconnect", "code": 1006}

    async def send(message):
        raise OSError("connection lost")

    ws = WebSocket({"type": "websocket", "path": "/ws/booth/x", "headers": []}, receive=receive, send=send)
    ws.client_state = WebSocketState.CONNECTED
    ws.application_state = WebSocketState.CONNECTED
    return ws


def _session(booth_id: str, granted_role: str | None = "interpreter") -> Session:
    """Create a mock session for testing."""
    return Session(
        booth_id=booth_id,
        participant_id=None,
        language="English",
        channel_id=f"{booth_id}-audio",
        granted_role=granted_role,
    )


@pytest.mark.anyio
async def test_broadcast_reaches_other_sockets_after_a_dropped_one():
    """Verify broadcast delivers to surviving sockets after one drops."""
    manager = ConnectionManager()
    dropped = _dropped_websocket()
    alive = MockWebSocket()
    manager.add(dropped, _session("pycon2026-1-en"))
    manager.add(alive, _session("pycon2026-1-en"))

    await manager.broadcast("pycon2026-1-en", {"type": "booth:chat", "message": "hi"})

    assert [json.loads(m) for m in alive.sent_messages] == [{"type": "booth:chat", "message": "hi"}]


@pytest.mark.anyio
async def test_broadcast_removes_the_dropped_socket():
    """Verify dropped socket is pruned from the room after failed broadcast."""
    manager = ConnectionManager()
    dropped = _dropped_websocket()
    alive = MockWebSocket()
    manager.add(dropped, _session("pycon2026-1-en"))
    manager.add(alive, _session("pycon2026-1-en"))

    await manager.broadcast("pycon2026-1-en", {"type": "booth:state", "state": {}})

    assert manager.get_session(dropped) is None
    assert manager.get_session(alive) is not None


@pytest.mark.anyio
async def test_broadcast_skips_sockets_without_a_booth_role():
    """Sockets whose session carries no granted booth role receive nothing.

    They stay connected (cleanup semantics unchanged) but are excluded from
    booth:state/chat delivery.
    """
    manager = ConnectionManager()
    outsider = MockWebSocket()
    member = MockWebSocket()
    manager.add(outsider, _session("pycon2026-1-en", granted_role=None))
    manager.add(member, _session("pycon2026-1-en", granted_role="interpreter"))

    await manager.broadcast("pycon2026-1-en", {"type": "booth:chat", "message": "hi"})

    assert outsider.sent_messages == []
    assert [json.loads(m) for m in member.sent_messages] == [{"type": "booth:chat", "message": "hi"}]
    # The skipped socket is not treated as dead: membership is unchanged.
    assert manager.get_session(outsider) is not None


@pytest.mark.anyio
async def test_broadcast_skips_unsupported_role():
    """Verify sockets with unsupported roles receive no broadcast messages."""
    manager = ConnectionManager()
    outsider = MockWebSocket()
    member = MockWebSocket()
    manager.add(outsider, _session("pycon2026-1-en", granted_role="guest"))
    manager.add(member, _session("pycon2026-1-en", granted_role="interpreter"))

    await manager.broadcast("pycon2026-1-en", {"type": "booth:chat", "message": "hi"})

    assert outsider.sent_messages == []
    assert [json.loads(m) for m in member.sent_messages] == [{"type": "booth:chat", "message": "hi"}]


@pytest.mark.anyio
async def test_broadcast_skips_revoked_session():
    """Verify revoked sessions are skipped during broadcast."""
    manager = ConnectionManager()
    ws = MockWebSocket()
    session = _session("pycon2026-1-en", granted_role="interpreter")
    session.user_id = 42
    manager.add(ws, session)

    manager.revoke_session(ws)
    assert session.revoked is True
    assert session.granted_role is None

    await manager.broadcast("pycon2026-1-en", {"type": "booth:chat", "message": "hi"})
    assert ws.sent_messages == []


@pytest.mark.anyio
async def test_revoke_user_sessions():
    """Verify revoke_user_sessions marks all sockets of a user as revoked."""
    manager = ConnectionManager()
    ws1 = MockWebSocket()
    ws2 = MockWebSocket()
    ws3 = MockWebSocket()

    sess1 = _session("b1", granted_role="interpreter")
    sess1.user_id = 99
    sess2 = _session("b2", granted_role="interpreter")
    sess2.user_id = 99
    sess3 = _session("b1", granted_role="interpreter")
    sess3.user_id = 100

    manager.add(ws1, sess1)
    manager.add(ws2, sess2)
    manager.add(ws3, sess3)

    revoked = manager.revoke_user_sessions(user_id=99, booth_id="b1")
    assert revoked == [ws1]
    assert sess1.revoked is True
    assert sess2.revoked is False
    assert sess3.revoked is False


@pytest.mark.anyio
async def test_close_user_sessions_closes_only_that_users_sockets():
    """Verify close_user_sessions closes only the targeted user's sockets."""
    manager = ConnectionManager()
    target = MockWebSocket()
    other_user = MockWebSocket()
    invite = MockWebSocket()

    target_session = _session("b1", granted_role="interpreter")
    target_session.user_id = 7
    other_session = _session("b1", granted_role="interpreter")
    other_session.user_id = 8
    # Invite tokens are bearer capabilities: their UUID ``sub`` never yields a
    # numeric user_id, so account revocation must not touch them.
    invite_session = _session("b1", granted_role="interpreter")
    invite_session.user_id = None

    manager.add(target, target_session)
    manager.add(other_user, other_session)
    manager.add(invite, invite_session)

    await manager.close_user_sessions(7)

    assert target.closed_with == (4003, "Authorization revoked")
    assert target_session.revoked is True
    assert manager.get_session(target) is None

    assert other_user.closed_with is None
    assert other_session.revoked is False
    assert manager.get_session(other_user) is not None

    assert invite.closed_with is None
    assert invite_session.revoked is False
    assert manager.get_session(invite) is not None


class FailingCloseMockWebSocket(MockWebSocket):
    """Mock WebSocket whose close method raises an unexpected exception."""

    async def close(self, code=1000, reason=""):
        """Raise unexpected runtime error on close."""
        raise RuntimeError("Unexpected failure during close")


@pytest.mark.anyio
async def test_close_user_sessions_handles_socket_exception_and_closes_others():
    """Verify failing close on one socket does not abort closing other sockets."""
    manager = ConnectionManager()
    failing = FailingCloseMockWebSocket()
    normal = MockWebSocket()

    sess_fail = _session("b1", granted_role="interpreter")
    sess_fail.user_id = 7
    sess_norm = _session("b1", granted_role="interpreter")
    sess_norm.user_id = 7

    manager.add(failing, sess_fail)
    manager.add(normal, sess_norm)

    await manager.close_user_sessions(7)

    assert sess_fail.revoked is True
    assert manager.get_session(failing) is None

    assert normal.closed_with == (4003, "Authorization revoked")
    assert sess_norm.revoked is True
    assert manager.get_session(normal) is None


@pytest.mark.anyio
async def test_broadcast_booth_state_isolates_and_sanitizes():
    """broadcast_booth_state sends full state to authorized interpreters and sanitized state to listeners."""
    from portal.websockets.manager import broadcast_booth_state, listener_manager, manager

    booth_id = "test-booth-state"
    interp_ws = MockWebSocket()
    listener_ws = MockWebSocket()
    revoked_ws = MockWebSocket()

    interp_session = _session(booth_id, granted_role="interpreter")
    revoked_session = _session(booth_id, granted_role="interpreter")
    revoked_session.revoked = True

    manager.add(interp_ws, interp_session)
    manager.add(revoked_ws, revoked_session)
    listener_manager.add(listener_ws, booth_id)

    raw_state = {
        "booth_id": booth_id,
        "broadcast_unlocked": True,
        "ingest_status": "connected",
        "chat_messages": [{"sender": "Alice", "text": "secret"}],
        "participants": [{"id": "p1", "name": "Alice"}],
    }

    try:
        await broadcast_booth_state(booth_id, raw_state)

        # Interpreter receives full state
        assert len(interp_ws.sent_messages) == 1
        interp_payload = json.loads(interp_ws.sent_messages[0])
        assert interp_payload["type"] == "booth:state"
        assert interp_payload["state"] == raw_state
        assert "participants" in interp_payload["state"]

        # Revoked socket receives nothing
        assert len(revoked_ws.sent_messages) == 0

        # Listener receives sanitized state
        assert len(listener_ws.sent_messages) == 1
        listener_payload = json.loads(listener_ws.sent_messages[0])
        assert listener_payload["type"] == "booth:state"
        assert listener_payload["state"] == {
            "booth_id": booth_id,
            "event_slug": None,
            "room_id": None,
            "language_code": None,
            "broadcast_unlocked": True,
            "ingest_status": "connected",
        }
        assert "participants" not in listener_payload["state"]
        assert "chat_messages" not in listener_payload["state"]
    finally:
        manager.remove(interp_ws)
        manager.remove(revoked_ws)
        listener_manager.remove(listener_ws, booth_id)
