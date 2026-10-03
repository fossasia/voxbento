import json

import pytest
from starlette.websockets import WebSocket, WebSocketState

from portal.websockets.manager import ConnectionManager, Session


class MockWebSocket:
    def __init__(self):
        self.sent_messages = []

    async def send_text(self, data):
        self.sent_messages.append(data)


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


def _session(booth_id: str) -> Session:
    return Session(booth_id=booth_id, participant_id=None, language="English", channel_id=f"{booth_id}-audio")


@pytest.mark.anyio
async def test_broadcast_reaches_other_sockets_after_a_dropped_one():
    manager = ConnectionManager()
    dropped = _dropped_websocket()
    alive = MockWebSocket()
    manager.add(dropped, _session("pycon2026-1-en"))
    manager.add(alive, _session("pycon2026-1-en"))

    await manager.broadcast("pycon2026-1-en", {"type": "booth:chat", "message": "hi"})

    assert [json.loads(m) for m in alive.sent_messages] == [{"type": "booth:chat", "message": "hi"}]


@pytest.mark.anyio
async def test_broadcast_removes_the_dropped_socket():
    manager = ConnectionManager()
    dropped = _dropped_websocket()
    alive = MockWebSocket()
    manager.add(dropped, _session("pycon2026-1-en"))
    manager.add(alive, _session("pycon2026-1-en"))

    await manager.broadcast("pycon2026-1-en", {"type": "booth:state", "state": {}})

    assert manager.get_session(dropped) is None
    assert manager.get_session(alive) is not None
