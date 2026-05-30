"""Integration tests for FastAPI app — Phase 1C WebSocket protocol and REST API."""
from __future__ import annotations

import json
import os

# Force auth off for all tests — setdefault would not override an already-set env var,
# so use an unconditional assignment to guarantee a deterministic baseline.
os.environ['BOOTH_ACCESS_TOKEN'] = ''

import pytest
from fastapi.testclient import TestClient

from fastapi_app import app

client = TestClient(app)


# ── REST & page tests ─────────────────────────────────────────────────────────

def test_healthz_ok():
    res = client.get('/healthz')
    assert res.status_code == 200
    body = res.json()
    assert body['ok'] is True
    assert body['server'] == 'fastapi'
    assert 'mediamtx_ok' in body
    assert 'aiortc_available' not in body


def test_home_redirects_to_demo_booth():
    res = client.get('/', follow_redirects=False)
    assert res.status_code in (301, 302, 307, 308)
    assert 'demo-booth' in res.headers['location']


def test_interpreter_booth_page_renders():
    res = client.get('/interpreter/test-booth')
    assert res.status_code == 200
    assert b'test-booth' in res.content


def test_interpreter_booth_jitsi_url_uses_base_url():
    """Jitsi URL in the booth page must use the configured base URL, not
    a hard-coded http:// scheme, to avoid mixed-content on HTTPS deployments."""
    res = client.get('/interpreter/test-booth')
    assert res.status_code == 200
    from portal.config import settings
    from fastapi_app import _make_jitsi_url
    expected = _make_jitsi_url(settings.effective_jitsi_base_url, settings.default_jitsi_room)
    assert expected.encode() in res.content


def test_make_jitsi_url_bare_room():
    """Bare room name is prefixed with the base URL."""
    from fastapi_app import _make_jitsi_url
    assert _make_jitsi_url('http://localhost:8080', 'my-room') == 'http://localhost:8080/my-room'


def test_make_jitsi_url_full_url_unchanged():
    """A full URL stored in DEFAULT_JITSI_ROOM must not be double-prefixed."""
    from fastapi_app import _make_jitsi_url
    full = 'https://meet.jit.si/eventyay-stage-room'
    assert _make_jitsi_url('http://localhost:8080', full) == full


def test_interpreter_booth_jitsi_domain_matches_base_url_host():
    """data-jitsi-domain must equal the host of the effective Jitsi base URL.

    When JITSI_BASE_URL overrides the scheme/host, the JS validation in
    joinMonitoringFeed() compares meetingUrl.host against data-jitsi-domain.
    If they differ the user's own pre-filled URL is rejected.
    """
    from urllib.parse import urlparse
    from portal.config import settings
    res = client.get('/interpreter/test-booth')
    assert res.status_code == 200
    expected_host = urlparse(settings.effective_jitsi_base_url).netloc
    assert f"data-jitsi-domain='{expected_host}'".encode() in res.content


def test_auth_token_no_password():
    """When BOOTH_ACCESS_TOKEN is empty, any (or empty) token grants a JWT."""
    res = client.post('/api/auth/token', json={'token': ''})
    assert res.status_code == 200
    body = res.json()
    assert 'access_token' in body
    assert body['token_type'] == 'bearer'


def test_booth_state_returns_empty_booth():
    res = client.get('/api/booth/empty-booth/state')
    assert res.status_code == 200
    body = res.json()
    assert 'participants' in body


def test_ingest_status_endpoint():
    res = client.get('/api/interpreter/status/some-channel')
    assert res.status_code == 200
    body = res.json()
    assert body['channel_id'] == 'some-channel'
    assert body['state'] == 'mediamtx'
    assert 'reachable' in body


# ── WebSocket protocol tests ──────────────────────────────────────────────────

def test_ws_join_receives_joined_and_state():
    with client.websocket_connect('/ws/booth/ws-test-booth') as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Alice',
            'role': 'interpreter',
            'language': 'French',
            'channel_id': 'ws-test-booth-audio',
        }))
        msg1 = json.loads(ws.receive_text())
        msg2 = json.loads(ws.receive_text())

    types = {msg1['type'], msg2['type']}
    assert 'booth:joined' in types
    assert 'booth:state' in types

    joined = msg1 if msg1['type'] == 'booth:joined' else msg2
    assert 'participant_id' in joined
    assert 'state' in joined


def test_ws_join_then_leave_broadcasts_state():
    with client.websocket_connect('/ws/booth/leave-test-booth') as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Bob',
            'role': 'interpreter',
            'language': 'German',
            'channel_id': 'leave-test-booth-audio',
        }))
        # consume booth:joined + booth:state
        ws.receive_text()
        ws.receive_text()

        ws.send_text(json.dumps({'type': 'booth:leave'}))
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:state'


def test_ws_chat_message():
    with client.websocket_connect('/ws/booth/chat-test-booth') as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Charlie',
            'role': 'interpreter',
            'language': 'Spanish',
            'channel_id': 'chat-test-booth-audio',
        }))
        ws.receive_text()  # booth:joined
        ws.receive_text()  # booth:state

        ws.send_text(json.dumps({'type': 'booth:chat', 'body': 'Hello'}))
        chat_msg = json.loads(ws.receive_text())
        # there is also a booth:state broadcast; allow either order
        if chat_msg['type'] == 'booth:state':
            chat_msg = json.loads(ws.receive_text())

    assert chat_msg['type'] == 'booth:chat'
    assert 'message' in chat_msg


def test_ws_invalid_json_returns_error():
    with client.websocket_connect('/ws/booth/json-err-booth') as ws:
        ws.send_text('not-valid-json')
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_unknown_message_type_returns_error():
    with client.websocket_connect('/ws/booth/unknown-msg-booth') as ws:
        ws.send_text(json.dumps({'type': 'something:weird'}))
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_chat_before_join_returns_error():
    with client.websocket_connect('/ws/booth/no-join-chat-booth') as ws:
        ws.send_text(json.dumps({'type': 'booth:chat', 'body': 'too early'}))
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_set_active_before_join_returns_error():
    with client.websocket_connect('/ws/booth/no-join-sa-booth') as ws:
        ws.send_text(json.dumps({'type': 'booth:set-active', 'target_id': 'nobody'}))
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_set_active_missing_target_returns_error():
    with client.websocket_connect('/ws/booth/sa-missing-booth') as ws:
        ws.send_text(json.dumps({'type': 'booth:join', 'display_name': 'Dave', 'role': 'interpreter', 'language': 'Italian', 'channel_id': 'sa-missing-booth-audio'}))
        ws.receive_text()
        ws.receive_text()

        ws.send_text(json.dumps({'type': 'booth:set-active'}))  # no target_id
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_update_state_active_interpreter():
    with client.websocket_connect('/ws/booth/upd-state-booth') as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Eve',
            'role': 'interpreter',
            'language': 'French',
            'channel_id': 'upd-state-booth-audio',
        }))
        ws.receive_text()  # booth:joined
        ws.receive_text()  # booth:state broadcast

        ws.send_text(json.dumps({'type': 'booth:update-state', 'mic_active': True}))
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:state'


def test_ws_disconnect_without_leave_auto_removes_participant():
    """Participant is cleaned up from registry when WS disconnects unexpectedly."""
    with client.websocket_connect('/ws/booth/disc-booth') as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Frank',
            'role': 'interpreter',
            'language': 'Dutch',
            'channel_id': 'disc-booth-audio',
        }))
        ws.receive_text()  # booth:joined
        ws.receive_text()  # booth:state

    # After disconnect, state should have zero participants
    res = client.get('/api/booth/disc-booth/state?language=Dutch&channel=disc-booth-audio')
    assert res.status_code == 200
    assert len(res.json()['participants']) == 0


def test_ws_active_interpreter_can_set_active():
    """Active interpreter can call booth:set-active (targeting themselves).

    Uses a single WebSocket connection to avoid multi-connection message-ordering
    issues with TestClient. Permission logic is covered by test_booth_state.py;
    here we verify the WS protocol path produces a booth:state response.
    """
    with client.websocket_connect('/ws/booth/self-active-booth') as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Solo',
            'role': 'interpreter',
            'language': 'French',
            'channel_id': 'self-active-booth-audio',
        }))
        joined = json.loads(ws.receive_text())
        if joined['type'] != 'booth:joined':
            joined = json.loads(ws.receive_text())
        pid = joined['participant_id']
        ws.receive_text()  # drain booth:state broadcast

        # Active interpreter sets themselves as active — success path
        ws.send_text(json.dumps({'type': 'booth:set-active', 'target_id': pid}))
        state_msg = json.loads(ws.receive_text())

    assert state_msg['type'] == 'booth:state'
    assert state_msg['state']['active_interpreter_id'] == pid


def test_ws_standby_cannot_set_mic_active():
    """Standby interpreter (not active) receives booth:error when trying to set mic_active."""
    # Use two connections: IntA (first-joined = active), IntB (standby)
    with client.websocket_connect('/ws/booth/standby-perm-booth') as ws_a, \
         client.websocket_connect('/ws/booth/standby-perm-booth') as ws_b:

        # IntA joins first → becomes active; ws_b receives the broadcast immediately
        ws_a.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'A', 'role': 'interpreter',
            'language': 'French', 'channel_id': 'standby-perm-audio',
        }))
        joined_a = json.loads(ws_a.receive_text())
        if joined_a['type'] != 'booth:joined':
            joined_a = json.loads(ws_a.receive_text())
        ws_a.receive_text()  # drain booth:state broadcast on ws_a
        ws_b.receive_text()  # drain booth:state broadcast from IntA joining (ws_b's queue)

        # IntB joins → is standby; ws_a gets a broadcast
        ws_b.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'B', 'role': 'interpreter',
            'language': 'French', 'channel_id': 'standby-perm-audio',
        }))
        joined_b = json.loads(ws_b.receive_text())
        if joined_b['type'] != 'booth:joined':
            joined_b = json.loads(ws_b.receive_text())
        ws_b.receive_text()  # drain booth:state on ws_b
        ws_a.receive_text()  # drain broadcast to ws_a when ws_b joined

        # IntB (standby) tries to set mic active → should get booth:error
        ws_b.send_text(json.dumps({'type': 'booth:update-state', 'mic_active': True}))
        err_msg = json.loads(ws_b.receive_text())

    assert err_msg['type'] == 'booth:error'


def test_ws_three_way_coordinator_flow():
    """Full 3-connection scenario: two interpreters + coordinator.

    Coordinator switches the active interpreter from A to B.
    Verifies the booth:state broadcast reflects the new active interpreter.
    All expected WS messages are drained to keep the test deterministic.
    """
    booth = 'three-way-coord-booth'
    channel = f'{booth}-audio'

    def ws_join(ws, name, role, n_pending):
        """Send booth:join and drain deterministically.

        n_pending = number of booth:state messages already queued on this
        connection from other participants who joined earlier.
        After this call the connection's receive queue is empty.
        """
        ws.send_text(json.dumps({
            'type': 'booth:join', 'display_name': name, 'role': role,
            'language': 'French', 'channel_id': channel,
        }))
        # Drain stale broadcasts from earlier joins
        for _ in range(n_pending):
            ws.receive_text()
        # Read booth:joined (may follow stale state if not fully drained; drain handles it)
        msg = json.loads(ws.receive_text())
        assert msg['type'] == 'booth:joined', (
            f'Expected booth:joined after draining {n_pending}; got {msg["type"]}'
        )
        pid = msg['participant_id']
        ws.receive_text()  # drain booth:state broadcast from own join
        return pid

    with client.websocket_connect(f'/ws/booth/{booth}') as ws_a, \
         client.websocket_connect(f'/ws/booth/{booth}') as ws_b, \
         client.websocket_connect(f'/ws/booth/{booth}') as ws_coord:

        # IntA joins (no pending for ws_a; ws_b + ws_coord each queue 1 state msg)
        pid_a = ws_join(ws_a, 'IntA', 'interpreter', n_pending=0)

        # IntB joins (1 pending from IntA's join; ws_a + ws_coord queue 1 more)
        pid_b = ws_join(ws_b, 'IntB', 'interpreter', n_pending=1)
        ws_a.receive_text()   # booth:state broadcast to ws_a when IntB joined

        # Coordinator joins (2 pending from IntA + IntB joins; ws_a + ws_b queue 1 more)
        _pid_coord = ws_join(ws_coord, 'Coord', 'coordinator', n_pending=2)
        ws_a.receive_text()   # booth:state broadcast to ws_a when coordinator joined
        ws_b.receive_text()   # booth:state broadcast to ws_b when coordinator joined

        # All queues are empty. Coordinator sets IntB as active.
        ws_coord.send_text(json.dumps({'type': 'booth:set-active', 'target_id': pid_b}))

        # All three connections receive the broadcast; consume all to keep test clean
        state_on_coord = json.loads(ws_coord.receive_text())
        ws_a.receive_text()
        ws_b.receive_text()

    assert state_on_coord['type'] == 'booth:state', (
        f'Expected booth:state after set-active, got {state_on_coord["type"]}'
    )
    assert state_on_coord['state']['active_interpreter_id'] == pid_b


def test_ws_full_flow_join_update_chat_leave():
    """Single-connection end-to-end flow: join → update-state → chat → leave."""
    with client.websocket_connect('/ws/booth/e2e-flow-booth') as ws:
        # 1. Join
        ws.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'E2E', 'role': 'interpreter',
            'language': 'French', 'channel_id': 'e2e-flow-audio',
        }))
        joined = json.loads(ws.receive_text())
        if joined['type'] != 'booth:joined':
            joined = json.loads(ws.receive_text())
        assert joined['type'] == 'booth:joined'
        pid = joined['participant_id']
        ws.receive_text()  # drain booth:state

        # 2. Update state (active interpreter can set mic_active)
        ws.send_text(json.dumps({'type': 'booth:update-state', 'mic_active': True}))
        state_msg = json.loads(ws.receive_text())
        assert state_msg['type'] == 'booth:state'
        active_p = next((p for p in state_msg['state']['participants'] if p['participant_id'] == pid), None)
        assert active_p is not None
        assert active_p['mic_active'] is True

        # 3. Chat — server sends booth:chat THEN booth:state (both must be drained)
        ws.send_text(json.dumps({'type': 'booth:chat', 'body': 'E2E test message'}))
        msg_x = json.loads(ws.receive_text())
        msg_y = json.loads(ws.receive_text())
        # Normalise order (chat comes first in practice, but be defensive)
        if msg_x['type'] == 'booth:state':
            msg_x, msg_y = msg_y, msg_x
        assert msg_x['type'] == 'booth:chat'
        assert msg_x['message']['body'] == 'E2E test message'
        assert msg_y['type'] == 'booth:state'

        # 4. Leave
        ws.send_text(json.dumps({'type': 'booth:leave'}))
        leave_state = json.loads(ws.receive_text())

    assert leave_state['type'] == 'booth:state'
    # After leave, participant is no longer in the booth
    participants = leave_state['state']['participants']
    assert all(p['participant_id'] != pid for p in participants)


def test_ws_auth_required_with_token(monkeypatch):
    """When BOOTH_ACCESS_TOKEN is set, a valid JWT is needed to use the API."""
    from portal.config import settings
    monkeypatch.setenv('BOOTH_ACCESS_TOKEN', 'secret-test-token')
    monkeypatch.setattr(settings, 'booth_access_token', 'secret-test-token')
    # When the provided token matches BOOTH_ACCESS_TOKEN, the endpoint issues a JWT.
    res = client.post('/api/auth/token', json={'token': 'secret-test-token'})
    assert res.status_code == 200
    jwt_token = res.json()['access_token']

    # WebSocket WITHOUT a token should be rejected (closed with code 4001).
    with pytest.raises(Exception):
        with client.websocket_connect('/ws/booth/auth-test') as ws:
            ws.receive_text()

    # WebSocket WITH a valid JWT should be accepted.
    with client.websocket_connect(f'/ws/booth/auth-test?token={jwt_token}') as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'AuthUser',
            'role': 'interpreter', 'language': 'English',
            'channel_id': 'auth-test-audio',
        }))
        msg = json.loads(ws.receive_text())
        assert msg['type'] in ('booth:joined', 'booth:state')

    # A wrong access token should be rejected by the token endpoint.
    res_bad = client.post('/api/auth/token', json={'token': 'wrong-password'})
    assert res_bad.status_code == 401


def test_ws_coordinator_can_switch_active_interpreter():
    """Coordinator assigns a second interpreter as active; state broadcast reflects the change."""
    with client.websocket_connect('/ws/booth/switch-booth') as ws_a, \
         client.websocket_connect('/ws/booth/switch-booth') as ws_coord:

        # Interpreter A joins
        ws_a.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'IntA',
            'role': 'interpreter', 'language': 'French', 'channel_id': 'switch-booth-audio',
        }))
        joined_a = json.loads(ws_a.receive_text())
        if joined_a['type'] != 'booth:joined':
            joined_a = json.loads(ws_a.receive_text())
        pid_a = joined_a['participant_id']
        ws_a.receive_text()  # booth:state broadcast

        # Coordinator joins; IntA gets a state broadcast
        ws_coord.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'Coord',
            'role': 'coordinator', 'language': 'French', 'channel_id': 'switch-booth-audio',
        }))
        joined_coord = json.loads(ws_coord.receive_text())
        if joined_coord['type'] != 'booth:joined':
            joined_coord = json.loads(ws_coord.receive_text())
        ws_coord.receive_text()  # booth:state on coord side
        ws_a.receive_text()     # booth:state broadcast to IntA when coord joins

        # Coordinator sets IntA as active
        ws_coord.send_text(json.dumps({'type': 'booth:set-active', 'target_id': pid_a}))
        # Drain responses until we find a booth:state with active_interpreter_id set
        state_msg = None
        for _ in range(3):
            raw = ws_coord.receive_text()
            msg = json.loads(raw)
            if msg['type'] == 'booth:state':
                state_msg = msg
                break

    assert state_msg is not None, 'Expected a booth:state after set-active'
    assert state_msg['state']['active_interpreter_id'] == pid_a

