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

from portal.auth import (
    create_admin_token, create_participant_token, create_token, create_user_token,
    decode_token, get_booth_session, get_current_user, hash_password, require_admin,
    require_user, resolve_booth_role, can_perform_role,
    security, verify_password, verify_ws_token,
)
from portal.booth_identity import make_booth_id, make_mediamtx_path
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
    # Role derived from the bearer cookie at WS connection time — never from client data.
    granted_role: str | None = None


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


# Track which channel paths have already been created this process lifetime
# to avoid redundant API calls on every page load.
_created_paths: set[str] = set()


async def _ensure_mediamtx_path(channel_id: str) -> None:
    """Create a named MediaMTX path with alwaysAvailable if it doesn't exist.

    alwaysAvailable keeps the stream alive during publisher handoffs so WHEP
    readers don't get disconnected.  This cannot be set on the wildcard
    all_others path, so we create named paths via the Control API.

    Uses ADD first; if the path already exists, PATCHes it to ensure
    alwaysAvailable is set (handles MediaMTX restarts where the runtime
    config was lost while the portal's in-memory cache was stale).
    """
    if channel_id in _created_paths:
        return
    api_base = settings.mediamtx_api_base
    if not api_base:
        return
    body = {
        'alwaysAvailable': True,
        'alwaysAvailableTracks': [{'codec': 'Opus'}],
        'overridePublisher': True,
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.post(
                f'{api_base}/v3/config/paths/add/{channel_id}', json=body,
            )
            if r.status_code == 200:
                _created_paths.add(channel_id)
            elif r.status_code == 400 and 'already exists' in r.text.lower():
                # Path exists but may lack alwaysAvailable — patch it
                r2 = await client.patch(
                    f'{api_base}/v3/config/paths/patch/{channel_id}', json=body,
                )
                if r2.status_code == 200:
                    _created_paths.add(channel_id)
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        pass  # Non-fatal; path will use all_others defaults


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


async def _resolve_whip_url(booth_id: str, participant_id: str, language: str, channel_id: str) -> dict:
    """Check publish permission and return the WHIP URL payload.

    Shared by both the legacy ``/api/booth/{id}/whip-url`` endpoint and the
    event-scoped ``/api/events/{slug}/booths/{lang}/whip-url`` endpoint.
    Raises :class:`HTTPException` on permission/lookup failures.
    """
    try:
        await booths.check_publish_permission(booth_id, participant_id, language, channel_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    await _ensure_mediamtx_path(channel_id)
    whip_url = f'{settings.mediamtx_whip_base}/{channel_id}/whip'
    return {'whip_url': whip_url, 'channel_id': channel_id, 'booth_id': booth_id}


# ── Pydantic request models ───────────────────────────────────────────────────

class TokenRequest(BaseModel):
    token: str = ''


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = 'bearer'


class CreateBoothRequest(BaseModel):
    language_code: str
    language: str = ''
    room_id: int | None = None
    instance: str = 'primary'


# ── Auth endpoint ─────────────────────────────────────────────────────────────

@app.post('/api/auth/token', response_model=TokenResponse)
async def get_token(body: Annotated[TokenRequest | None, Body()] = None) -> TokenResponse:
    provided = body.token if body is not None else ''
    if settings.booth_access_token and provided != settings.booth_access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid access token.')
    return TokenResponse(access_token=create_token())


# ── Invite-token join flow ────────────────────────────────────────────────────

@app.get('/join/{token}')
async def join_via_invite(token: str) -> RedirectResponse:
    """Validate an invite token, issue a JWT cookie, and redirect to the booth.

    Flow:
    1. Look up the token in the database
    2. Validate: not expired, not already used
    3. Mark the token as used (``used_at = now``)
    4. Issue a signed JWT cookie with role claims
    5. Redirect to ``/interpreter/{event_slug}/{language_code}``
    """
    from portal.database import get_session, redeem_invite_token

    async with get_session() as session:
        try:
            tok = await redeem_invite_token(session, token)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    if tok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Invalid invite token.')

    jwt_token = create_participant_token(
        booth_id=tok.booth_id,
        role=tok.role,
        event_slug=tok.booth.event.slug,
        language_code=tok.booth.language_code,
    )

    redirect_url = f'/interpreter/{tok.booth.event.slug}/{tok.booth.language_code}'
    response = RedirectResponse(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key='session_token',
        value=jwt_token,
        httponly=True,
        samesite='lax',
        max_age=settings.jwt_expiry_seconds,
    )
    return response


# ── Page routes ───────────────────────────────────────────────────────────────

@app.get('/')
async def home(request: Request):
    from portal.database import get_session, list_events, list_booths_for_event, list_booth_memberships_for_user, list_memberships_for_user

    current_user = await get_current_user(request)
    my_booths = []
    
    try:
        async with get_session() as session:
            events = await list_events(session)
            
            user_event_roles = {}
            user_booth_roles = {}
            if current_user:
                uid = int(current_user['sub'])
                ems = await list_memberships_for_user(session, uid)
                user_event_roles = {em.event_id: em.role for em in ems}
                
                bms = await list_booth_memberships_for_user(session, uid)
                user_booth_roles = {bm.booth_id: bm.role for bm in bms}
                for bm in bms:
                    bid = make_booth_id(bm.booth.event.slug, bm.booth.language_code)
                    mem_booth = booths.get_booth_sync(bid)
                    is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'
                    my_booths.append({
                        'membership': bm,
                        'booth_id': bid,
                        'is_live': is_live,
                        'event_name': bm.booth.event.display_name,
                        'language_name': bm.booth.language_name,
                        'event_slug': bm.booth.event.slug,
                        'language_code': bm.booth.language_code,
                    })
                    
            event_data = []
            for ev in events:
                db_booths = await list_booths_for_event(session, ev.id)
                booth_statuses = []
                for b in db_booths:
                    bid = make_booth_id(ev.slug, b.language_code)
                    mem_booth = booths.get_booth_sync(bid)
                    is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'
                    
                    can_interpret = False
                    if current_user:
                        is_admin = current_user.get('is_admin', False)
                        ev_role = user_event_roles.get(ev.id)
                        booth_role = user_booth_roles.get(b.id)
                        if is_admin or ev_role in ('interpreter', 'coordinator', 'event_admin') or booth_role in ('interpreter', 'coordinator'):
                            can_interpret = True
                            
                    booth_statuses.append({
                        'db': b, 
                        'booth_id': bid, 
                        'is_live': is_live,
                        'can_interpret': can_interpret
                    })
                event_data.append({
                    'event': ev,
                    'booths': booth_statuses,
                    'live_count': sum(1 for bs in booth_statuses if bs['is_live']),
                })
    except Exception as _exc:
        import logging
        logging.getLogger(__name__).warning('home() DB error: %s', _exc, exc_info=True)
        event_data = []

    return templates.TemplateResponse(request, 'home.html', {
        'events': event_data,
        'current_user': current_user,
        'my_booths': my_booths,
    })


@app.get('/healthz')
async def healthz() -> dict:
    return {
        'ok': True,
        'server': 'fastapi',
        'mediamtx_ok': await _check_mediamtx(),
    }


@app.get('/interpreter/{event_slug}/{language_code}')
async def interpreter_booth_by_identity(
    request: Request,
    event_slug: str,
    language_code: str,
    token: str = '',
    language: str = '',
) -> Any:
    """Booth page addressed by event_slug and language_code (preferred URL).

    Requires a valid session_token (invite link) or user_token (registered user).
    The granted role is passed to the template so the client cannot self-promote.
    """
    payload = get_booth_session(request)
    if payload is None:
        return RedirectResponse(
            url=f'/login?next=/interpreter/{event_slug}/{language_code}',
            status_code=status.HTTP_303_SEE_OTHER,
        )

    granted_role = resolve_booth_role(payload)

    # is_admin flag in JWT always grants event_admin — no DB lookup needed.
    if granted_role is None and payload.get('is_admin'):
        granted_role = 'event_admin'

    # If role is still None the user is registered but has no role claim in the JWT.
    # Check BoothMembership from the DB for this exact booth first,
    # then fallback to EventMembership (for coordinators/event_admins).
    if granted_role is None and payload.get('sub'):
        from portal.database import get_session, list_memberships_for_user, list_booth_memberships_for_user
        try:
            async with get_session() as db_session:
                # 1. Booth-level membership (e.g. interpreter assigned to this specific booth)
                bms = await list_booth_memberships_for_user(db_session, int(payload['sub']))
                for bm in bms:
                    if bm.booth.event.slug == event_slug and bm.booth.language_code == language_code:
                        granted_role = bm.role
                        break

                # 2. Event-level membership (coordinator, event_admin assigned to the event)
                if granted_role is None:
                    memberships = await list_memberships_for_user(db_session, int(payload['sub']))
                    for m in memberships:
                        if m.event and m.event.slug == event_slug:
                            granted_role = m.role
                            break
        except Exception:
            pass

    if granted_role is None:
        # User is authenticated but has no role for this event
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='You do not have a role assigned for this event. Ask an admin to assign you one.',
        )

    booth_id = make_booth_id(event_slug, language_code)
    mediamtx_path = make_mediamtx_path(event_slug, language_code)
    channel_id = mediamtx_path
    display_language = language or language_code.upper()
    await _ensure_mediamtx_path(channel_id)
    whip_url = f'{settings.mediamtx_whip_base}/{mediamtx_path}/whip'
    whep_url = f'{settings.mediamtx_whip_base}/{mediamtx_path}/whep'
    return templates.TemplateResponse(
        request,
        'interpreter_booth.html',
        {
            'booth_id': booth_id,
            'booth_token': token,
            'booth_language': display_language,
            'booth_channel_id': channel_id,
            'event_slug': event_slug,
            'language_code': language_code,
            'whip_url': whip_url,
            'whep_url': whep_url,
            'granted_role': granted_role,
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


@app.get('/interpreter/{booth_id}')
async def interpreter_booth(
    request: Request,
    booth_id: str,
    token: str = '',
    language: str = 'English',
    channel: str | None = Query(None),
) -> Any:
    """Legacy booth URL (no event scope). Requires a valid session."""
    payload = get_booth_session(request)
    if payload is None:
        return RedirectResponse(
            url=f'/login?next=/interpreter/{booth_id}',
            status_code=status.HTTP_303_SEE_OTHER,
        )

    granted_role = resolve_booth_role(payload)
    if granted_role is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='You do not have a role assigned. Ask an admin for an invite link.',
        )

    if channel:
        channel_id = channel
    else:
        try:
            from portal.booth_identity import booth_id_to_mediamtx_path
            channel_id = booth_id_to_mediamtx_path(booth_id)
        except ValueError:
            channel_id = f'{booth_id}-audio'

    await _ensure_mediamtx_path(channel_id)
    return templates.TemplateResponse(
        request,
        'interpreter_booth.html',
        {
            'booth_id': booth_id,
            'booth_token': token,
            'booth_language': language,
            'booth_channel_id': channel_id,
            'granted_role': granted_role,
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
    payload = get_booth_session(request)
    if payload is None:
        return RedirectResponse(
            url=f'/login?next=/listen/{booth_id}',
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if channel:
        channel_id = channel
    else:
        try:
            from portal.booth_identity import booth_id_to_mediamtx_path
            channel_id = booth_id_to_mediamtx_path(booth_id)
        except ValueError:
            channel_id = f'{booth_id}-audio'

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


@app.get('/listener-webrtc/{booth_id}')
async def listen_webrtc_booth(
    request: Request,
    booth_id: str,
    language: str = 'English',
    channel: str | None = Query(None),
) -> Any:
    """Listener page using WHEP/WebRTC for low-latency playback."""
    payload = get_booth_session(request)
    if payload is None:
        return RedirectResponse(
            url=f'/login?next=/listener-webrtc/{booth_id}',
            status_code=status.HTTP_303_SEE_OTHER,
        )
    if channel:
        channel_id = channel
    else:
        try:
            from portal.booth_identity import booth_id_to_mediamtx_path
            channel_id = booth_id_to_mediamtx_path(booth_id)
        except ValueError:
            channel_id = f'{booth_id}-audio'

    await _ensure_mediamtx_path(channel_id)
    whep_url = f'{settings.mediamtx_whip_base}/{channel_id}/whep'
    return templates.TemplateResponse(
        request,
        'listener-webrtc.html',
        {
            'booth_id': booth_id,
            'language': language,
            'channel_id': channel_id,
            'whep_url': whep_url,
            'js_version': _JS_CACHE_BUST,
        },
    )


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get('/api/booth/{booth_id}/state')
async def booth_state_api(
    booth_id: str,
    token: str = Query(''),
    language: str = 'English',
    channel: str | None = Query(None),
    room: int | None = Query(None),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    _require_access(credentials, token)
    channel_id = channel or f'{booth_id}-audio'
    return await booths.snapshot(booth_id, language, channel_id, room_id=room)


@app.get('/api/booth/{booth_id}/whip-url')
async def booth_whip_url(
    booth_id: str,
    participant_id: str = Query(...),
    token: str = Query(''),
    language: str = 'English',
    channel: str | None = Query(None),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Return the WHIP ingest URL only if the caller is the active interpreter.

    Layer 2 enforcement: the browser must call this endpoint before starting
    a WHIP session. Non-active interpreters and non-interpreter roles receive
    a 403 response and never learn the WHIP URL.
    """
    _require_access(credentials, token)
    channel_id = channel or f'{booth_id}-audio'
    return await _resolve_whip_url(booth_id, participant_id, language, channel_id)


@app.post('/api/events/{event_slug}/booths', status_code=status.HTTP_201_CREATED)
async def create_event_booth(
    event_slug: str,
    body: CreateBoothRequest,
    token: str = Query(''),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Create a booth for an event.

    Returns the booth state including derived booth_id, MediaMTX path,
    WHIP URL, and WHEP URL.
    """
    _require_access(credentials, token)
    try:
        state = await booths.create_booth(
            event_slug=event_slug,
            language_code=body.language_code,
            language=body.language or body.language_code.upper(),
            instance=body.instance,
            room_id=body.room_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    mediamtx_path = state['mediamtx_path']
    await _ensure_mediamtx_path(mediamtx_path)
    state['whip_url'] = f'{settings.mediamtx_whip_base}/{mediamtx_path}/whip'
    state['whep_url'] = f'{settings.mediamtx_whip_base}/{mediamtx_path}/whep'
    return state


@app.get('/api/events/{event_slug}/booths')
async def list_event_booths(
    event_slug: str,
    token: str = Query(''),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """List all booths for an event."""
    _require_access(credentials, token)
    booth_list = await booths.list_booths_for_event(event_slug)
    for b in booth_list:
        mtx = b.get('mediamtx_path', '')
        if mtx:
            b['whip_url'] = f'{settings.mediamtx_whip_base}/{mtx}/whip'
            b['whep_url'] = f'{settings.mediamtx_whip_base}/{mtx}/whep'
    return {'event_slug': event_slug, 'booths': booth_list}


@app.get('/api/events/{event_slug}/booths/{language_code}/state')
async def event_booth_state(
    event_slug: str,
    language_code: str,
    token: str = Query(''),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Event-scoped booth state — never auto-creates a booth.

    Returns 404 if the booth does not exist for this event.
    """
    _require_access(credentials, token)
    state = await booths.get_booth_for_event(event_slug, language_code)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No booth for language '{language_code}' in event '{event_slug}'.",
        )
    return state


@app.get('/api/events/{event_slug}/booths/{language_code}/whip-url')
async def event_booth_whip_url(
    event_slug: str,
    language_code: str,
    participant_id: str = Query(...),
    token: str = Query(''),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Event-scoped WHIP URL — validates event ownership before returning.

    Combines event namespace isolation (booth must belong to event) with
    Layer 2 active-interpreter enforcement.
    """
    _require_access(credentials, token)
    booth_id = make_booth_id(event_slug, language_code)
    channel_id = make_mediamtx_path(event_slug, language_code)
    try:
        await booths.validate_booth_event(booth_id, event_slug)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return await _resolve_whip_url(booth_id, participant_id, language_code.upper(), channel_id)


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

    # Role enforcement: use the server-derived granted_role stored on the session
    # (populated from cookies at WS connect time). Never trust data['role'].
    if session.granted_role is None:
        await ws.send_text(json.dumps({
            'type': 'booth:error',
            'message': 'No role assigned for this session.',
        }))
        return
    if not can_perform_role(session.granted_role, role):
        await ws.send_text(json.dumps({
            'type': 'booth:error',
            'message': f'Your assigned role ({session.granted_role}) does not permit joining as {role}.',
        }))
        return

    # Cross-event isolation: reject if client-supplied event_slug doesn't match
    client_event = data.get('event_slug')
    if client_event is not None:
        try:
            await booths.validate_booth_event(session.booth_id, client_event)
        except PermissionError as exc:
            await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))
            return
    room_id = data.get('room_id')
    if room_id is not None:
        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            await ws.send_text(json.dumps({'type': 'booth:error', 'message': 'room_id must be an integer.'}))
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


# ── User registration & login routes ─────────────────────────────────────────


@app.get('/register')
async def register_page(request: Request):
    current_user = await get_current_user(request)
    if current_user:
        return RedirectResponse(url='/account', status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, 'register.html', {})


@app.post('/register')
async def register_submit(request: Request):
    from portal.database import create_user, get_session, get_user_by_email

    form = await request.form()
    email = form.get('email', '').strip().lower()
    display_name = form.get('display_name', '').strip()
    password = form.get('password', '')
    password_confirm = form.get('password_confirm', '')

    errors = []
    if not email or '@' not in email:
        errors.append('Valid email is required.')
    if not display_name:
        errors.append('Display name is required.')
    if len(password) < 8:
        errors.append('Password must be at least 8 characters.')
    if password != password_confirm:
        errors.append('Passwords do not match.')

    if not errors:
        async with get_session() as session:
            existing = await get_user_by_email(session, email)
            if existing:
                errors.append('An account with this email already exists.')
            else:
                pw_hash = hash_password(password)
                user = await create_user(
                    session, email=email, display_name=display_name,
                    password_hash=pw_hash,
                )
                token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
                response = RedirectResponse(url='/account', status_code=status.HTTP_303_SEE_OTHER)
                response.set_cookie(
                    key='user_token', value=token,
                    httponly=True, samesite='lax', max_age=settings.jwt_expiry_seconds,
                )
                return response

    return templates.TemplateResponse(
        request, 'register.html',
        {'errors': errors, 'email': email, 'display_name': display_name},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@app.get('/login')
async def user_login_page(request: Request):
    current_user = await get_current_user(request)
    if current_user:
        return RedirectResponse(url='/account', status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, 'login.html', {})


@app.post('/login')
async def user_login_submit(request: Request):
    from portal.database import get_session, get_user_by_email

    form = await request.form()
    email = form.get('email', '').strip().lower()
    password = form.get('password', '')

    async with get_session() as session:
        user = await get_user_by_email(session, email)

    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, 'login.html',
            {'error': 'Invalid email or password.', 'email': email},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            request, 'login.html',
            {'error': 'Your account has been deactivated. Contact an admin.', 'email': email},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    token = create_user_token(user_id=user.id, email=user.email, is_admin=user.is_admin)
    response = RedirectResponse(url='/account', status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key='user_token', value=token,
        httponly=True, samesite='lax', max_age=settings.jwt_expiry_seconds,
    )
    return response


@app.get('/logout')
async def user_logout(request: Request):
    response = RedirectResponse(url='/', status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie('user_token')
    return response


@app.get('/account')
async def account_page(request: Request):
    from portal.database import get_session, get_user_by_id, list_memberships_for_user

    current_user = await get_current_user(request)
    if current_user is None:
        return RedirectResponse(url='/login', status_code=status.HTTP_303_SEE_OTHER)

    async with get_session() as session:
        user = await get_user_by_id(session, int(current_user['sub']))
        if user is None:
            response = RedirectResponse(url='/login', status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie('user_token')
            return response
        memberships = await list_memberships_for_user(session, user.id)

    return templates.TemplateResponse(request, 'account.html', {'user': user, 'memberships': memberships})


# ── Admin panel routes ────────────────────────────────────────────────────────


@app.get('/admin/login')
async def admin_login_page(request: Request):
    user = await get_current_user(request)
    if user and user.get('is_admin'):
        return RedirectResponse(url='/admin/', status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, 'admin/login.html', {})


@app.post('/admin/login')
async def admin_login_submit(request: Request):
    form = await request.form()
    password = form.get('password', '')
    if not settings.admin_password or password != settings.admin_password:
        return templates.TemplateResponse(
            request,
            'admin/login.html',
            {'error': 'Invalid password.'},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    token = create_admin_token()
    response = RedirectResponse(url='/admin/', status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key='admin_token', value=token,
        httponly=True, samesite='lax', max_age=settings.jwt_expiry_seconds,
    )
    return response


@app.get('/admin/logout')
async def admin_logout():
    response = RedirectResponse(url='/admin/login', status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie('admin_token')
    return response


@app.get('/admin/', dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request):
    from portal.database import get_session, list_events, list_booths_for_event

    async with get_session() as session:
        events = await list_events(session)
        event_data = []
        for ev in events:
            db_booths = await list_booths_for_event(session, ev.id)
            booth_statuses = []
            for b in db_booths:
                booth_id = make_booth_id(ev.slug, b.language_code)
                mem_booth = booths.get_booth_sync(booth_id)
                is_live = (
                    mem_booth is not None
                    and mem_booth.ingest_status == 'connected'
                )
                booth_statuses.append({
                    'db': b,
                    'booth_id': booth_id,
                    'is_live': is_live,
                })
            event_data.append({
                'event': ev,
                'booths': booth_statuses,
                'live_count': sum(1 for bs in booth_statuses if bs['is_live']),
                'total_booths': len(booth_statuses),
            })

    mediamtx_ok = await _check_mediamtx()
    return templates.TemplateResponse(request, 'admin/dashboard.html', {
        'event_data': event_data,
        'mediamtx_ok': mediamtx_ok,
    })


@app.get('/admin/events/', dependencies=[Depends(require_admin)])
async def admin_event_list(request: Request):
    from portal.database import get_session, list_events

    async with get_session() as session:
        events = await list_events(session)
    return templates.TemplateResponse(request, 'admin/event_list.html', {
        'events': events,
    })


@app.post('/admin/events/', dependencies=[Depends(require_admin)])
async def admin_create_event(request: Request):
    from portal.database import get_session, create_event

    form = await request.form()
    slug = form.get('slug', '').strip()
    display_name = form.get('display_name', '').strip()
    if not slug or not display_name:
        return RedirectResponse(url='/admin/events/', status_code=status.HTTP_303_SEE_OTHER)
    try:
        async with get_session() as session:
            await create_event(session, slug=slug, display_name=display_name)
    except Exception:
        return RedirectResponse(url='/admin/events/', status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url='/admin/events/', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/admin/events/{event_id}/', dependencies=[Depends(require_admin)])
async def admin_event_detail(request: Request, event_id: int):
    from portal.database import (
        get_session, get_event_by_id, list_rooms_for_event, list_booths_for_event,
    )

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')
        rooms = await list_rooms_for_event(session, event_id)
        db_booths = await list_booths_for_event(session, event_id)

    booth_statuses = []
    for b in db_booths:
        bid = make_booth_id(event.slug, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'
        booth_statuses.append({'db': b, 'booth_id': bid, 'is_live': is_live})

    return templates.TemplateResponse(request, 'admin/event_detail.html', {
        'event': event,
        'rooms': rooms,
        'booths': booth_statuses,
        'live_count': sum(1 for bs in booth_statuses if bs['is_live']),
    })


@app.post('/admin/events/{event_id}/delete', dependencies=[Depends(require_admin)])
async def admin_delete_event(request: Request, event_id: int):
    from portal.database import get_session, delete_event

    async with get_session() as session:
        await delete_event(session, event_id)
    return RedirectResponse(url='/admin/events/', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/admin/events/{event_id}/rooms/', dependencies=[Depends(require_admin)])
async def admin_room_list(request: Request, event_id: int):
    from portal.database import (
        get_session, get_event_by_id, list_rooms_for_event, list_booths_for_room,
    )

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')
        rooms = await list_rooms_for_event(session, event_id)
        room_data = []
        for room in rooms:
            room_booths = await list_booths_for_room(session, room.id)
            room_data.append({'room': room, 'booth_count': len(room_booths)})
    return templates.TemplateResponse(request, 'admin/room_list.html', {
        'event': event,
        'room_data': room_data,
    })


@app.post('/admin/events/{event_id}/rooms/', dependencies=[Depends(require_admin)])
async def admin_create_room(request: Request, event_id: int):
    from portal.database import get_session, create_room

    form = await request.form()
    display_name = form.get('display_name', '').strip()
    if not display_name:
        return RedirectResponse(
            url=f'/admin/events/{event_id}/rooms/',
            status_code=status.HTTP_303_SEE_OTHER,
        )
    async with get_session() as session:
        await create_room(session, event_id=event_id, display_name=display_name)
    return RedirectResponse(
        url=f'/admin/events/{event_id}/rooms/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get('/admin/events/{event_id}/rooms/{room_id}/', dependencies=[Depends(require_admin)])
async def admin_room_detail(request: Request, event_id: int, room_id: int):
    from portal.database import (
        get_session, get_event_by_id, get_room_by_id, list_booths_for_room,
    )

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail='Room not found.')
        db_booths = await list_booths_for_room(session, room_id)

    booth_statuses = []
    for b in db_booths:
        bid = make_booth_id(event.slug, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'
        booth_statuses.append({'db': b, 'booth_id': bid, 'is_live': is_live})

    return templates.TemplateResponse(request, 'admin/room_detail.html', {
        'event': event,
        'room': room,
        'booths': booth_statuses,
    })


@app.post('/admin/events/{event_id}/rooms/{room_id}/delete', dependencies=[Depends(require_admin)])
async def admin_delete_room(request: Request, event_id: int, room_id: int):
    from portal.database import get_session, delete_room

    async with get_session() as session:
        await delete_room(session, room_id)
    return RedirectResponse(
        url=f'/admin/events/{event_id}/rooms/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get('/admin/events/{event_id}/rooms/{room_id}/booths/', dependencies=[Depends(require_admin)])
async def admin_booth_list(request: Request, event_id: int, room_id: int):
    from portal.database import (
        get_session, get_event_by_id, get_room_by_id, list_booths_for_room,
    )

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail='Room not found.')
        db_booths = await list_booths_for_room(session, room_id)

    booth_statuses = []
    for b in db_booths:
        bid = make_booth_id(event.slug, b.language_code)
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'
        booth_statuses.append({'db': b, 'booth_id': bid, 'is_live': is_live})

    return templates.TemplateResponse(request, 'admin/booth_list.html', {
        'event': event,
        'room': room,
        'booths': booth_statuses,
    })


@app.post('/admin/events/{event_id}/rooms/{room_id}/booths/', dependencies=[Depends(require_admin)])
async def admin_create_booth(request: Request, event_id: int, room_id: int):
    from portal.database import get_session, create_booth

    form = await request.form()
    language_code = form.get('language_code', '').strip().lower()
    language_name = form.get('language_name', '').strip()
    if not language_code or not language_name:
        return RedirectResponse(
            url=f'/admin/events/{event_id}/rooms/{room_id}/booths/',
            status_code=status.HTTP_303_SEE_OTHER,
        )
    try:
        async with get_session() as session:
            await create_booth(
                session, event_id=event_id, room_id=room_id,
                language_code=language_code, language_name=language_name,
            )
    except Exception:
        pass
    return RedirectResponse(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
    dependencies=[Depends(require_admin)],
)
async def admin_booth_detail(request: Request, event_id: int, room_id: int, booth_id: int):
    from portal.database import (
        get_session, get_event_by_id, get_room_by_id, get_booth_by_id,
        list_tokens_for_booth, list_users, list_memberships_for_booth
    )

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')
        room = await get_room_by_id(session, room_id)
        if room is None or room.event_id != event_id:
            raise HTTPException(status_code=404, detail='Room not found.')
        db_booth = await get_booth_by_id(session, booth_id)
        if db_booth is None or db_booth.room_id != room_id:
            raise HTTPException(status_code=404, detail='Booth not found.')
        tokens = await list_tokens_for_booth(session, booth_id)
        users = await list_users(session)
        memberships = await list_memberships_for_booth(session, booth_id)

    membership_map = {m.user_id: m for m in memberships}

    bid = make_booth_id(event.slug, db_booth.language_code)
    mediamtx_path = make_mediamtx_path(event.slug, db_booth.language_code)
    whep_url = f'{settings.mediamtx_whip_base}/{mediamtx_path}/whep'
    mem_booth = booths.get_booth_sync(bid)
    is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'
    participants = list(mem_booth.participants.values()) if mem_booth else []
    active_interpreter = None
    if mem_booth and mem_booth.active_interpreter_id:
        active_interpreter = mem_booth.participants.get(mem_booth.active_interpreter_id)

    return templates.TemplateResponse(request, 'admin/booth_detail.html', {
        'event': event,
        'room': room,
        'booth': db_booth,
        'booth_id': bid,
        'mediamtx_path': mediamtx_path,
        'whep_url': whep_url,
        'is_live': is_live,
        'participants': participants,
        'active_interpreter': active_interpreter,
        'tokens': tokens,
        'users': users,
        'memberships': memberships,
        'membership_map': membership_map,
    })


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/members/',
    dependencies=[Depends(require_admin)],
)
async def admin_add_booth_member(request: Request, event_id: int, room_id: int, booth_id: int):
    from portal.database import get_session, list_memberships_for_booth, remove_booth_membership, set_booth_membership, get_user_by_email

    form = await request.form()
    email = form.get('email', '').strip()
    role = form.get('role', '').strip()
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                return RedirectResponse(
                    url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/?error=user_not_found',
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            uid = user.id
            if role:
                await set_booth_membership(session, user_id=uid, booth_id=booth_id, role=role)
            else:
                # "— none —" selected: remove any existing membership
                memberships = await list_memberships_for_booth(session, booth_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_booth_membership(session, m.id)
                        break
    return RedirectResponse(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/members/{membership_id}/delete',
    dependencies=[Depends(require_admin)],
)
async def admin_remove_booth_member(request: Request, event_id: int, room_id: int, booth_id: int, membership_id: int):
    from portal.database import get_session, remove_booth_membership

    async with get_session() as session:
        await remove_booth_membership(session, membership_id)
    return RedirectResponse(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/delete',
    dependencies=[Depends(require_admin)],
)
async def admin_delete_booth(request: Request, event_id: int, room_id: int, booth_id: int):
    from portal.database import get_session, delete_booth

    async with get_session() as session:
        await delete_booth(session, booth_id)
    return RedirectResponse(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── Admin user management routes ─────────────────────────────────────────────


@app.get('/admin/users/', dependencies=[Depends(require_admin)])
async def admin_user_list(request: Request):
    from portal.database import get_session, list_users

    async with get_session() as session:
        users = await list_users(session)
    return templates.TemplateResponse(request, 'admin/user_list.html', {'users': users})


@app.post('/admin/users/{user_id}/toggle-active', dependencies=[Depends(require_admin)])
async def admin_toggle_user_active(request: Request, user_id: int):
    from portal.database import get_session, get_user_by_id, update_user_active

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            await update_user_active(session, user_id, is_active=not user.is_active)
    return RedirectResponse(url='/admin/users/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/admin/users/{user_id}/delete', dependencies=[Depends(require_admin)])
async def admin_delete_user(request: Request, user_id: int):
    from portal.database import get_session, delete_user

    async with get_session() as session:
        await delete_user(session, user_id)
    return RedirectResponse(url='/admin/users/', status_code=status.HTTP_303_SEE_OTHER)

@app.get('/admin/users/{user_id}/', dependencies=[Depends(require_admin)])
async def admin_user_detail(request: Request, user_id: int):
    from portal.database import get_session, get_user_by_id, list_events, list_memberships_for_user

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if not user:
            return RedirectResponse(url='/admin/users/', status_code=status.HTTP_303_SEE_OTHER)
        
        events = await list_events(session)
        memberships = await list_memberships_for_user(session, user_id)
        
        event_admin_map = {m.event_id: m for m in memberships if m.role == 'event_admin'}
        
    return templates.TemplateResponse(request, 'admin/user_detail.html', {
        'user_detail': user,  # Named 'user_detail' so it doesn't clash with context 'user'
        'events': events,
        'event_admin_map': event_admin_map
    })


@app.post('/admin/users/{user_id}/toggle-admin', dependencies=[Depends(require_admin)])
async def admin_toggle_user_admin(request: Request, user_id: int):
    from portal.database import get_session, get_user_by_id
    from sqlalchemy import update
    from portal.models import User

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            stmt = update(User).where(User.id == user_id).values(is_admin=not user.is_admin)
            await session.execute(stmt)
            await session.commit()
    return RedirectResponse(url=f'/admin/users/{user_id}/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/admin/users/{user_id}/events/{event_id}/toggle-admin', dependencies=[Depends(require_admin)])
async def admin_toggle_user_event_admin(request: Request, user_id: int, event_id: int):
    from portal.database import get_session, get_user_by_id, list_memberships_for_user, set_event_membership, remove_event_membership

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            memberships = await list_memberships_for_user(session, user_id)
            event_admin_membership = next((m for m in memberships if m.event_id == event_id and m.role == 'event_admin'), None)
            
            if event_admin_membership:
                await remove_event_membership(session, event_admin_membership.id)
            else:
                await set_event_membership(session, user_id=user_id, event_id=event_id, role='event_admin')
                
    return RedirectResponse(url=f'/admin/users/{user_id}/', status_code=status.HTTP_303_SEE_OTHER)


# ── Admin event membership routes ────────────────────────────────────────────


@app.get('/admin/events/{event_id}/members/', dependencies=[Depends(require_admin)])
async def admin_event_members(request: Request, event_id: int):
    from portal.database import get_session, get_event_by_id, list_memberships_for_event, list_users

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')
        memberships = await list_memberships_for_event(session, event_id)
        users = await list_users(session)

    # Build lookup: user_id → membership (role, id)
    membership_map = {m.user_id: m for m in memberships}

    return templates.TemplateResponse(request, 'admin/event_members.html', {
        'event': event,
        'memberships': memberships,
        'membership_map': membership_map,
        'users': users,
    })


@app.post('/admin/events/{event_id}/members/', dependencies=[Depends(require_admin)])
async def admin_add_event_member(request: Request, event_id: int):
    from portal.database import get_session, list_memberships_for_event, remove_event_membership, set_event_membership, get_user_by_email

    form = await request.form()
    email = form.get('email', '').strip()
    role = form.get('role', '').strip()
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                return RedirectResponse(
                    url=f'/admin/events/{event_id}/members/?error=user_not_found',
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            uid = user.id
            if role:
                await set_event_membership(session, user_id=uid, event_id=event_id, role=role)
            else:
                # "— none —" selected: remove any existing membership
                memberships = await list_memberships_for_event(session, event_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_event_membership(session, m.id)
                        break
    return RedirectResponse(
        url=f'/admin/events/{event_id}/members/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post('/admin/events/{event_id}/members/{membership_id}/delete', dependencies=[Depends(require_admin)])
async def admin_remove_event_member(request: Request, event_id: int, membership_id: int):
    from portal.database import get_session, remove_event_membership

    async with get_session() as session:
        await remove_event_membership(session, membership_id)
    return RedirectResponse(
        url=f'/admin/events/{event_id}/members/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── Admin token management routes ────────────────────────────────────────────


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/tokens/',
    dependencies=[Depends(require_admin)],
)
async def admin_create_token(request: Request, event_id: int, room_id: int, booth_id: int):
    from portal.database import create_invite_token, get_session

    form = await request.form()
    role = form.get('role', '').strip()
    label = form.get('label', '').strip()
    expires_hours = form.get('expires_hours', '').strip()

    expires_at = None
    if expires_hours:
        from datetime import timedelta
        from portal.models import utc_now
        try:
            expires_at = utc_now() + timedelta(hours=int(expires_hours))
        except ValueError:
            pass

    if role:
        async with get_session() as session:
            await create_invite_token(
                session, booth_id=booth_id, role=role, label=label,
                expires_at=expires_at,
            )

    return RedirectResponse(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/tokens/{token_id}/revoke',
    dependencies=[Depends(require_admin)],
)
async def admin_revoke_token(request: Request, event_id: int, room_id: int, booth_id: int, token_id: str):
    from portal.database import get_session, revoke_invite_token

    async with get_session() as session:
        await revoke_invite_token(session, token_id)

    return RedirectResponse(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket('/ws/booth/{booth_id}')
async def ws_booth(websocket: WebSocket, booth_id: str) -> None:
    try:
        await verify_ws_token(websocket)
    except ValueError:
        return

    # Derive granted_role from cookies at connect time — never trust client data.
    ws_cookies = websocket.cookies
    ws_session_payload: dict | None = None
    for cookie_name in ('session_token', 'user_token'):
        raw = ws_cookies.get(cookie_name, '')
        if not raw:
            continue
        try:
            import jwt as _pyjwt
            ws_session_payload = _pyjwt.decode(
                raw, settings.effective_jwt_secret, algorithms=['HS256'],
            )
            break
        except Exception:
            continue

    ws_granted_role = resolve_booth_role(ws_session_payload)

    # is_admin in JWT always grants event_admin at the WS level too.
    if ws_granted_role is None and ws_session_payload is not None and ws_session_payload.get('is_admin'):
        ws_granted_role = 'event_admin'

    # For registered users whose token carries no 'role' claim, fall back to
    # BoothMembership then EventMembership in the DB.
    if ws_granted_role is None and ws_session_payload is not None and ws_session_payload.get('sub'):
        try:
            from portal.booth_identity import parse_booth_id as _parse_booth_id
            from portal.database import (
                get_session as _get_session,
                list_memberships_for_user as _list_memberships,
                list_booth_memberships_for_user as _list_booth_memberships,
            )
            _event_slug, _lang_code = _parse_booth_id(booth_id)
            async with _get_session() as _db:
                # 1. Booth-level membership first (interpreter for specific booth)
                _booth_mems = await _list_booth_memberships(_db, int(ws_session_payload['sub']))
                for _bm in _booth_mems:
                    if (
                        _bm.booth.event.slug == _event_slug
                        and _bm.booth.language_code == _lang_code
                    ):
                        ws_granted_role = _bm.role
                        break
                # 2. Event-level membership (coordinator, event_admin)
                if ws_granted_role is None:
                    _memberships = await _list_memberships(_db, int(ws_session_payload['sub']))
                    for _m in _memberships:
                        if _m.event and _m.event.slug == _event_slug:
                            ws_granted_role = _m.role
                            break
        except Exception:
            pass

    # Validate invite/participant token scope: the token's (event_slug, language_code)
    # must match the booth being connected to.  This prevents a valid token for booth A
    # from being used to join booth B.
    if ws_session_payload is not None and 'role' in ws_session_payload:
        token_event = ws_session_payload.get('event_slug', '')
        token_lang = ws_session_payload.get('language_code', '')
        if token_event and token_lang:
            try:
                from portal.booth_identity import make_booth_id as _make_booth_id
                expected_booth_id = _make_booth_id(token_event, token_lang)
                if expected_booth_id != booth_id:
                    await websocket.close(code=4003)
                    return
            except Exception:
                await websocket.close(code=4003)
                return

    await websocket.accept()
    session = Session(
        booth_id=booth_id,
        participant_id=None,
        language='English',
        channel_id=f'{booth_id}-audio',
        granted_role=ws_granted_role,
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
