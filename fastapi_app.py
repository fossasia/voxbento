"""FastAPI entry point — Phase 1C: handles all coordination, templates, and REST.

Flask (app.py) is kept as a deprecated stub until Phase 1D.

Start with:
    uvicorn fastapi_app:app --host 0.0.0.0 --port 8001 --reload
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import jwt as pyjwt
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from portal.auth import create_token, decode_token, security, verify_ws_token
from portal.booth_state import BoothRegistry
from portal.config import settings
from portal.ingest import AIORTC_AVAILABLE, IngestService, IngestUnavailableError

_BASE_DIR = Path(__file__).resolve().parent

booths = BoothRegistry()
ingest = IngestService(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    ingest.shutdown()


app = FastAPI(title='Eventyay Interpretation Portal', version='1.0.0', lifespan=lifespan)
app.mount('/static', StaticFiles(directory=_BASE_DIR / 'static'), name='static')
templates = Jinja2Templates(directory=str(_BASE_DIR / 'templates'))


# ── WebSocket connection manager ──────────────────────────────────────────────

@dataclass
class Session:
    booth_id: str
    participant_id: str | None
    language: str
    channel_id: str


class ConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}
        # Keyed by id(ws) to avoid __hash__ issues with WebSocket objects
        self._sessions: dict[int, Session] = {}

    def add(self, ws: WebSocket, session: Session) -> None:
        self._rooms.setdefault(session.booth_id, set()).add(ws)
        self._sessions[id(ws)] = session

    def remove(self, ws: WebSocket) -> Session | None:
        session = self._sessions.pop(id(ws), None)
        if session:
            room = self._rooms.get(session.booth_id, set())
            room.discard(ws)
            if not room:
                self._rooms.pop(session.booth_id, None)
        return session

    def get_session(self, ws: WebSocket) -> Session | None:
        return self._sessions.get(id(ws))

    async def broadcast(self, booth_id: str, message: dict) -> None:
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in list(self._rooms.get(booth_id, set())):
            try:
                await ws.send_text(payload)
            except (RuntimeError, OSError):
                dead.append(ws)
        for ws in dead:
            self.remove(ws)


manager = ConnectionManager()


# ── Utilities ─────────────────────────────────────────────────────────────────

def _check_mediamtx() -> bool:
    if not settings.mediamtx_hls_base:
        return False
    try:
        req = urllib.request.Request(f'{settings.mediamtx_hls_base}/', method='HEAD')
        with urllib.request.urlopen(req, timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except urllib.error.URLError:
        return False


def _require_access(
    credentials: HTTPAuthorizationCredentials | None,
    token_query: str = '',
) -> None:
    """Allow request if access token is unset, or if a valid JWT or legacy token is provided."""
    if not settings.booth_access_token:
        return
    if credentials is not None:
        try:
            decode_token(credentials.credentials)
            return
        except pyjwt.InvalidTokenError:
            pass
    if token_query and token_query == settings.booth_access_token:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid or missing auth token.')


# ── Pydantic request models ───────────────────────────────────────────────────

class TokenRequest(BaseModel):
    token: str = ''


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'


class IngestConnectPayload(BaseModel):
    booth_id: str
    participant_id: str
    token: str = ''
    language: str = 'English'
    type: str
    sdp: str


class IngestDisconnectPayload(BaseModel):
    booth_id: str
    participant_id: str
    token: str = ''
    language: str = 'English'


# ── Auth endpoint ─────────────────────────────────────────────────────────────

@app.post('/api/auth/token', response_model=TokenResponse)
async def get_token(body: Annotated[TokenRequest | None, Body()] = None) -> TokenResponse:
    provided = body.token if body is not None else ''
    if settings.booth_access_token and provided != settings.booth_access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid access token.')
    return TokenResponse(access_token=create_token())


# ── Page routes ───────────────────────────────────────────────────────────────

@app.get('/')
async def home() -> RedirectResponse:
    return RedirectResponse('/interpreter/demo-booth')


@app.get('/healthz')
async def healthz() -> dict:
    return {
        'ok': True,
        'server': 'fastapi',
        'aiortc_available': AIORTC_AVAILABLE,
        'use_legacy_ingest': settings.use_legacy_ingest,
        'mediamtx_ok': _check_mediamtx(),
    }


@app.get('/interpreter/{booth_id}')
async def interpreter_booth(
    request: Request,
    booth_id: str,
    token: str = '',
    language: str = 'English',
    channel: str | None = Query(None),
) -> Any:
    channel_id = channel or f'{booth_id}-audio'
    return templates.TemplateResponse(
        request,
        'interpreter_booth.html',
        {
            'booth_id': booth_id,
            'booth_token': token,
            'booth_language': language,
            'booth_channel_id': channel_id,
            'default_jitsi_room': settings.default_jitsi_room,
            'jitsi_domain': settings.jitsi_domain,
            'aiortc_available': AIORTC_AVAILABLE,
            'mediamtx_whip_base': settings.mediamtx_whip_base,
            'mediamtx_hls_base': settings.mediamtx_hls_base,
            'use_legacy_ingest': settings.use_legacy_ingest,
        },
    )


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get('/api/booth/{booth_id}/state')
async def booth_state_api(
    booth_id: str,
    token: str = Query(''),
    language: str = 'English',
    channel: str | None = Query(None),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _require_access(credentials, token)
    channel_id = channel or f'{booth_id}-audio'
    return await booths.snapshot(booth_id, language, channel_id)


@app.post('/api/interpreter/connect/{channel_id}')
async def connect_interpreter_ingest(
    channel_id: str,
    payload: IngestConnectPayload,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _require_access(credentials, payload.token)
    if not await booths.is_active_interpreter(payload.booth_id, payload.participant_id, payload.language, channel_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='Only the active interpreter can publish ingest audio.',
        )
    try:
        answer = await asyncio.to_thread(
            ingest.connect,
            channel_id=channel_id,
            booth_id=payload.booth_id,
            participant_id=payload.participant_id,
            offer_type=payload.type,
            offer_sdp=payload.sdp,
        )
    except IngestUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    state = await booths.update_participant_state(
        payload.booth_id,
        payload.participant_id,
        payload.language,
        channel_id,
        mic_active=True,
        ingest_connected=True,
    )
    await manager.broadcast(payload.booth_id, {'type': 'booth:state', 'state': state})
    return answer


@app.post('/api/interpreter/disconnect/{channel_id}')
async def disconnect_interpreter_ingest(
    channel_id: str,
    payload: IngestDisconnectPayload,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _require_access(credentials, payload.token)
    await asyncio.to_thread(ingest.disconnect, channel_id)
    state = await booths.update_participant_state(
        payload.booth_id,
        payload.participant_id,
        payload.language,
        channel_id,
        mic_active=False,
        ingest_connected=False,
    )
    await manager.broadcast(payload.booth_id, {'type': 'booth:state', 'state': state})
    return {'ok': True}


@app.get('/api/interpreter/status/{channel_id}')
async def ingest_status_api(channel_id: str) -> dict:
    return {
        'channel_id': channel_id,
        'state': ingest.status(channel_id),
        'reachable': AIORTC_AVAILABLE,
    }


# ── WebSocket message handlers ────────────────────────────────────────────────

async def _handle_join(ws: WebSocket, session: Session, data: dict) -> None:
    display_name = data.get('display_name', 'Interpreter')
    role = data.get('role', 'interpreter')
    language = data.get('language', 'English')
    channel_id = data.get('channel_id', f'{session.booth_id}-audio')
    participant_id = data.get('participant_id')
    try:
        participant, state = await booths.join_participant(
            booth_id=session.booth_id,
            display_name=display_name,
            role=role,
            language=language,
            channel_id=channel_id,
            participant_id=participant_id,
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))
        return
    session.participant_id = participant.participant_id
    session.language = language
    session.channel_id = channel_id
    await ws.send_text(json.dumps({
        'type': 'booth:joined',
        'participant_id': participant.participant_id,
        'state': state,
    }))
    await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


async def _handle_leave(session: Session) -> None:
    if not session.participant_id:
        return
    state = await booths.leave_participant(
        session.booth_id, session.participant_id, session.language, session.channel_id,
    )
    session.participant_id = None
    await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


async def _handle_chat(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': 'Join the booth before sending messages.'}))
        return
    body = data.get('body', '')
    try:
        message, state = await booths.add_chat_message(
            session.booth_id, session.participant_id, body, session.language, session.channel_id,
        )
    except ValueError as exc:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))
        return
    await manager.broadcast(session.booth_id, {'type': 'booth:chat', 'message': message})
    await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


async def _handle_set_active(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': 'Join the booth first.'}))
        return
    target_id = data.get('target_id')
    if not target_id:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': 'Missing target_id.'}))
        return
    snap = await booths.snapshot(session.booth_id, session.language, session.channel_id)
    previous_active = snap.get('active_interpreter_id')
    try:
        state = await booths.set_active_interpreter(
            session.booth_id, session.participant_id, target_id, session.language, session.channel_id,
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))
        return
    if previous_active and previous_active != target_id:
        await asyncio.to_thread(ingest.disconnect, session.channel_id)
    await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


async def _handle_update_state(ws: WebSocket, session: Session, data: dict) -> None:
    if not session.participant_id:
        return
    try:
        state = await booths.update_participant_state(
            session.booth_id,
            session.participant_id,
            session.language,
            session.channel_id,
            mic_active=data.get('mic_active'),
            ingest_connected=data.get('ingest_connected'),
            connected=data.get('connected'),
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))
        return
    await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket('/ws/booth/{booth_id}')
async def ws_booth(websocket: WebSocket, booth_id: str) -> None:
    try:
        await verify_ws_token(websocket)
    except ValueError:
        return

    await websocket.accept()
    session = Session(
        booth_id=booth_id,
        participant_id=None,
        language='English',
        channel_id=f'{booth_id}-audio',
    )
    manager.add(websocket, session)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({'type': 'booth:error', 'message': 'Invalid JSON.'}))
                continue

            msg_type = data.get('type', '')
            if msg_type == 'booth:join':
                await _handle_join(websocket, session, data)
            elif msg_type == 'booth:leave':
                await _handle_leave(session)
                break
            elif msg_type == 'booth:chat':
                await _handle_chat(websocket, session, data)
            elif msg_type == 'booth:set-active':
                await _handle_set_active(websocket, session, data)
            elif msg_type == 'booth:update-state':
                await _handle_update_state(websocket, session, data)
            else:
                await websocket.send_text(
                    json.dumps({'type': 'booth:error', 'message': f'Unknown message type: {msg_type}'})
                )
    except WebSocketDisconnect:
        pass
    finally:
        manager.remove(websocket)
        if session.participant_id:
            state = await booths.leave_participant(
                session.booth_id, session.participant_id, session.language, session.channel_id,
            )
            await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


def main() -> None:
    import uvicorn
    uvicorn.run('fastapi_app:app', host=settings.host, port=8001, reload=settings.debug)


if __name__ == '__main__':
    main()
