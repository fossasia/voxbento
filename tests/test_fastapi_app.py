"""Integration tests for FastAPI app — Phase 1C WebSocket protocol and REST API."""
from __future__ import annotations

import json
import os

# Ensure auth is disabled for tests
os.environ.setdefault('BOOTH_ACCESS_TOKEN', '')

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


def test_home_redirects_to_demo_booth():
    res = client.get('/', follow_redirects=False)
    assert res.status_code in (301, 302, 307, 308)
    assert 'demo-booth' in res.headers['location']


def test_interpreter_booth_page_renders():
    res = client.get('/interpreter/test-booth')
    assert res.status_code == 200
    assert b'test-booth' in res.content


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
    assert 'state' in body


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
