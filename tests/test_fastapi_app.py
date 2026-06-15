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
from portal.auth import create_participant_token, create_user_token

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    import anyio

    from portal.database import configure, dispose, init_db

    configure('sqlite+aiosqlite://')
    anyio.run(init_db)
    yield
    anyio.run(dispose)

def _interpreter_cookie(event_slug: str = 'test-event', language_code: str = 'en') -> dict:
    """Return a cookies dict with a valid interpreter session_token."""
    tok = create_participant_token(
        booth_id=1,
        role='interpreter',
        event_slug=event_slug,
        language_code=language_code,
    )
    return {'session_token': tok}


def _admin_user_cookie() -> dict:
    """Return a cookies dict with a valid is_admin user_token."""
    tok = create_user_token(user_id=1, email='admin@test.com', is_admin=True)
    return {'user_token': tok}


# Convenience alias: admin user token has event_admin role and no scope restriction,
# so it works with any booth ID in WS tests.
_ws_auth = _admin_user_cookie


# ── REST & page tests ─────────────────────────────────────────────────────────

def test_healthz_ok():
    res = client.get('/healthz')
    assert res.status_code == 200
    body = res.json()
    assert body['ok'] is True
    assert body['server'] == 'fastapi'
    assert 'mediamtx_ok' in body
    assert 'aiortc_available' not in body


def test_home_renders_home_page():
    res = client.get('/')
    assert res.status_code == 200
    assert b'VoxBento' in res.content


def test_interpreter_booth_requires_auth():
    """Unauthenticated /interpreter/ requests redirect to login."""
    res = client.get('/interpreter/test-booth', follow_redirects=False)
    assert res.status_code == 303
    assert '/login' in res.headers['location']


def test_interpreter_booth_page_renders():
    res = client.get('/interpreter/test-booth', cookies=_interpreter_cookie())
    assert res.status_code == 200
    assert b'test-booth' in res.content


def test_interpreter_booth_jitsi_url_uses_base_url():
    """Jitsi URL in the booth page must use the configured base URL, not
    a hard-coded http:// scheme, to avoid mixed-content on HTTPS deployments."""
    res = client.get('/interpreter/test-booth', cookies=_interpreter_cookie())
    assert res.status_code == 200
    from fastapi_app import _make_jitsi_url
    from portal.config import settings
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
    res = client.get('/interpreter/test-booth', cookies=_interpreter_cookie())
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
    with client.websocket_connect('/ws/booth/ws-test-booth', cookies=_ws_auth()) as ws:
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


def test_ws_join_rejected_without_session():
    """WebSocket join must be rejected when no session cookie is present."""
    with client.websocket_connect('/ws/booth/no-session-booth') as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Hacker',
            'role': 'interpreter',
            'language': 'English',
            'channel_id': 'no-session-audio',
        }))
        msg = json.loads(ws.receive_text())
    assert msg['type'] == 'booth:error'
    assert 'No role' in msg['message']


def test_ws_join_then_leave_broadcasts_state():
    with client.websocket_connect('/ws/booth/leave-test-booth', cookies=_ws_auth()) as ws:
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
    with client.websocket_connect('/ws/booth/chat-test-booth', cookies=_ws_auth()) as ws:
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
    with client.websocket_connect('/ws/booth/json-err-booth', cookies=_ws_auth()) as ws:
        ws.send_text('not-valid-json')
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_unknown_message_type_returns_error():
    with client.websocket_connect('/ws/booth/unknown-msg-booth', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({'type': 'something:weird'}))
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_chat_before_join_returns_error():
    with client.websocket_connect('/ws/booth/no-join-chat-booth', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({'type': 'booth:chat', 'body': 'too early'}))
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_set_active_before_join_returns_error():
    with client.websocket_connect('/ws/booth/no-join-sa-booth', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({'type': 'booth:set-active', 'target_id': 'nobody'}))
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_set_active_missing_target_returns_error():
    with client.websocket_connect('/ws/booth/sa-missing-booth', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({'type': 'booth:join', 'display_name': 'Dave', 'role': 'interpreter', 'language': 'Italian', 'channel_id': 'sa-missing-booth-audio'}))
        ws.receive_text()
        ws.receive_text()

        ws.send_text(json.dumps({'type': 'booth:set-active'}))  # no target_id
        msg = json.loads(ws.receive_text())

    assert msg['type'] == 'booth:error'


def test_ws_update_state_active_interpreter():
    with client.websocket_connect('/ws/booth/upd-state-booth', cookies=_ws_auth()) as ws:
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
    with client.websocket_connect('/ws/booth/disc-booth', cookies=_ws_auth()) as ws:
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
    with client.websocket_connect('/ws/booth/self-active-booth', cookies=_ws_auth()) as ws:
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
    with client.websocket_connect('/ws/booth/standby-perm-booth', cookies=_ws_auth()) as ws_a, \
         client.websocket_connect('/ws/booth/standby-perm-booth', cookies=_ws_auth()) as ws_b:

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

    with client.websocket_connect(f'/ws/booth/{booth}', cookies=_ws_auth()) as ws_a, \
         client.websocket_connect(f'/ws/booth/{booth}', cookies=_ws_auth()) as ws_b, \
         client.websocket_connect(f'/ws/booth/{booth}', cookies=_ws_auth()) as ws_coord:

        # IntA joins (no pending for ws_a; ws_b + ws_coord each queue 1 state msg)
        ws_join(ws_a, 'IntA', 'interpreter', n_pending=0)

        # IntB joins (1 pending from IntA's join; ws_a + ws_coord queue 1 more)
        pid_b = ws_join(ws_b, 'IntB', 'interpreter', n_pending=1)
        ws_a.receive_text()   # booth:state broadcast to ws_a when IntB joined

        # Coordinator joins (2 pending from IntA + IntB joins; ws_a + ws_b queue 1 more)
        _pid_coord = ws_join(ws_coord, 'Coord', 'room_coordinator', n_pending=2)
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
    with client.websocket_connect('/ws/booth/e2e-flow-booth', cookies=_ws_auth()) as ws:
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
    with client.websocket_connect(f'/ws/booth/auth-test?token={jwt_token}', cookies=_ws_auth()) as ws:
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
    with client.websocket_connect('/ws/booth/switch-booth', cookies=_ws_auth()) as ws_a, \
         client.websocket_connect('/ws/booth/switch-booth', cookies=_ws_auth()) as ws_coord:

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
            'role': 'room_coordinator', 'language': 'French', 'channel_id': 'switch-booth-audio',
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


# ── Layer 2: WHIP URL gated endpoint tests ────────────────────────────────────

def test_whip_url_active_interpreter_gets_url():
    """Active interpreter receives a WHIP URL from the gated endpoint."""
    booth = 'whip-gate-booth'
    channel = f'{booth}-audio'
    with client.websocket_connect(f'/ws/booth/{booth}', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'Active',
            'role': 'interpreter', 'language': 'English', 'channel_id': channel,
        }))
        joined = json.loads(ws.receive_text())
        if joined['type'] != 'booth:joined':
            joined = json.loads(ws.receive_text())
        pid = joined['participant_id']
        ws.receive_text()  # drain booth:state

        res = client.get(
            f'/api/booth/{booth}/whip-url',
            params={'participant_id': pid, 'language': 'English', 'channel': channel},
        )

    assert res.status_code == 200
    body = res.json()
    assert 'whip_url' in body
    assert body['channel_id'] == channel
    assert body['booth_id'] == booth
    assert body['whip_url'].endswith(f'/{channel}/whip')


def test_whip_url_standby_interpreter_rejected():
    """Standby interpreter receives 403 from the WHIP URL endpoint."""
    booth = 'whip-standby-booth'
    channel = f'{booth}-audio'
    with client.websocket_connect(f'/ws/booth/{booth}', cookies=_ws_auth()) as ws_a, \
         client.websocket_connect(f'/ws/booth/{booth}', cookies=_ws_auth()) as ws_b:

        # IntA joins first → becomes active
        ws_a.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'IntA',
            'role': 'interpreter', 'language': 'English', 'channel_id': channel,
        }))
        ws_a.receive_text()  # booth:joined
        ws_a.receive_text()  # booth:state
        ws_b.receive_text()  # booth:state broadcast

        # IntB joins → standby
        ws_b.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'IntB',
            'role': 'interpreter', 'language': 'English', 'channel_id': channel,
        }))
        joined_b = json.loads(ws_b.receive_text())
        if joined_b['type'] != 'booth:joined':
            joined_b = json.loads(ws_b.receive_text())
        pid_b = joined_b['participant_id']
        ws_b.receive_text()  # drain booth:state
        ws_a.receive_text()  # drain broadcast to ws_a

        res = client.get(
            f'/api/booth/{booth}/whip-url',
            params={'participant_id': pid_b, 'language': 'English', 'channel': channel},
        )

    assert res.status_code == 403
    assert 'active interpreter' in res.json()['detail'].lower()


def test_whip_url_coordinator_rejected():
    """Coordinator role receives 403 from the WHIP URL endpoint."""
    booth = 'whip-coord-booth'
    channel = f'{booth}-audio'
    with client.websocket_connect(f'/ws/booth/{booth}', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'Coord',
            'role': 'room_coordinator', 'language': 'English', 'channel_id': channel,
        }))
        joined = json.loads(ws.receive_text())
        if joined['type'] != 'booth:joined':
            joined = json.loads(ws.receive_text())
        pid = joined['participant_id']
        ws.receive_text()  # drain booth:state

        res = client.get(
            f'/api/booth/{booth}/whip-url',
            params={'participant_id': pid, 'language': 'English', 'channel': channel},
        )

    assert res.status_code == 403
    assert 'interpreter role' in res.json()['detail'].lower()


def test_whip_url_unknown_participant_returns_404():
    """Unknown participant_id returns 404."""
    res = client.get(
        '/api/booth/whip-404-booth/whip-url',
        params={'participant_id': 'nonexistent', 'language': 'English'},
    )
    assert res.status_code == 404


def test_whip_url_missing_participant_id_returns_422():
    """Missing required participant_id query param returns 422."""
    res = client.get('/api/booth/whip-missing-booth/whip-url')
    assert res.status_code == 422


# ── Booth bootstrap flow tests (Issue #61) ────────────────────────────────────

def test_create_event_booth():
    """POST /api/events/{slug}/booths creates a booth and returns WHIP/WHEP URLs."""
    res = client.post('/api/events/pycon2026/booths', json={
        'language_code': 'en',
        'language': 'English',
        'room_id': 42,
    })
    assert res.status_code == 201
    body = res.json()
    assert body['booth_id'] == 'pycon2026-en'
    assert body['event_slug'] == 'pycon2026'
    assert body['language_code'] == 'en'
    assert body['mediamtx_path'] == 'pycon2026/en'
    assert body['room_id'] == 42
    assert body['whip_url'].endswith('/pycon2026/en/whip')
    assert body['whep_url'].endswith('/pycon2026/en/whep')


def test_create_event_booth_duplicate_returns_400():
    """Creating the same booth twice returns 400."""
    client.post('/api/events/duptest/booths', json={'language_code': 'fr', 'language': 'French'})
    res = client.post('/api/events/duptest/booths', json={'language_code': 'fr', 'language': 'French'})
    assert res.status_code == 400
    assert 'already exists' in res.json()['detail']


def test_create_event_booth_invalid_language_code():
    """Invalid language code returns 400."""
    res = client.post('/api/events/pycon2026/booths', json={
        'language_code': 'xyz',
        'language': 'Unknown',
    })
    assert res.status_code == 400


def test_create_event_booth_invalid_event_slug():
    """Invalid event slug returns 400."""
    res = client.post('/api/events/--bad--/booths', json={
        'language_code': 'en',
        'language': 'English',
    })
    assert res.status_code == 400


def test_list_event_booths():
    """GET /api/events/{slug}/booths lists booths for the event."""
    client.post('/api/events/listtest/booths', json={'language_code': 'en', 'language': 'English'})
    client.post('/api/events/listtest/booths', json={'language_code': 'de', 'language': 'German'})
    client.post('/api/events/other/booths', json={'language_code': 'ja', 'language': 'Japanese'})

    res = client.get('/api/events/listtest/booths')
    assert res.status_code == 200
    body = res.json()
    assert body['event_slug'] == 'listtest'
    assert len(body['booths']) == 2
    codes = {b['language_code'] for b in body['booths']}
    assert codes == {'en', 'de'}
    # Each booth should have WHEP/WHIP URLs
    for b in body['booths']:
        assert 'whip_url' in b
        assert 'whep_url' in b


def test_list_event_booths_empty():
    """Listing booths for a non-existent event returns empty list."""
    res = client.get('/api/events/nonexistent/booths')
    assert res.status_code == 200
    assert res.json()['booths'] == []


def test_interpreter_booth_by_identity_requires_auth():
    """Unauthenticated /interpreter/{slug}/{lang} redirects to login."""
    res = client.get('/interpreter/myevent/en', follow_redirects=False)
    assert res.status_code == 303
    assert '/login' in res.headers['location']


def test_interpreter_booth_by_identity_page():
    """GET /interpreter/{event_slug}/{language_code} renders the booth page."""
    res = client.get('/interpreter/myevent/en', cookies=_interpreter_cookie('myevent', 'en'))
    assert res.status_code == 200
    assert b'myevent-en' in res.content
    assert b"data-event-slug='myevent'" in res.content
    assert b"data-language-code='en'" in res.content
    assert b'data-whip-url=' in res.content
    assert b'data-whep-url=' in res.content


def test_interpreter_booth_by_identity_whip_whep_urls():
    """The identity-based booth page has correct WHIP and WHEP URLs."""
    res = client.get('/interpreter/fossasia/fr', cookies=_interpreter_cookie('fossasia', 'fr'))
    assert res.status_code == 200
    content = res.content.decode()
    assert 'fossasia/fr/whip' in content
    assert 'fossasia/fr/whep' in content


def test_interpreter_booth_by_identity_no_role_returns_403():
    """Registered user without event membership gets 403 on the booth page."""
    # user_token without is_admin and no EventMembership in DB
    tok = create_user_token(user_id=999, email='norole@test.com', is_admin=False)
    res = client.get('/interpreter/norole-event/en', cookies={'user_token': tok})
    assert res.status_code == 403


def test_interpreter_booth_admin_user_gets_super_admin_role():
    """A user with is_admin=True gets super_admin role without needing a membership."""
    res = client.get('/interpreter/myevent/en', cookies=_admin_user_cookie())
    assert res.status_code == 200
    assert b"data-granted-role='super_admin'" in res.content


def test_legacy_interpreter_booth_requires_auth():
    """Unauthenticated legacy /interpreter/{booth_id} redirects to login."""
    res = client.get('/interpreter/demo-booth', follow_redirects=False)
    assert res.status_code == 303


def test_legacy_interpreter_booth_still_works():
    """The old /interpreter/{booth_id} route still works for backward compat."""
    res = client.get('/interpreter/demo-booth', cookies=_interpreter_cookie())
    assert res.status_code == 200
    assert b'demo-booth' in res.content



def test_full_bootstrap_flow():
    """End-to-end: create booth → access page → join → go live (get WHIP URL)."""
    # 1. Organiser creates booth via API
    create_res = client.post('/api/events/bootstrap/booths', json={
        'language_code': 'es',
        'language': 'Spanish',
        'room_id': 5,
    })
    assert create_res.status_code == 201
    booth = create_res.json()
    booth_id = booth['booth_id']
    assert booth_id == 'bootstrap-es'

    # 2. Interpreter accesses booth page (with valid invite token)
    page_res = client.get('/interpreter/bootstrap/es',
                          cookies=_interpreter_cookie('bootstrap', 'es'))
    assert page_res.status_code == 200
    assert b'bootstrap-es' in page_res.content

    # 3. Interpreter joins via WebSocket
    channel = booth['mediamtx_path']
    with client.websocket_connect(f'/ws/booth/{booth_id}', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Interpreter A',
            'role': 'interpreter',
            'language': 'Spanish',
            'channel_id': channel,
        }))
        joined = json.loads(ws.receive_text())
        if joined['type'] != 'booth:joined':
            joined = json.loads(ws.receive_text())
        pid = joined['participant_id']
        ws.receive_text()  # drain booth:state

        # 4. Active interpreter requests WHIP URL (Go Live)
        whip_res = client.get(
            f'/api/booth/{booth_id}/whip-url',
            params={'participant_id': pid, 'language': 'Spanish', 'channel': channel},
        )
        assert whip_res.status_code == 200
        whip_body = whip_res.json()
        assert whip_body['whip_url'].endswith(f'/{channel}/whip')

        # 5. Verify WHEP URL is derivable from the same path
        whep_url = whip_body['whip_url'].replace('/whip', '/whep')
        assert f'/{channel}/whep' in whep_url


# ── Multi-event namespace isolation tests (#62) ──────────────────────────────


def test_event_booth_state_returns_existing():
    """Event-scoped state endpoint returns 200 for an existing booth."""
    client.post('/api/events/statetest/booths', json={'language_code': 'en', 'language': 'English'})
    res = client.get('/api/events/statetest/booths/en/state')
    assert res.status_code == 200
    body = res.json()
    assert body['booth_id'] == 'statetest-en'
    assert body['event_slug'] == 'statetest'
    assert body['language_code'] == 'en'


def test_event_booth_state_404_for_missing():
    """Event-scoped state returns 404 when booth does not exist."""
    res = client.get('/api/events/nosuchevent/booths/en/state')
    assert res.status_code == 404
    assert 'No booth' in res.json()['detail']


def test_event_booth_state_404_wrong_language():
    """Event-scoped state returns 404 when language not registered."""
    client.post('/api/events/langtest/booths', json={'language_code': 'fr', 'language': 'French'})
    res = client.get('/api/events/langtest/booths/de/state')
    assert res.status_code == 404


def test_event_booth_state_does_not_autocreate():
    """Event-scoped state must not auto-create a booth."""
    client.get('/api/events/autocreate/booths/en/state')
    res = client.get('/api/events/autocreate/booths')
    assert res.json()['booths'] == []


def test_event_booth_whip_url_active_interpreter():
    """Event-scoped WHIP URL returns URL for active interpreter."""
    client.post('/api/events/whipevent/booths', json={'language_code': 'en', 'language': 'English'})
    with client.websocket_connect('/ws/booth/whipevent-en', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Interp',
            'role': 'interpreter',
            'language': 'English',
            'channel_id': 'whipevent/en',
        }))
        joined = json.loads(ws.receive_text())
        if joined['type'] != 'booth:joined':
            joined = json.loads(ws.receive_text())
        pid = joined['participant_id']
        ws.receive_text()  # drain booth:state

        res = client.get(
            '/api/events/whipevent/booths/en/whip-url',
            params={'participant_id': pid},
        )
        assert res.status_code == 200
        body = res.json()
        assert body['whip_url'].endswith('/whipevent/en/whip')
        assert body['booth_id'] == 'whipevent-en'


def test_event_booth_whip_url_standby_rejected():
    """Event-scoped WHIP URL rejects standby interpreter."""
    client.post('/api/events/whiprej/booths', json={'language_code': 'en', 'language': 'English'})
    with client.websocket_connect('/ws/booth/whiprej-en', cookies=_ws_auth()) as ws:
        # First interpreter joins (becomes active)
        ws.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'Active',
            'role': 'interpreter', 'language': 'English', 'channel_id': 'whiprej/en',
        }))
        ws.receive_text()  # joined
        ws.receive_text()  # state

        # Second interpreter joins (becomes standby)
        with client.websocket_connect('/ws/booth/whiprej-en', cookies=_ws_auth()) as ws2:
            ws2.send_text(json.dumps({
                'type': 'booth:join', 'display_name': 'Standby',
                'role': 'interpreter', 'language': 'English', 'channel_id': 'whiprej/en',
            }))
            joined2 = json.loads(ws2.receive_text())
            if joined2['type'] != 'booth:joined':
                joined2 = json.loads(ws2.receive_text())
            pid2 = joined2['participant_id']
            ws2.receive_text()  # state

            res = client.get(
                '/api/events/whiprej/booths/en/whip-url',
                params={'participant_id': pid2},
            )
            assert res.status_code == 403


def test_cross_event_listing_isolation():
    """Booths created under event A must not appear in event B listing."""
    client.post('/api/events/isolatea/booths', json={'language_code': 'en', 'language': 'English'})
    client.post('/api/events/isolatea/booths', json={'language_code': 'fr', 'language': 'French'})
    client.post('/api/events/isolateb/booths', json={'language_code': 'de', 'language': 'German'})

    a_res = client.get('/api/events/isolatea/booths')
    b_res = client.get('/api/events/isolateb/booths')

    assert len(a_res.json()['booths']) == 2
    assert len(b_res.json()['booths']) == 1
    assert all(b['event_slug'] == 'isolatea' for b in a_res.json()['booths'])
    assert all(b['event_slug'] == 'isolateb' for b in b_res.json()['booths'])


def test_cross_event_state_isolation():
    """Event-scoped state endpoint must not leak booths across events."""
    client.post('/api/events/eventx/booths', json={'language_code': 'en', 'language': 'English'})
    # eventx-en exists, but asking eventy for 'en' must return 404
    res = client.get('/api/events/eventy/booths/en/state')
    assert res.status_code == 404


def test_cross_event_mediamtx_path_isolation():
    """Two events with the same language must get separate MediaMTX paths."""
    r1 = client.post('/api/events/confa/booths', json={'language_code': 'en', 'language': 'English'})
    r2 = client.post('/api/events/confb/booths', json={'language_code': 'en', 'language': 'English'})

    assert r1.json()['mediamtx_path'] == 'confa/en'
    assert r2.json()['mediamtx_path'] == 'confb/en'
    assert r1.json()['booth_id'] != r2.json()['booth_id']


def test_ws_cross_event_join_rejected():
    """WebSocket join with mismatched event_slug must be rejected."""
    client.post('/api/events/evtreal/booths', json={'language_code': 'en', 'language': 'English'})
    with client.websocket_connect('/ws/booth/evtreal-en', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Attacker',
            'role': 'interpreter',
            'language': 'English',
            'channel_id': 'evtreal/en',
            'event_slug': 'wrongevent',  # mismatch!
        }))
        resp = json.loads(ws.receive_text())
        assert resp['type'] == 'booth:error'
        assert 'does not belong' in resp['message']


def test_ws_cross_event_join_accepted_with_correct_slug():
    """WebSocket join with matching event_slug must succeed."""
    client.post('/api/events/evtok/booths', json={'language_code': 'en', 'language': 'English'})
    with client.websocket_connect('/ws/booth/evtok-en', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join',
            'display_name': 'Good Interpreter',
            'role': 'interpreter',
            'language': 'English',
            'channel_id': 'evtok/en',
            'event_slug': 'evtok',  # correct
        }))
        resp = json.loads(ws.receive_text())
        assert resp['type'] == 'booth:joined'


def test_full_isolation_flow():
    """End-to-end: two separate events share no state."""
    # Create booths for two events with the same language
    client.post('/api/events/fest1/booths', json={'language_code': 'en', 'language': 'English'})
    client.post('/api/events/fest2/booths', json={'language_code': 'en', 'language': 'English'})

    # Join fest1 booth
    with client.websocket_connect('/ws/booth/fest1-en', cookies=_ws_auth()) as ws:
        ws.send_text(json.dumps({
            'type': 'booth:join', 'display_name': 'Alice',
            'role': 'interpreter', 'language': 'English', 'channel_id': 'fest1/en',
        }))
        joined = json.loads(ws.receive_text())
        if joined['type'] != 'booth:joined':
            joined = json.loads(ws.receive_text())
        ws.receive_text()  # state

        # fest1 has 1 participant; fest2 has 0
        state1 = client.get('/api/events/fest1/booths/en/state').json()
        state2 = client.get('/api/events/fest2/booths/en/state').json()
        assert len(state1['participants']) == 1
        assert len(state2['participants']) == 0

        # fest2 listing must not show fest1 booths
        listing2 = client.get('/api/events/fest2/booths').json()
        assert all(b['event_slug'] == 'fest2' for b in listing2['booths'])

