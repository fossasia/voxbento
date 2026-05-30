"""FastAPI entry point — sole backend for the Eventyay Interpretation Portal.

Start with:
    uvicorn fastapi_app:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx
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

_BASE_DIR = Path(__file__).resolve().parent
# Appended to static JS URLs so the browser always fetches fresh JS after
# a server restart (prevents stale-cache issues during development).
_JS_CACHE_BUST = str(int(time.time()))

booths = BoothRegistry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


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

def _make_jitsi_url(base_url: str, room: str) -> str:
    """Return a full Jitsi meeting URL.

    If *room* is already an absolute URL it is returned unchanged, so
    existing deployments that stored a full URL in DEFAULT_JITSI_ROOM
    are not broken by the base-URL prefix.
    """
    if room.startswith(('http://', 'https://')):
        return room
    return f'{base_url.rstrip("/")}/{room.lstrip("/")}'


async def _check_mediamtx() -> bool:
    """Non-blocking reachability check for MediaMTX HLS endpoint."""
    base = settings.effective_mediamtx_internal_base
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.head(f'{base}/')
        return r.status_code < 500
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
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
        'mediamtx_ok': await _check_mediamtx(),
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
            'default_jitsi_url': _make_jitsi_url(
                settings.effective_jitsi_base_url, settings.default_jitsi_room
            ),
            'jitsi_domain': settings.effective_jitsi_domain,
            'jitsi_base_url': settings.effective_jitsi_base_url,
            'mediamtx_whip_base': settings.mediamtx_whip_base,
            'mediamtx_hls_base': settings.mediamtx_hls_base,
            'js_version': _JS_CACHE_BUST,
        },
    )


@app.get('/listen/{booth_id}')
async def listen_booth(
    request: Request,
    booth_id: str,
    language: str = 'English',
    channel: str | None = Query(None),
) -> Any:
    """Listener page with hls.js auto-recovery for seamless handoff."""
    channel_id = channel or f'{booth_id}-audio'
    hls_url = f'{settings.mediamtx_hls_base}/{channel_id}/index.m3u8'
    return templates.TemplateResponse(
        request,
        'listener.html',
        {
            'booth_id': booth_id,
            'language': language,
            'channel_id': channel_id,
            'hls_url': hls_url,
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



@app.get('/api/interpreter/status/{channel_id}')
async def ingest_status_api(channel_id: str) -> dict:
    """Returns MediaMTX reachability — used by the frontend preflight check."""
    return {
        'channel_id': channel_id,
        'state': 'mediamtx',
        'reachable': await _check_mediamtx(),
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
        pass  # client is responsible for stopping its own ingest (WHIP teardown)
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
    uvicorn.run('fastapi_app:app', host=settings.host, port=settings.port, reload=settings.debug)


if __name__ == '__main__':
    main()
