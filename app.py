from __future__ import annotations

import atexit
from threading import RLock
from typing import Any

from flask import Flask, jsonify, redirect, render_template, request
from flask_socketio import SocketIO, emit, join_room, leave_room

from portal.booth_state import BoothRegistry
from portal.config import settings
from portal.ingest import AIORTC_AVAILABLE, IngestService, IngestUnavailableError

app = Flask(__name__)
app.config['SECRET_KEY'] = settings.secret_key
socketio = SocketIO(app, cors_allowed_origins=settings.socket_cors_origins, async_mode='threading')

booths = BoothRegistry()
ingest = IngestService(settings)

sid_index: dict[str, dict[str, str]] = {}
sid_index_lock = RLock()


def booth_room(booth_id: str) -> str:
    return f'booth:{booth_id}'


def require_access_token(token: str | None) -> None:
    if not settings.booth_access_token:
        return
    if token != settings.booth_access_token:
        raise PermissionError('Invalid booth access token.')


def emit_booth_state(booth_id: str, state: dict[str, Any]) -> None:
    socketio.emit('booth:state', state, room=booth_room(booth_id))


def json_error(message: str, code: int) -> tuple[dict[str, str], int]:
    return {'error': message}, code


@app.route('/')
def home() -> Any:
    return redirect('/interpreter/demo-booth')


@app.route('/healthz')
def healthz() -> Any:
    return jsonify(
        {
            'ok': True,
            'aiortc_available': AIORTC_AVAILABLE,
        }
    )


@app.route('/interpreter/<booth_id>')
def interpreter_booth(booth_id: str) -> Any:
    token = request.args.get('token', '')
    language = request.args.get('language', 'English')
    channel_id = request.args.get('channel', f'{booth_id}-audio')
    return render_template(
        'interpreter_booth.html',
        booth_id=booth_id,
        booth_token=token,
        booth_language=language,
        booth_channel_id=channel_id,
        default_jitsi_room=settings.default_jitsi_room,
        jitsi_domain=settings.jitsi_domain,
        aiortc_available=AIORTC_AVAILABLE,
    )


@app.route('/api/booth/<booth_id>/state')
def booth_state(booth_id: str) -> Any:
    token = request.args.get('token')
    language = request.args.get('language', 'English')
    channel_id = request.args.get('channel', f'{booth_id}-audio')
    try:
        require_access_token(token)
    except PermissionError as error:
        return json_error(str(error), 403)
    return jsonify(booths.snapshot(booth_id, language, channel_id))


@app.route('/api/interpreter/connect/<channel_id>', methods=['POST'])
def connect_interpreter_ingest(channel_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    booth_id = payload.get('booth_id')
    participant_id = payload.get('participant_id')
    token = payload.get('token')
    language = payload.get('language', 'English')
    offer_type = payload.get('type')
    offer_sdp = payload.get('sdp')
    if not booth_id or not participant_id or not offer_type or not offer_sdp:
        return json_error('Missing required fields: booth_id, participant_id, type, sdp.', 400)
    try:
        require_access_token(token)
    except PermissionError as error:
        return json_error(str(error), 403)
    if not booths.is_active_interpreter(booth_id, participant_id, language, channel_id):
        return json_error('Only the active interpreter can publish ingest audio.', 403)
    try:
        answer = ingest.connect(
            channel_id=channel_id,
            booth_id=booth_id,
            participant_id=participant_id,
            offer_type=offer_type,
            offer_sdp=offer_sdp,
        )
    except IngestUnavailableError as error:
        return json_error(str(error), 503)
    except Exception as error:  # pragma: no cover - runtime service failures
        return json_error(f'Ingest negotiation failed: {error}', 500)
    state = booths.update_participant_state(
        booth_id,
        participant_id,
        language,
        channel_id,
        mic_active=True,
        ingest_connected=True,
    )
    emit_booth_state(booth_id, state)
    return jsonify(answer)


@app.route('/api/interpreter/disconnect/<channel_id>', methods=['POST'])
def disconnect_interpreter_ingest(channel_id: str) -> Any:
    payload = request.get_json(silent=True) or {}
    booth_id = payload.get('booth_id')
    participant_id = payload.get('participant_id')
    token = payload.get('token')
    language = payload.get('language', 'English')
    if not booth_id or not participant_id:
        return json_error('Missing required fields: booth_id and participant_id.', 400)
    try:
        require_access_token(token)
    except PermissionError as error:
        return json_error(str(error), 403)
    ingest.disconnect(channel_id)
    state = booths.update_participant_state(
        booth_id,
        participant_id,
        language,
        channel_id,
        mic_active=False,
        ingest_connected=False,
    )
    emit_booth_state(booth_id, state)
    return jsonify({'ok': True})


@app.route('/api/interpreter/status/<channel_id>')
def ingest_status(channel_id: str) -> Any:
    return jsonify({'channel_id': channel_id, 'state': ingest.status(channel_id), 'reachable': AIORTC_AVAILABLE})


@socketio.on('booth:join')
def socket_join_booth(data: dict[str, Any]) -> None:
    booth_id = data.get('booth_id')
    token = data.get('token')
    display_name = data.get('display_name', 'Interpreter')
    role = data.get('role', 'interpreter')
    language = data.get('language', 'English')
    channel_id = data.get('channel_id', f'{booth_id}-audio')
    participant_id = data.get('participant_id')
    if not booth_id:
        emit('booth:error', {'message': 'Missing booth_id.'})
        return
    try:
        require_access_token(token)
    except PermissionError as error:
        emit('booth:error', {'message': str(error)})
        return
    try:
        participant, state = booths.join_participant(
            booth_id=booth_id,
            display_name=display_name,
            role=role,
            language=language,
            channel_id=channel_id,
            participant_id=participant_id,
        )
    except (ValueError, PermissionError) as error:
        emit('booth:error', {'message': str(error)})
        return
    join_room(booth_room(booth_id))
    with sid_index_lock:
        sid_index[request.sid] = {
            'booth_id': booth_id,
            'participant_id': participant.participant_id,
            'language': language,
            'channel_id': channel_id,
        }
    emit('booth:joined', {'participant_id': participant.participant_id, 'state': state})
    emit_booth_state(booth_id, state)


@socketio.on('booth:leave')
def socket_leave_booth(data: dict[str, Any]) -> None:
    booth_id = data.get('booth_id')
    participant_id = data.get('participant_id')
    language = data.get('language', 'English')
    channel_id = data.get('channel_id', f'{booth_id}-audio')
    if not booth_id or not participant_id:
        return
    state = booths.leave_participant(booth_id, participant_id, language, channel_id)
    leave_room(booth_room(booth_id))
    with sid_index_lock:
        sid_index.pop(request.sid, None)
    emit_booth_state(booth_id, state)


@socketio.on('booth:chat')
def socket_chat_message(data: dict[str, Any]) -> None:
    booth_id = data.get('booth_id')
    sender_id = data.get('sender_id')
    body = data.get('body', '')
    language = data.get('language', 'English')
    channel_id = data.get('channel_id', f'{booth_id}-audio')
    if not booth_id or not sender_id:
        emit('booth:error', {'message': 'Missing sender or booth metadata.'})
        return
    try:
        message, state = booths.add_chat_message(booth_id, sender_id, body, language, channel_id)
    except ValueError as error:
        emit('booth:error', {'message': str(error)})
        return
    emit('booth:chat', message, room=booth_room(booth_id))
    emit_booth_state(booth_id, state)


@socketio.on('booth:set-active')
def socket_set_active(data: dict[str, Any]) -> None:
    booth_id = data.get('booth_id')
    requester_id = data.get('requester_id')
    target_id = data.get('target_id')
    language = data.get('language', 'English')
    channel_id = data.get('channel_id', f'{booth_id}-audio')
    if not booth_id or not requester_id or not target_id:
        emit('booth:error', {'message': 'Missing required handoff metadata.'})
        return
    previous_active_id = booths.snapshot(booth_id, language, channel_id).get('active_interpreter_id')
    try:
        state = booths.set_active_interpreter(booth_id, requester_id, target_id, language, channel_id)
    except (ValueError, PermissionError) as error:
        emit('booth:error', {'message': str(error)})
        return
    if previous_active_id and previous_active_id != target_id:
        ingest.disconnect(channel_id)
    emit_booth_state(booth_id, state)


@socketio.on('booth:update-state')
def socket_update_state(data: dict[str, Any]) -> None:
    booth_id = data.get('booth_id')
    participant_id = data.get('participant_id')
    language = data.get('language', 'English')
    channel_id = data.get('channel_id', f'{booth_id}-audio')
    if not booth_id or not participant_id:
        return
    try:
        state = booths.update_participant_state(
            booth_id,
            participant_id,
            language,
            channel_id,
            mic_active=data.get('mic_active'),
            ingest_connected=data.get('ingest_connected'),
            connected=data.get('connected'),
        )
    except ValueError as error:
        emit('booth:error', {'message': str(error)})
        return
    emit_booth_state(booth_id, state)


@socketio.on('disconnect')
def socket_disconnect() -> None:
    with sid_index_lock:
        session = sid_index.pop(request.sid, None)
    if session is None:
        return
    state = booths.leave_participant(
        session['booth_id'],
        session['participant_id'],
        session['language'],
        session['channel_id'],
    )
    emit_booth_state(session['booth_id'], state)


@atexit.register
def close_ingest() -> None:
    ingest.shutdown()


def main() -> None:
    socketio.run(app, host=settings.host, port=settings.port, debug=settings.debug)


if __name__ == '__main__':
    main()
