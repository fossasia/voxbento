"""FastAPI entry point — sole backend for the Voxbento.

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
from urllib.parse import urlparse

import httpx
import jwt as pyjwt
from fastapi import Body, Depends, FastAPI, Form, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from portal.auth import (
    can_perform_role,
    create_admin_token,
    create_participant_token,
    create_token,
    create_user_token,
    decode_token,
    get_booth_session,
    get_current_user,
    hash_password,
    require_admin,
    require_user,
    resolve_booth_role,
    security,
    verify_password,
    verify_ws_token,
)
from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.booth_state import BoothRegistry
from portal.config import settings
from portal.transcription import start_transcription_worker, stop_transcription_worker

_BASE_DIR = Path(__file__).resolve().parent
# Appended to static JS URLs so the browser always fetches fresh JS after
# a server restart (prevents stale-cache issues during development).
_JS_CACHE_BUST = str(int(time.time()))


def safe_redirect(url: str, status_code: int = status.HTTP_303_SEE_OTHER) -> RedirectResponse:
    url = url.replace('\\', '').strip()
    parsed = urlparse(url)
    if url and not parsed.netloc and not parsed.scheme and url.startswith('/'):
        return RedirectResponse(url=url, status_code=status_code)
    return RedirectResponse(url='/', status_code=status_code)

booths = BoothRegistry()


@asynccontextmanager
async def lifespan(app: FastAPI):
    import httpx

    import portal.transcription as ts
    ts.shared_http_client = httpx.AsyncClient(timeout=10.0)
    yield
    if ts.shared_http_client:
        await ts.shared_http_client.aclose()


app = FastAPI(title='Voxbento', version='1.0.0', lifespan=lifespan)

from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if 'text/html' in request.headers.get('accept', ''):
        if exc.status_code == 403:
            return templates.TemplateResponse(request, '403.html', {"request": request, "detail": exc.detail}, status_code=403)
        if exc.status_code == 404:
            return templates.TemplateResponse(request, '404.html', {"request": request, "detail": exc.detail}, status_code=404)
        if exc.status_code >= 500:
            return templates.TemplateResponse(request, '500.html', {"request": request, "detail": exc.detail}, status_code=exc.status_code)
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    import logging
    logging.exception("Unhandled Server Error:")
    if 'text/html' in request.headers.get('accept', ''):
        return templates.TemplateResponse(request, '500.html', {"request": request, "detail": "Internal Server Error"}, status_code=500)
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
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

class ListenerConnectionManager:
    def __init__(self) -> None:
        self._rooms: dict[str, set[WebSocket]] = {}

    def add(self, ws: WebSocket, booth_id: str) -> None:
        self._rooms.setdefault(booth_id, set()).add(ws)

    def remove(self, ws: WebSocket, booth_id: str) -> None:
        room = self._rooms.get(booth_id, set())
        room.discard(ws)
        if not room:
            self._rooms.pop(booth_id, None)

    async def broadcast(self, booth_id: str, message: dict) -> None:
        payload = json.dumps(message)
        dead: list[WebSocket] = []
        for ws in list(self._rooms.get(booth_id, set())):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.remove(ws, booth_id)

manager = ConnectionManager()
listener_manager = ListenerConnectionManager()

async def broadcast_transcription(booth_id: str, payload: str | dict):
    if isinstance(payload, str):
        # Legacy support and error handling
        text = payload
        if text.startswith("[Server overloaded") or text.startswith("[Transcription provider failed"):
            await manager.broadcast(booth_id, {'type': 'booth:error', 'message': text})
            booth = booths.get_booth_sync(booth_id)
            if booth:
                booth.ingest_status = 'overloaded'
                await manager.broadcast(booth_id, {'type': 'booth:state', 'state': booth.as_public_dict()})
        else:
            msg = {'type': 'caption', 'status': 'final', 'text': text}
            await listener_manager.broadcast(booth_id, msg)
    else:
        # Aggregator payload
        await listener_manager.broadcast(booth_id, payload)

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
    """Non-blocking reachability check for MediaMTX API endpoint."""
    base = settings.mediamtx_api_base
    if not base:
        return False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f'{base}/v3/paths/list')
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

    if tok.role == 'listener':
        redirect_url = f'/listener/{tok.booth.event.slug}'
    else:
        redirect_url = '/interpreter'

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
    from portal.database import (
        get_session,
        list_all_booths_for_events,
        list_booth_memberships_for_user,
        list_events,
        list_memberships_for_user,
    )

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

            event_ids = [ev.id for ev in events]
            booths_by_event = await list_all_booths_for_events(session, event_ids)

            event_data = []
            for ev in events:
                db_booths = booths_by_event.get(ev.id, [])
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
                        if is_admin or ev_role == 'event_owner' or booth_role == 'interpreter':
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


# ── Research & GEO Pages ──────────────────────────────────────────────────────

@app.get('/research')
@app.get('/research/')
async def research_index(request: Request):
    return templates.TemplateResponse(request, 'research/index.html', {})

@app.get('/research/speech-ai-statistics')
async def research_speech_ai_statistics(request: Request):
    return templates.TemplateResponse(request, 'research/speech_ai_statistics.html', {})

@app.get('/research/whisper-benchmark')
async def research_whisper_benchmark(request: Request):
    return templates.TemplateResponse(request, 'research/whisper_benchmark.html', {})

@app.get('/research/stt-comparison')
async def research_stt_comparison(request: Request):
    return templates.TemplateResponse(request, 'research/stt_comparison.html', {})

@app.get('/research/real-time-audio-latency')
async def research_real_time_audio_latency(request: Request):
    return templates.TemplateResponse(request, 'research/real_time_audio_latency.html', {})

@app.get('/research/webrtc-vs-rtmp-vs-hls')
async def research_webrtc_vs_rtmp_vs_hls(request: Request):
    return templates.TemplateResponse(request, 'research/webrtc_vs_rtmp_vs_hls.html', {})

@app.get('/research/speech-ai-glossary')
async def research_speech_ai_glossary(request: Request):
    return templates.TemplateResponse(request, 'research/speech_ai_glossary.html', {})

@app.get('/research/streaming-stt-architecture')
async def research_streaming_stt_architecture(request: Request):
    return templates.TemplateResponse(request, 'research/streaming_stt_architecture.html', {})

@app.get('/llms.txt')
async def llms_txt(request: Request):
    content = """# VoxBento LLM Discoverability
Welcome to VoxBento's LLM directory.
We provide real-time browser-based interpretation with Speech AI integration.

## Documentation
- Architecture: /research/streaming-stt-architecture
- Benchmarks: /research/whisper-benchmark
- Statistics: /research/speech-ai-statistics

For full context, see /llms-full.txt
"""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content)

@app.get('/llms-full.txt')
async def llms_full_txt(request: Request):
    content = """# VoxBento Comprehensive Context
VoxBento is a production-grade browser-first interpretation booth console for live events.

# Routes
- /research/speech-ai-statistics
- /research/whisper-benchmark
- /research/stt-comparison
- /research/real-time-audio-latency
- /research/webrtc-vs-rtmp-vs-hls
- /research/speech-ai-glossary
- /research/streaming-stt-architecture

See those pages for specific benchmarking data and methodologies.
"""
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(content)

@app.get('/sitemap.xml')
async def sitemap(request: Request):
    from fastapi.responses import Response
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://voxbento.org/</loc>
    <changefreq>daily</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://voxbento.org/research</loc>
    <changefreq>weekly</changefreq>
    <priority>0.9</priority>
  </url>
  <url>
    <loc>https://voxbento.org/research/speech-ai-statistics</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/whisper-benchmark</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/stt-comparison</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/real-time-audio-latency</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/webrtc-vs-rtmp-vs-hls</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/speech-ai-glossary</loc>
    <changefreq>weekly</changefreq>
  </url>
  <url>
    <loc>https://voxbento.org/research/streaming-stt-architecture</loc>
    <changefreq>weekly</changefreq>
  </url>
</urlset>"""
    return Response(content=xml_content, media_type="application/xml")


@app.get('/healthz')
async def healthz() -> dict:
    return {
        'ok': True,
        'server': 'fastapi',
        'mediamtx_ok': await _check_mediamtx(),
    }


@app.get('/interpreter')
async def interpreter_landing_page(request: Request) -> Any:
    """Central lobby for interpreters to run pre-flight checks and view assigned booths."""
    from portal.database import get_session, list_booth_memberships_for_user

    payload = get_booth_session(request)
    if payload is None:
        return safe_redirect(url='/login?next=/interpreter', status_code=status.HTTP_303_SEE_OTHER)

    my_booths = []

    # If they joined via an invite link, the payload contains the specific event/language.
    if 'event_slug' in payload and 'language_code' in payload:
        bid = make_booth_id(payload['event_slug'], payload['language_code'])
        mem_booth = booths.get_booth_sync(bid)
        is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'

        # We need the event name and language name. We can query the DB.
        async with get_session() as session:
            from sqlalchemy import select
            from sqlalchemy.orm import joinedload

            from portal.models import DBBooth, Event

            # Simple query to get names for the UI
            stmt = select(DBBooth).options(joinedload(DBBooth.event), joinedload(DBBooth.room)).join(Event).where(Event.slug == payload['event_slug'], DBBooth.language_code == payload['language_code'])
            res = await session.execute(stmt)
            b = res.scalar_one_or_none()

            event_name = b.event.display_name if b and b.event else payload['event_slug']
            language_name = b.language_name if b else payload['language_code']
            room_name = b.room.display_name if b and b.room else ''

        my_booths.append({
            'booth_id': bid,
            'is_live': is_live,
            'event_name': event_name,
            'language_name': language_name,
            'room_name': room_name,
            'event_slug': payload['event_slug'],
            'language_code': payload['language_code'],
            'role': payload.get('role', 'interpreter'),
        })

    # If they logged in as a user, fetch all their assigned booths.
    elif payload.get('sub') and payload.get('user'):
        try:
            uid = int(payload['sub'])
            async with get_session() as session:
                bms = await list_booth_memberships_for_user(session, uid)
                for bm in bms:
                    bid = make_booth_id(bm.booth.event.slug, bm.booth.language_code)
                    mem_booth = booths.get_booth_sync(bid)
                    is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'
                    my_booths.append({
                        'booth_id': bid,
                        'is_live': is_live,
                        'event_name': bm.booth.event.display_name,
                        'language_name': bm.booth.language_name,
                        'room_name': bm.booth.room.display_name if bm.booth.room else '',
                        'event_slug': bm.booth.event.slug,
                        'language_code': bm.booth.language_code,
                        'role': bm.role,
                    })
        except ValueError:
            pass

    return templates.TemplateResponse(request, 'interpreter_landing.html', {
        'my_booths': my_booths,
        'js_version': _JS_CACHE_BUST,
    })


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
        return safe_redirect(
            url=f'/login?next=/interpreter/{event_slug}/{language_code}',
            status_code=status.HTTP_303_SEE_OTHER,
        )

    booth_id = make_booth_id(event_slug, language_code)
    granted_role = await resolve_booth_role(payload, booth_id)

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
    from sqlalchemy import select
    from sqlalchemy.orm import joinedload

    from portal.database import get_session
    from portal.models import DBBooth, Event

    room_jitsi_url = None
    async with get_session() as session:
        stmt = (
            select(DBBooth)
            .join(Event)
            .options(joinedload(DBBooth.room))
            .where(Event.slug == event_slug)
            .where(DBBooth.language_code == language_code)
        )
        db_booth = (await session.scalars(stmt)).first()
        relay_whep_url = None
        relay_language_name = None
        if db_booth and db_booth.room:
            if db_booth.room.jitsi_url:
                room_jitsi_url = db_booth.room.jitsi_url
            if db_booth.room.relay_booth_id:
                from portal.database import get_booth_by_id
                relay_b = await get_booth_by_id(session, db_booth.room.relay_booth_id)
                if relay_b:
                    relay_channel = make_mediamtx_path(event_slug, relay_b.language_code)
                    relay_whep_url = f'{settings.mediamtx_whip_base}/{relay_channel}/whep'
                    relay_language_name = relay_b.language_name

    default_jitsi_url = _make_jitsi_url(
        settings.effective_jitsi_base_url, settings.default_jitsi_room
    )
    final_jitsi_url = room_jitsi_url or default_jitsi_url

    display_name = payload.get('display_name', '') or payload.get('email', '')
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
            'relay_whep_url': relay_whep_url,
            'relay_language_name': relay_language_name,
            'granted_role': granted_role,
            'display_name': display_name,
            'default_jitsi_room': settings.default_jitsi_room,
            'jitsi_url': final_jitsi_url,
            'jitsi_domain': settings.effective_jitsi_domain,
            'jitsi_base_url': settings.effective_jitsi_base_url,
            'mediamtx_whip_base': settings.mediamtx_whip_base,
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
        return safe_redirect(
            url=f'/login?next=/interpreter/{booth_id}',
            status_code=status.HTTP_303_SEE_OTHER,
        )

    granted_role = await resolve_booth_role(payload, booth_id)
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
    display_name = payload.get('display_name', '') or payload.get('email', '')
    return templates.TemplateResponse(
        request,
        'interpreter_booth.html',
        {
            'booth_id': booth_id,
            'booth_token': token,
            'booth_language': language,
            'booth_channel_id': channel_id,
            'granted_role': granted_role,
            'display_name': display_name,
            'default_jitsi_room': settings.default_jitsi_room,
            'jitsi_url': _make_jitsi_url(
                settings.effective_jitsi_base_url, settings.default_jitsi_room
            ),
            'jitsi_domain': settings.effective_jitsi_domain,
            'jitsi_base_url': settings.effective_jitsi_base_url,
            'mediamtx_whip_base': settings.mediamtx_whip_base,
            'js_version': _JS_CACHE_BUST,
        },
    )


@app.get('/listener/{event_slug}')
async def listen_event_page(
    request: Request,
    event_slug: str,
    code: str | None = None
) -> Any:
    """Listener page scoped by event, allowing users to select room and language."""
    import asyncio

    from portal.database import get_event_by_slug, get_session, list_booths_for_event, list_rooms_for_event

    async with get_session() as session:
        ev = await get_event_by_slug(session, event_slug)
        if not ev:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")

        # Check access
        has_access = False
        payload = get_booth_session(request)
        if payload and payload.get('user'):
            has_access = True

        cookie_code = request.cookies.get(f'listener_code_{event_slug}')
        active_code = code or cookie_code

        if ev.listener_join_code and active_code == ev.listener_join_code:
            has_access = True

        if not has_access:
            return templates.TemplateResponse(request, 'listener_join.html', {
                'event': ev,
                'error': 'Invalid join code.' if code else None
            })

        rooms = await list_rooms_for_event(session, ev.id)
        db_booths = await list_booths_for_event(session, ev.id)

    booths_data = []
    ensure_tasks = []
    for b in db_booths:
        channel_id = b.mediamtx_path
        booth_lang_data = [
            {"code": lang.language_code, "name": lang.language_name}
            for lang in b.translation_languages if lang.enabled
        ]
        booths_data.append({
            'id': b.id,
            'room_id': b.room_id,
            'language_code': b.language_code,
            'language_name': b.language_name,
            'channel_id': channel_id,
            'whep_url': f'{settings.mediamtx_whip_base}/{channel_id}/whep',
            'translation_enabled': getattr(b, 'translation_enabled', False),
            'translation_languages': booth_lang_data
        })
        ensure_tasks.append(_ensure_mediamtx_path(channel_id))

    rooms_data = []
    for r in rooms:
        lang_data = [
            {"code": lang.language_code, "name": lang.language_name}
            for lang in r.translation_languages if lang.enabled
        ]
        rooms_data.append({
            'id': r.id,
            'floor_translation_enabled': r.floor_translation_enabled,
            'translation_languages': lang_data
        })

        if r.floor_transcription_enabled:
            channel_id = f"{ev.slug}/floor"
            booths_data.append({
                'id': f"floor_{r.id}",
                'room_id': r.id,
                'language_code': "floor",
                'language_name': "🌍 Floor Audio (Original)",
                'channel_id': channel_id,
                'whep_url': f'{settings.mediamtx_whip_base}/{channel_id}/whep',
                'translation_enabled': r.floor_translation_enabled,
                'translation_languages': lang_data
            })
            ensure_tasks.append(_ensure_mediamtx_path(channel_id))

    if ensure_tasks:
        await asyncio.gather(*ensure_tasks)

    response = templates.TemplateResponse(
        request,
        'listener-event.html',
        {
            'event': ev,
            'rooms': rooms,
            'rooms_json': json.dumps(rooms_data),
            'booths_json': json.dumps(booths_data),
            'js_version': _JS_CACHE_BUST,
        },
    )
    if code and code == ev.listener_join_code:
        response.set_cookie(f'listener_code_{event_slug}', code, httponly=True, max_age=31536000)
    return response


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


@app.get('/api/interpreter/status/{channel_id:path}')
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

async def _handle_set_broadcast_unlocked(ws: WebSocket, session: Session, data: dict) -> None:
    if session.granted_role not in ('room_coordinator', 'event_owner', 'super_admin'):
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': 'Only Room Coordinators can manage broadcast lock.'}))
        return

    unlocked = bool(data.get('unlocked'))
    from portal.booth_identity import parse_booth_id
    from portal.database import get_session as get_db_session

    try:
        event_slug, language_code = parse_booth_id(session.booth_id)
        async with get_db_session() as db:
            from sqlalchemy import select

            from portal.models import DBBooth, Event, Room
            stmt = select(DBBooth).join(Room).join(Event).where(Event.slug == event_slug, DBBooth.language_code == language_code)
            result = await db.execute(stmt)
            db_booth = result.scalar_one_or_none()
            if db_booth:
                db_booth.broadcast_unlocked = unlocked
                await db.commit()
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error persisting broadcast lock: {e}")

    try:
        state = await booths.set_broadcast_unlocked(
            session.booth_id, unlocked, session.language, session.channel_id,
        )
        await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})
        await listener_manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})
    except Exception as exc:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))


async def _handle_initiate_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': 'Join the booth first.'}))
        return
    try:
        state = await booths.initiate_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id,
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))
        return
    await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


async def _handle_accept_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': 'Join the booth first.'}))
        return
    try:
        state = await booths.accept_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id,
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))
        return
    await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


async def _handle_cancel_handoff(ws: WebSocket, session: Session, _data: dict) -> None:
    if not session.participant_id:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': 'Join the booth first.'}))
        return
    try:
        state = await booths.cancel_handoff(
            session.booth_id, session.participant_id, session.language, session.channel_id,
        )
    except (ValueError, PermissionError) as exc:
        await ws.send_text(json.dumps({'type': 'booth:error', 'message': str(exc)}))
        return
    await manager.broadcast(session.booth_id, {'type': 'booth:state', 'state': state})


@app.get('/register')
async def register_page(request: Request):
    current_user = await get_current_user(request)
    if current_user:
        return safe_redirect(url='/account', status_code=status.HTTP_303_SEE_OTHER)
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
                token = create_user_token(user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin)
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
async def user_login_page(request: Request, next: str = ''):
    current_user = await get_current_user(request)
    if current_user:
        redirect_to = next if next and next.startswith('/') and not next.startswith('//') else '/account'
        return safe_redirect(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, 'login.html', {'next_url': next})


@app.post('/login')
async def user_login_submit(request: Request):
    from portal.database import get_session, get_user_by_email

    form = await request.form()
    email = form.get('email', '').strip().lower()
    password = form.get('password', '')
    next_url = form.get('next_url', '')

    async with get_session() as session:
        user = await get_user_by_email(session, email)

    if user is None or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, 'login.html',
            {'error': 'Invalid email or password.', 'email': email, 'next_url': next_url},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            request, 'login.html',
            {'error': 'Your account has been deactivated. Contact an admin.', 'email': email, 'next_url': next_url},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    token = create_user_token(user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin)
    redirect_to = next_url if next_url and next_url.startswith('/') and not next_url.startswith('//') else '/account'
    response = RedirectResponse(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key='user_token', value=token,
        httponly=True, samesite='lax', max_age=settings.jwt_expiry_seconds,
    )
    return response


@app.get('/logout')
async def user_logout(request: Request):
    response = safe_redirect(url='/', status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie('user_token')
    response.delete_cookie('session_token')
    response.delete_cookie('admin_token')
    return response


@app.get('/account')
async def account_page(request: Request):
    from portal.database import get_session, get_user_by_id, list_memberships_for_user

    current_user = await get_current_user(request)
    if current_user is None:
        return safe_redirect(url='/login', status_code=status.HTTP_303_SEE_OTHER)

    async with get_session() as session:
        user = await get_user_by_id(session, int(current_user['sub']))
        if user is None:
            response = safe_redirect(url='/login', status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie('user_token')
            return response
        memberships = await list_memberships_for_user(session, user.id)

    return templates.TemplateResponse(request, 'account.html', {'user': user, 'memberships': memberships})


# ---------------------------------------------------------------------------
# Mission Control
# ---------------------------------------------------------------------------

@app.get('/mission-control/')
async def mission_control_list(request: Request, user=Depends(require_user), page: int = 1):
    import math

    from portal.auth import get_accessible_event_ids
    from portal.database import count_events, get_session, list_events

    is_super_admin, allowed_event_ids = await get_accessible_event_ids(
        request, user_id=int(user['sub'])
    )

    async with get_session() as session:
        limit = 20
        offset = (page - 1) * limit
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids)
        accessible_events = await list_events(session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids)

    total_pages = max(1, math.ceil(total_events / limit))
    return templates.TemplateResponse(
        request,
        'mission_control/event_list.html',
        {
            'events': accessible_events,
            'is_super_admin': is_super_admin,
            'active_nav': 'mission-control',
            'page': page,
            'total_pages': total_pages,

        }
    )

@app.get('/mission-control/{event_slug}/')
async def mission_control_grid(request: Request, event_slug: str, user=Depends(require_user)):
    from portal.auth import get_accessible_event_ids
    from portal.booth_identity import make_booth_id
    from portal.database import get_event_by_slug, get_session, list_booths_for_event

    is_super_admin, _ = await get_accessible_event_ids(
        request, user_id=int(user['sub'])
    )

    async with get_session() as session:
        event = await get_event_by_slug(session, event_slug)
        if not event:
            raise HTTPException(status_code=404, detail='Event not found')

        allowed_room_ids = None
        if not is_super_admin:
            from portal.database import list_memberships_for_user, list_room_memberships_for_user
            memberships = await list_memberships_for_user(session, int(user['sub']))
            room_memberships = await list_room_memberships_for_user(session, int(user['sub']))

            event_owner_ids = {m.event_id for m in memberships if m.role == 'event_owner'}
            coord_room_ids = {rm.room_id for rm in room_memberships if rm.role == 'room_coordinator'}

            if event.id not in event_owner_ids:
                from portal.database import list_rooms_for_event
                rooms = await list_rooms_for_event(session, event.id)
                event_room_ids = {r.id for r in rooms}
                if not coord_room_ids.intersection(event_room_ids):
                    raise HTTPException(status_code=403, detail='Access denied. Event Owner or Room Coordinator required.')
                allowed_room_ids = coord_room_ids

        # Load ALL configured DB booths for the event (not just in-memory ones).
        # This ensures every booth card is shown even when no interpreter is connected.
        db_booths = await list_booths_for_event(session, event.id)

        event_booths = []
        for db_b in db_booths:
            # Respect room-level access for coordinators.
            if allowed_room_ids is not None and db_b.room_id not in allowed_room_ids:
                continue

            booth_id = make_booth_id(event.slug, db_b.language_code)
            in_mem = booths._booths.get(booth_id)

            if in_mem is not None:
                state = in_mem.as_public_dict()
            else:
                # Booth has no live connections yet — build a static stub from DB.
                mtx_path = db_b.mediamtx_path
                state = {
                    'booth_id': booth_id,
                    'event_slug': event.slug,
                    'language_code': db_b.language_code,
                    'language': db_b.language_name,
                    'instance': 'primary',
                    'mediamtx_path': mtx_path,
                    'room_id': db_b.room_id,
                    'channel_id': mtx_path,
                    'active_interpreter_id': None,
                    'handoff_state': 'idle',
                    'handoff_initiator_id': None,
                    'broadcast_unlocked': db_b.broadcast_unlocked,
                    'ingest_status': 'disconnected',
                    'participants': [],
                    'chat_messages': [],
                }

            # Always annotate with the human-readable room name from DB.
            state['room_name'] = db_b.room.display_name if db_b.room else 'Unknown Room'
            event_booths.append(state)

    return templates.TemplateResponse(
        request,
        'mission_control/grid.html',
        {
            'event': event,
            'booths': event_booths,
            'whip_base': settings.mediamtx_whip_base,
            'js_version': _JS_CACHE_BUST,
            'active_nav': 'mission-control',
        }
    )

# ---------------------------------------------------------------------------
# Admin Panel Pages ────────────────────────────────────────────────────────


@app.get('/admin/login')
async def admin_login_page(request: Request):
    user = await get_current_user(request)
    if user and user.get('is_admin'):
        return safe_redirect(url='/admin/', status_code=status.HTTP_303_SEE_OTHER)
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
    response = safe_redirect(url='/admin/', status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key='admin_token', value=token,
        httponly=True, samesite='lax', max_age=settings.jwt_expiry_seconds,
    )
    return response


@app.get('/admin/logout')
async def admin_logout():
    response = safe_redirect(url='/admin/login', status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie('admin_token')
    response.delete_cookie('user_token')
    response.delete_cookie('session_token')
    return response


@app.get('/admin/', dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request, page: int = 1):
    import math

    from portal.auth import get_accessible_event_ids, get_admin_flags, get_current_user
    from portal.database import (
        count_events,
        get_session,
        list_all_booths_for_events,
        list_events,
    )

    admin_flags = await get_admin_flags(request)
    user = await get_current_user(request)
    user_id = int(user['sub']) if user and user.get('sub') else None
    _, allowed_event_ids = await get_accessible_event_ids(request, user_id=user_id)

    limit = 20
    offset = (page - 1) * limit

    async with get_session() as session:
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids)
        events = await list_events(session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids)

        if not admin_flags.get('is_super_admin') and user:
            if len(events) == 1 and total_events == 1:
                return safe_redirect(url=f'/admin/events/{events[0].id}/', status_code=status.HTTP_303_SEE_OTHER)

        event_ids = [ev.id for ev in events]
        booths_by_event = await list_all_booths_for_events(session, event_ids)

        event_data = []
        for ev in events:
            db_booths = booths_by_event.get(ev.id, [])
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
    total_pages = max(1, math.ceil(total_events / limit))
    return templates.TemplateResponse(request, 'admin/dashboard.html', {
        'event_data': event_data,
        'mediamtx_ok': mediamtx_ok,
        'page': page,
        'total_pages': total_pages,
        **admin_flags,
    })


@app.get('/admin/events/', dependencies=[Depends(require_admin)])
async def admin_event_list(request: Request, page: int = 1):
    import math

    from portal.auth import get_accessible_event_ids, get_admin_flags, get_current_user
    from portal.database import count_events, get_session, list_events

    admin_flags = await get_admin_flags(request)
    user = await get_current_user(request)
    user_id = int(user['sub']) if user and user.get('sub') else None
    _, allowed_event_ids = await get_accessible_event_ids(request, user_id=user_id)

    limit = 20
    offset = (page - 1) * limit

    async with get_session() as session:
        total_events = await count_events(session, allowed_event_ids=allowed_event_ids)
        events = await list_events(session, limit=limit, offset=offset, allowed_event_ids=allowed_event_ids)

    total_pages = max(1, math.ceil(total_events / limit))
    return templates.TemplateResponse(request, 'admin/event_list.html', {
        'events': events,
        'page': page,
        'total_pages': total_pages,
        **admin_flags,
    })


@app.post('/admin/events/', dependencies=[Depends(require_admin)])
async def admin_create_event(request: Request):
    from portal.database import create_event, get_session

    form = await request.form()
    slug = form.get('slug', '').strip()
    display_name = form.get('display_name', '').strip()
    if not slug or not display_name:
        return safe_redirect(url='/admin/events/', status_code=status.HTTP_303_SEE_OTHER)
    try:
        async with get_session() as session:
            await create_event(session, slug=slug, display_name=display_name)
    except Exception:
        return safe_redirect(url='/admin/events/', status_code=status.HTTP_303_SEE_OTHER)
    return safe_redirect(url='/admin/events/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/admin/events/{event_id}/regenerate_join_code/', dependencies=[Depends(require_admin)])
async def admin_regenerate_join_code(request: Request, event_id: int):
    import secrets

    from portal.database import get_event_by_id, get_session

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')

        event.listener_join_code = ''.join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        await session.commit()

    return safe_redirect(url=f'/admin/events/{event_id}/', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/admin/events/{event_id}/', dependencies=[Depends(require_admin)])
async def admin_event_detail(request: Request, event_id: int):
    from portal.auth import get_admin_flags
    from portal.database import (
        get_event_by_id,
        get_session,
        list_booths_for_event,
        list_rooms_for_event,
    )
    admin_flags = await get_admin_flags(request, event_id=event_id)

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
        **admin_flags,
    })


@app.get('/admin/events/{event_id}/api-settings/', dependencies=[Depends(require_admin)])
async def admin_event_api_settings_get(request: Request, event_id: int):
    from portal.database import get_event_by_id, get_session

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')

    from portal.auth import get_admin_flags
    admin_flags = await get_admin_flags(request, event_id=event_id)

    return templates.TemplateResponse(request, 'admin/api_settings.html', {
        'event': event,
        **admin_flags,
    })


@app.post('/admin/events/{event_id}/api-settings', dependencies=[Depends(require_admin)])
async def admin_event_api_settings_post(
    request: Request,
    event_id: int,
    transcription_api_enabled: bool | None = Form(False),
    openai_api_key: str | None = Form(None),
    deepgram_api_key: str | None = Form(None),
    nvidia_api_key: str | None = Form(None),
    elevenlabs_api_key: str | None = Form(None),
    clear_openai_api_key: bool | None = Form(False),
    clear_deepgram_api_key: bool | None = Form(False),
    clear_nvidia_api_key: bool | None = Form(False),
    clear_elevenlabs_api_key: bool | None = Form(False),

    translation_openai_api_key: str | None = Form(None),
    openrouter_api_key: str | None = Form(None),
    gemini_api_key: str | None = Form(None),
    anthropic_api_key: str | None = Form(None),
    groq_api_key: str | None = Form(None),
    clear_translation_openai_api_key: bool | None = Form(False),
    clear_openrouter_api_key: bool | None = Form(False),
    clear_gemini_api_key: bool | None = Form(False),
    clear_anthropic_api_key: bool | None = Form(False),
    clear_groq_api_key: bool | None = Form(False),
):
    from portal.crypto import encrypt_val
    from portal.database import get_event_by_id, get_session

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')

        event.transcription_api_enabled = bool(transcription_api_enabled)

        try:
            if clear_openai_api_key:
                event.encrypted_openai_api_key = None
            elif openai_api_key and openai_api_key.strip():
                event.encrypted_openai_api_key = encrypt_val(openai_api_key.strip())

            if clear_deepgram_api_key:
                event.encrypted_deepgram_api_key = None
            elif deepgram_api_key and deepgram_api_key.strip():
                event.encrypted_deepgram_api_key = encrypt_val(deepgram_api_key.strip())

            if clear_nvidia_api_key:
                event.encrypted_nvidia_api_key = None
            elif nvidia_api_key and nvidia_api_key.strip():
                event.encrypted_nvidia_api_key = encrypt_val(nvidia_api_key.strip())

            if clear_elevenlabs_api_key:
                event.encrypted_elevenlabs_api_key = None
            elif elevenlabs_api_key and elevenlabs_api_key.strip():
                event.encrypted_elevenlabs_api_key = encrypt_val(elevenlabs_api_key.strip())

            if clear_translation_openai_api_key:
                event.encrypted_translation_openai_api_key = None
            elif translation_openai_api_key and translation_openai_api_key.strip():
                event.encrypted_translation_openai_api_key = encrypt_val(translation_openai_api_key.strip())

            if clear_openrouter_api_key:
                event.encrypted_openrouter_api_key = None
            elif openrouter_api_key and openrouter_api_key.strip():
                event.encrypted_openrouter_api_key = encrypt_val(openrouter_api_key.strip())

            if clear_gemini_api_key:
                event.encrypted_gemini_api_key = None
            elif gemini_api_key and gemini_api_key.strip():
                event.encrypted_gemini_api_key = encrypt_val(gemini_api_key.strip())

            if clear_anthropic_api_key:
                event.encrypted_anthropic_api_key = None
            elif anthropic_api_key and anthropic_api_key.strip():
                event.encrypted_anthropic_api_key = encrypt_val(anthropic_api_key.strip())

            if clear_groq_api_key:
                event.encrypted_groq_api_key = None
            elif groq_api_key and groq_api_key.strip():
                event.encrypted_groq_api_key = encrypt_val(groq_api_key.strip())

        except (ValueError, RuntimeError) as e:
            raise HTTPException(status_code=400, detail=f"API Key encryption failed: {e}")

        await session.commit()

    return safe_redirect(url=f'/admin/events/{event_id}/api-settings/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/admin/events/{event_id}/delete', dependencies=[Depends(require_admin)])
async def admin_delete_event(request: Request, event_id: int):
    from portal.database import delete_event, get_session

    async with get_session() as session:
        await delete_event(session, event_id)
    return safe_redirect(url='/admin/events/', status_code=status.HTTP_303_SEE_OTHER)


@app.get('/admin/events/{event_id}/rooms/', dependencies=[Depends(require_admin)])
async def admin_room_list(request: Request, event_id: int):
    from portal.database import (
        get_event_by_id,
        get_session,
        list_booths_for_room,
        list_rooms_for_event,
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
    from portal.database import create_room, get_session

    form = await request.form()
    display_name = form.get('display_name', '').strip()
    if not display_name:
        return safe_redirect(
            url=f'/admin/events/{event_id}/rooms/',
            status_code=status.HTTP_303_SEE_OTHER,
        )
    async with get_session() as session:
        import re

        from portal.database import get_event_by_id

        ev = await get_event_by_id(session, event_id)
        jitsi_url = None
        if ev:
            clean_name = re.sub(r'[^a-zA-Z0-9]+', '', display_name)
            room_id_str = f"Voxbento-{ev.slug}-{clean_name}"
            jitsi_url = _make_jitsi_url(settings.effective_jitsi_base_url, room_id_str)

        await create_room(session, event_id=event_id, display_name=display_name, jitsi_url=jitsi_url)
    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get('/admin/events/{event_id}/rooms/{room_id}/', dependencies=[Depends(require_admin)])
async def admin_room_detail(request: Request, event_id: int, room_id: int):
    from portal.auth import get_admin_flags
    from portal.database import (
        get_event_by_id,
        get_room_by_id,
        get_session,
        list_booths_for_room,
    )
    admin_flags = await get_admin_flags(request, event_id=event_id, room_id=room_id)

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

    import re
    clean_name = re.sub(r'[^a-zA-Z0-9]+', '', room.display_name)
    room_id_str = f"Voxbento-{event.slug}-{clean_name}"
    fallback_jitsi_url = _make_jitsi_url(settings.effective_jitsi_base_url, room_id_str)

    import pycountry
    # Get ISO 639-1 languages
    translation_languages_dataset = [
        {"code": lang.alpha_2, "name": lang.name}
        for lang in pycountry.languages if hasattr(lang, 'alpha_2')
    ]
    translation_languages_dataset.sort(key=lambda x: x["name"])

    enabled_translation_language_codes = [lang.language_code for lang in room.translation_languages if lang.enabled]

    from portal.database import list_memberships_for_room
    async with get_session() as session:
        memberships = await list_memberships_for_room(session, room_id)

    return templates.TemplateResponse(request, 'admin/room_detail.html', {
        'event': event,
        'room': room,
        'booths': booth_statuses,
        'fallback_jitsi_url': fallback_jitsi_url,
        'translation_languages_dataset': translation_languages_dataset,
        'enabled_translation_language_codes': enabled_translation_language_codes,
        'memberships': memberships,
        **admin_flags,
    })


@app.get('/admin/events/{event_id}/rooms/{room_id}/transcripts/', dependencies=[Depends(require_admin)])
async def admin_room_transcripts(request: Request, event_id: int, room_id: int):
    from portal.database import get_event_by_id, get_room_by_id, get_session, list_booths_for_room

    async with get_session() as session:
        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')
        room = await get_room_by_id(session, room_id)
        if room is None:
            raise HTTPException(status_code=404, detail='Room not found.')

        booths = await list_booths_for_room(session, room_id)

    from portal.auth import get_admin_flags
    admin_flags = await get_admin_flags(request, event_id=event_id, room_id=room_id)

    return templates.TemplateResponse(request, 'admin/room_transcripts.html', {
        'event': event,
        'room': room,
        'booths': booths,
        **admin_flags,
    })


@app.post('/admin/events/{event_id}/rooms/{room_id}/edit', dependencies=[Depends(require_admin)])
async def admin_edit_room(request: Request, event_id: int, room_id: int):
    import pycountry

    from portal.database import get_room_by_id, get_session
    from portal.models import RoomTranslationLanguage

    form = await request.form()
    display_name = form.get('display_name', '').strip()
    jitsi_url = form.get('jitsi_url', '').strip()
    relay_booth_id_str = form.get('relay_booth_id', '').strip()
    relay_booth_id = int(relay_booth_id_str) if relay_booth_id_str and relay_booth_id_str.lower() != 'none' else None

    floor_transcription_enabled = form.get('floor_transcription_enabled') == 'on'
    floor_transcription_provider = form.get('floor_transcription_provider', 'local').strip()
    floor_transcription_model = form.get('floor_transcription_model', 'tiny').strip()
    floor_language_code = form.get('floor_language_code', '').strip() or None

    floor_translation_enabled = form.get('floor_translation_enabled') == 'on'
    floor_translation_provider = form.get('floor_translation_provider', '').strip() or None
    floor_translation_model = form.get('floor_translation_model', '').strip() or None

    floor_translation_languages = form.getlist('floor_translation_languages')

    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if room and room.event_id == event_id:
            if display_name:
                room.display_name = display_name
            room.jitsi_url = jitsi_url if jitsi_url else None
            room.relay_booth_id = relay_booth_id
            room.floor_transcription_enabled = floor_transcription_enabled
            room.floor_transcription_provider = floor_transcription_provider
            room.floor_transcription_model = floor_transcription_model
            room.floor_language_code = floor_language_code

            room.floor_translation_enabled = floor_translation_enabled
            room.floor_translation_provider = floor_translation_provider
            room.floor_translation_model = floor_translation_model

            # Sync target languages
            existing_langs = {lang.language_code: lang for lang in room.translation_languages}
            requested_codes = set(floor_translation_languages)

            # Disable existing that are no longer requested
            for code, lang in existing_langs.items():
                if code not in requested_codes:
                    lang.enabled = False

            # Add or enable requested
            for code in requested_codes:
                if code in existing_langs:
                    existing_langs[code].enabled = True
                else:
                    lang_obj = pycountry.languages.get(alpha_2=code)
                    lang_name = lang_obj.name if lang_obj else code
                    new_lang = RoomTranslationLanguage(
                        room_id=room_id,
                        language_code=code,
                        language_name=lang_name,
                        enabled=True
                    )
                    session.add(new_lang)

            await session.commit()

    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/{room_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )

@app.get('/api/admin/providers/translation/models', dependencies=[Depends(require_admin)])
async def get_translation_models():
    from portal.translations.constants import TRANSLATION_MODELS
    return TRANSLATION_MODELS

import logging

logger = logging.getLogger(__name__)

@app.post('/api/rooms/{room_id}/floor-transcription/start', dependencies=[Depends(require_admin)])
async def api_start_floor_transcription(room_id: int):
    from portal.database import get_event_by_id, get_room_by_id, get_session
    from portal.transcription.worker import start_transcription_worker

    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room or not room.floor_transcription_enabled:
            raise HTTPException(status_code=400, detail="Floor transcription not enabled or invalid room")
        event = await get_event_by_id(session, room.event_id)
        if not event:
            raise HTTPException(status_code=400, detail="Event not found")

        event_slug = event.slug

        import re
        clean_name = re.sub(r'[^a-zA-Z0-9]+', '', room.display_name)
        room_id_str = f"Voxbento-{event.slug}-{clean_name}"

        if room.jitsi_url:
            jitsi_url = room.jitsi_url
            import urllib.parse
            parsed = urllib.parse.urlparse(room.jitsi_url)
            internal_parsed = urllib.parse.urlparse(settings.effective_jitsi_internal_base)
            base_parsed = urllib.parse.urlparse(settings.effective_jitsi_base_url)

            if parsed.netloc in ("jitsi.voxbento.com", base_parsed.netloc) or parsed.netloc.startswith(("localhost", "127.0.0.1")):
                parsed = parsed._replace(scheme=internal_parsed.scheme, netloc=internal_parsed.netloc)
                jitsi_url = urllib.parse.urlunparse(parsed)
            else:
                jitsi_url = room.jitsi_url
        else:
            jitsi_url = f"{settings.effective_jitsi_internal_base}/{room_id_str}"



    # 1. Start floor-bot subprocess
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.floor_bot_base}/start",
                json={
                    "event_slug": event_slug,
                    "jitsi_url": jitsi_url,
                    "mediamtx_rtsp_base": settings.mediamtx_rtsp_base
                },
                timeout=10.0
            )
            resp.raise_for_status()
    except Exception as e:
        logger.error(f"Failed to start floor-bot: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start floor bot: {e}")

    # 2. Start transcription worker reading from {event_slug}/floor via RTSP
    # We use {event_slug}-floor as the pseudo booth_id, and floor_language_code for the provider.
    from portal.transcription import ProviderConfig, ProviderEnum, get_api_key
    try:
        api_key = get_api_key(event, ProviderEnum(room.floor_transcription_provider))
        config = ProviderConfig(api_key=api_key)

        await start_transcription_worker(
            event_slug=event_slug,
            language_code="floor", # Tells aggregator this is floor audio path
            booth_id=f"{event_slug}-floor",
            broadcast_callback=broadcast_transcription,
            provider=room.floor_transcription_provider,
            model_size=room.floor_transcription_model,
            config=config,
            transcription_language=room.floor_language_code,
            room_id=room_id
        )
    except Exception as e:
        logger.error(f"Failed to start transcription worker: {e}")
        # Rollback bot if worker fails to start
        async with httpx.AsyncClient() as client:
            await client.post(f"{settings.floor_bot_base}/stop", json={"event_slug": event_slug})
        raise HTTPException(status_code=500, detail=f"Failed to start transcription worker: {e}")

    return {"status": "started"}

@app.post('/api/rooms/{room_id}/floor-transcription/stop', dependencies=[Depends(require_admin)])
async def api_stop_floor_transcription(room_id: int):
    from portal.database import get_event_by_id, get_room_by_id, get_session
    from portal.transcription.worker import stop_transcription_worker

    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room:
            raise HTTPException(status_code=400, detail="Invalid room")
        event = await get_event_by_id(session, room.event_id)
        if not event:
            raise HTTPException(status_code=400, detail="Event not found")

        event_slug = event.slug

    # 1. Stop floor-bot
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{settings.floor_bot_base}/stop",
                json={"event_slug": event_slug},
                timeout=5.0
            )
    except Exception as e:
        logger.error(f"Failed to stop floor-bot: {e}")
        # Continue to try stopping the worker even if bot fails

    # 2. Stop transcription worker
    stop_transcription_worker(f"{event_slug}-floor")

    return {"status": "stopped"}


@app.post('/admin/events/{event_id}/rooms/{room_id}/delete', dependencies=[Depends(require_admin)])
async def admin_delete_room(request: Request, event_id: int, room_id: int):
    from portal.database import delete_room, get_session

    async with get_session() as session:
        await delete_room(session, room_id)
    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get('/admin/events/{event_id}/rooms/{room_id}/booths/', dependencies=[Depends(require_admin)])
async def admin_booth_list(request: Request, event_id: int, room_id: int):
    from portal.database import (
        get_event_by_id,
        get_room_by_id,
        get_session,
        list_booths_for_room,
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

    from portal.auth import get_admin_flags
    admin_flags = await get_admin_flags(request, event_id=event_id, room_id=room_id)

    return templates.TemplateResponse(request, 'admin/booth_list.html', {
        'event': event,
        'room': room,
        'booths': booth_statuses,
        **admin_flags,
    })


@app.post('/admin/events/{event_id}/rooms/{room_id}/booths/', dependencies=[Depends(require_admin)])
async def admin_create_booth(request: Request, event_id: int, room_id: int):
    from portal.database import create_booth, get_session

    form = await request.form()
    language_code = form.get('language_code', '').strip().lower()
    language_name = form.get('language_name', '').strip()
    if not language_code or not language_name:
        return safe_redirect(
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
    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.get(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
    dependencies=[Depends(require_admin)],
)
async def admin_booth_detail(request: Request, event_id: int, room_id: int, booth_id: int):
    from portal.auth import get_admin_flags
    from portal.database import (
        get_booth_by_id,
        get_event_by_id,
        get_room_by_id,
        get_session,
        list_memberships_for_booth,
        list_tokens_for_booth,
        list_users,
    )
    admin_flags = await get_admin_flags(request, event_id=event_id, room_id=room_id)

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

    import pycountry
    # Get ISO 639-1 languages
    translation_languages_dataset = [
        {"code": lang.alpha_2, "name": lang.name}
        for lang in pycountry.languages if hasattr(lang, 'alpha_2')
    ]
    translation_languages_dataset.sort(key=lambda x: x["name"])

    enabled_translation_language_codes = [lang.language_code for lang in db_booth.translation_languages if lang.enabled]

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
        'translation_languages_dataset': translation_languages_dataset,
        'enabled_translation_language_codes': enabled_translation_language_codes,
        **admin_flags,
    })


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/members/',
    dependencies=[Depends(require_admin)],
)
async def admin_add_room_member(request: Request, event_id: int, room_id: int):
    from portal.database import (
        get_session,
        get_user_by_email,
        list_memberships_for_room,
        remove_room_membership,
        set_room_membership,
    )

    form = await request.form()
    email = form.get('email', '').strip()
    role = form.get('role', '').strip()
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                return safe_redirect(
                    url=f'/admin/events/{event_id}/rooms/{room_id}/?error=user_not_found',
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            uid = user.id
            if role:
                try:
                    await set_room_membership(session, user_id=uid, room_id=room_id, role=role)
                except ValueError:
                    return safe_redirect(
                        url=f'/admin/events/{event_id}/rooms/{room_id}/?error=invalid_role',
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                memberships = await list_memberships_for_room(session, room_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_room_membership(session, m.id)
                        break
    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/{room_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/members/{membership_id}/delete',
    dependencies=[Depends(require_admin)],
)
async def admin_remove_room_member(request: Request, event_id: int, room_id: int, membership_id: int):
    from portal.database import get_session, remove_room_membership

    async with get_session() as session:
        await remove_room_membership(session, membership_id)
    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/{room_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/members/',
    dependencies=[Depends(require_admin)],
)
async def admin_add_booth_member(request: Request, event_id: int, room_id: int, booth_id: int):
    from portal.database import (
        get_session,
        get_user_by_email,
        list_memberships_for_booth,
        remove_booth_membership,
        set_booth_membership,
    )

    form = await request.form()
    email = form.get('email', '').strip()
    role = form.get('role', '').strip()
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                return safe_redirect(
                    url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/?error=user_not_found',
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            uid = user.id
            if role:
                try:
                    await set_booth_membership(session, user_id=uid, booth_id=booth_id, role=role)
                except ValueError:
                    return safe_redirect(
                        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/?error=invalid_role',
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                # "— none —" selected: remove any existing membership
                memberships = await list_memberships_for_booth(session, booth_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_booth_membership(session, m.id)
                        break
    return safe_redirect(
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
    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/delete',
    dependencies=[Depends(require_admin)],
)
async def admin_delete_booth(request: Request, event_id: int, room_id: int, booth_id: int):
    from portal.database import delete_booth, get_session

    async with get_session() as session:
        await delete_booth(session, booth_id)
    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/translation-settings',
    dependencies=[Depends(require_admin)],
)
async def admin_booth_translation_settings(
    request: Request,
    event_id: int,
    room_id: int,
    booth_id: int,
    translation_enabled: bool | None = Form(False),
    translation_provider: str = Form('openai'),
    translation_model: str = Form('gpt-4o-mini'),
    translation_languages: list[str] = Form([]),
):
    import pycountry

    from portal.database import get_booth_by_id, get_event_by_id, get_session
    from portal.models import BoothTranslationLanguage
    from portal.translations.constants import TranslationProviderEnum

    async with get_session() as session:
        db_booth = await get_booth_by_id(session, booth_id)
        if db_booth is None or db_booth.room_id != room_id:
            raise HTTPException(status_code=404, detail='Booth not found.')

        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')

        try:
            TranslationProviderEnum(translation_provider)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid translation provider")

        db_booth.translation_enabled = translation_enabled
        db_booth.translation_provider = translation_provider
        db_booth.translation_model = translation_model

        # Update target languages
        current_langs = {lang.language_code: lang for lang in db_booth.translation_languages}

        # Add new ones or re-enable
        for code in translation_languages:
            if code in current_langs:
                current_langs[code].enabled = True
            else:
                lang_obj = pycountry.languages.get(alpha_2=code)
                lang_name = lang_obj.name if lang_obj else code
                db_booth.translation_languages.append(
                    BoothTranslationLanguage(
                        booth_id=db_booth.id,
                        language_code=code,
                        language_name=lang_name,
                        enabled=True
                    )
                )

        # Disable unselected ones
        for code, lang_model in current_langs.items():
            if code not in translation_languages:
                lang_model.enabled = False

        await session.commit()

    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )

@app.post('/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/edit', dependencies=[Depends(require_admin)])
async def admin_edit_booth(request: Request, event_id: int, room_id: int, booth_id: int):
    from portal.booth_identity import validate_language_code
    from portal.database import get_booth_by_id, get_session

    form = await request.form()
    language_name = form.get('language_name', '').strip()
    language_code_raw = form.get('language_code', '').strip()

    async with get_session() as session:
        booth = await get_booth_by_id(session, booth_id)
        if booth and booth.event_id == event_id and booth.room_id == room_id:
            if language_name:
                booth.language_name = language_name
            if language_code_raw:
                try:
                    booth.language_code = validate_language_code(language_code_raw)
                except ValueError:
                    pass
        await session.commit()

    return safe_redirect(
        url=str(request.url_for('admin_booth_detail', event_id=event_id, room_id=room_id, booth_id=booth_id)),
        status_code=status.HTTP_303_SEE_OTHER,
    )



@app.post(
    '/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/transcription-settings',
    dependencies=[Depends(require_admin)],
)
async def admin_transcription_settings(
    request: Request,
    event_id: int,
    room_id: int,
    booth_id: int,
    transcription_enabled: bool | None = Form(False),
    transcription_provider: str = Form('local'),
    transcription_model: str = Form('tiny'),
):
    from portal.booth_identity import make_booth_id
    from portal.database import get_booth_by_id, get_event_by_id, get_session
    from portal.transcription import (
        ALLOWED_MODELS,
        ProviderEnum,
        get_api_key,
        start_transcription_worker,
        stop_transcription_worker,
    )

    async with get_session() as session:
        db_booth = await get_booth_by_id(session, booth_id)
        if db_booth is None or db_booth.room_id != room_id:
            raise HTTPException(status_code=404, detail='Booth not found.')

        try:
            provider_enum = ProviderEnum(transcription_provider)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid transcription provider")

        if transcription_model not in ALLOWED_MODELS.get(provider_enum, set()):
            raise HTTPException(status_code=400, detail=f"Invalid model '{transcription_model}' for provider '{transcription_provider}'")

        event = await get_event_by_id(session, event_id)
        if event is None:
            raise HTTPException(status_code=404, detail='Event not found.')

        if provider_enum != ProviderEnum.LOCAL:
            if not event.transcription_api_enabled:
                raise HTTPException(status_code=400, detail="External API transcription is disabled for this event.")
            if not get_api_key(event, provider_enum):
                raise HTTPException(status_code=400, detail=f"API key for {transcription_provider} is not configured on the event.")

        old_enabled = db_booth.transcription_enabled
        old_provider = db_booth.transcription_provider
        old_model = db_booth.transcription_model

        # We need to manually update the columns and commit
        db_booth.transcription_enabled = bool(transcription_enabled)
        db_booth.transcription_provider = transcription_provider
        db_booth.transcription_model = transcription_model
        await session.commit()

        bid = make_booth_id(event.slug, db_booth.language_code)

        # Check if booth is live
        state = booths.get_booth_sync(bid)
        is_live = state is not None and state.active_interpreter_id is not None

        if is_live:
            if not transcription_enabled:
                await stop_transcription_worker(bid)
                await broadcast_transcription(bid, "")
            elif old_enabled != transcription_enabled or old_provider != transcription_provider or old_model != transcription_model:
                await stop_transcription_worker(bid)
                await broadcast_transcription(bid, "")

                from portal.transcription import ProviderConfig, ProviderEnum, get_api_key
                api_key = get_api_key(event, ProviderEnum(transcription_provider))
                config = ProviderConfig(api_key=api_key)

                import asyncio
                await asyncio.sleep(0.1)
                await start_transcription_worker(event.slug, db_booth.language_code, bid, broadcast_transcription, transcription_provider, transcription_model, config, room_id=db_booth.room_id)
    return safe_redirect(
        url=f'/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ── Admin user management routes ─────────────────────────────────────────────


@app.get('/admin/users/', dependencies=[Depends(require_admin)])
async def admin_user_list(request: Request, page: int = 1):
    import math

    from portal.database import count_users, get_session, list_users

    limit = 20
    offset = (page - 1) * limit

    async with get_session() as session:
        total_users = await count_users(session)
        users = await list_users(session, limit=limit, offset=offset)

    total_pages = max(1, math.ceil(total_users / limit))
    return templates.TemplateResponse(request, 'admin/user_list.html', {
        'users': users,
        'page': page,
        'total_pages': total_pages
    })


@app.post('/admin/users/{user_id}/toggle-active', dependencies=[Depends(require_admin)])
async def admin_toggle_user_active(request: Request, user_id: int):
    from portal.database import get_session, get_user_by_id, update_user_active

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            await update_user_active(session, user_id, is_active=not user.is_active)
    return safe_redirect(url='/admin/users/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/admin/users/{user_id}/delete', dependencies=[Depends(require_admin)])
async def admin_delete_user(request: Request, user_id: int):
    from portal.database import delete_user, get_session

    async with get_session() as session:
        await delete_user(session, user_id)
    return safe_redirect(url='/admin/users/', status_code=status.HTTP_303_SEE_OTHER)

@app.get('/admin/users/{user_id}/', dependencies=[Depends(require_admin)])
async def admin_user_detail(request: Request, user_id: int):
    from portal.database import get_session, get_user_by_id, list_events, list_memberships_for_user

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if not user:
            return safe_redirect(url='/admin/users/', status_code=status.HTTP_303_SEE_OTHER)

        events = await list_events(session)
        memberships = await list_memberships_for_user(session, user_id)

        event_owner_map = {m.event_id: m for m in memberships if m.role == 'event_owner'}

    return templates.TemplateResponse(request, 'admin/user_detail.html', {
        'user_detail': user,
        'events': events,
        'event_owner_map': event_owner_map
    })


@app.post('/admin/users/{user_id}/toggle-admin', dependencies=[Depends(require_admin)])
async def admin_toggle_user_admin(request: Request, user_id: int):
    from sqlalchemy import update

    from portal.database import get_session, get_user_by_id
    from portal.models import User

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            stmt = update(User).where(User.id == user_id).values(is_admin=not user.is_admin)
            await session.execute(stmt)
            await session.commit()
    return safe_redirect(url=f'/admin/users/{user_id}/', status_code=status.HTTP_303_SEE_OTHER)


@app.post('/admin/users/{user_id}/events/{event_id}/toggle-owner', dependencies=[Depends(require_admin)])
async def admin_toggle_user_event_owner(request: Request, user_id: int, event_id: int):
    from portal.database import (
        get_session,
        get_user_by_id,
        list_memberships_for_user,
        remove_event_membership,
        set_event_membership,
    )

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user:
            memberships = await list_memberships_for_user(session, user_id)
            event_owner_membership = next((m for m in memberships if m.event_id == event_id and m.role == 'event_owner'), None)

            if event_owner_membership:
                await remove_event_membership(session, event_owner_membership.id)
            else:
                await set_event_membership(session, user_id=user_id, event_id=event_id, role='event_owner')

    return safe_redirect(url=f'/admin/users/{user_id}/', status_code=status.HTTP_303_SEE_OTHER)


# ── Admin event membership routes ────────────────────────────────────────────


@app.get('/admin/events/{event_id}/members/', dependencies=[Depends(require_admin)])
async def admin_event_members(request: Request, event_id: int):
    from portal.database import get_event_by_id, get_session, list_memberships_for_event, list_users

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
    from portal.database import (
        get_session,
        get_user_by_email,
        list_memberships_for_event,
        remove_event_membership,
        set_event_membership,
    )

    form = await request.form()
    email = form.get('email', '').strip()
    role = form.get('role', '').strip()
    if email:
        async with get_session() as session:
            user = await get_user_by_email(session, email)
            if not user:
                return safe_redirect(
                    url=f'/admin/events/{event_id}/members/?error=user_not_found',
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            uid = user.id
            if role:
                try:
                    await set_event_membership(session, user_id=uid, event_id=event_id, role=role)
                except ValueError:
                    return safe_redirect(
                        url=f'/admin/events/{event_id}/members/?error=invalid_role',
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            else:
                # "— none —" selected: remove any existing membership
                memberships = await list_memberships_for_event(session, event_id)
                for m in memberships:
                    if m.user_id == uid:
                        await remove_event_membership(session, m.id)
                        break
    return safe_redirect(
        url=f'/admin/events/{event_id}/members/',
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post('/admin/events/{event_id}/members/{membership_id}/delete', dependencies=[Depends(require_admin)])
async def admin_remove_event_member(request: Request, event_id: int, membership_id: int):
    from portal.database import get_session, remove_event_membership

    async with get_session() as session:
        await remove_event_membership(session, membership_id)
    return safe_redirect(
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

    return safe_redirect(
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

    return safe_redirect(
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
    for cookie_name in ('admin_token', 'user_token', 'session_token'):
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

    ws_granted_role = await resolve_booth_role(ws_session_payload, booth_id)

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
            elif msg_type == 'booth:chat':
                await _handle_chat(websocket, session, data)
            elif msg_type == 'booth:set-active':
                await _handle_set_active(websocket, session, data)
            elif msg_type == 'booth:update-state':
                await _handle_update_state(websocket, session, data)
            elif msg_type == 'booth:set-broadcast-unlocked':
                await _handle_set_broadcast_unlocked(websocket, session, data)
            elif msg_type == 'booth:initiate-handoff':
                await _handle_initiate_handoff(websocket, session, data)
            elif msg_type == 'booth:accept-handoff':
                await _handle_accept_handoff(websocket, session, data)
            elif msg_type == 'booth:cancel-handoff':
                await _handle_cancel_handoff(websocket, session, data)
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

@app.websocket('/ws/captions/{booth_id}')
async def ws_captions(websocket: WebSocket, booth_id: str) -> None:
    await websocket.accept()
    listener_manager.add(websocket, booth_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        listener_manager.remove(websocket, booth_id)

@app.post('/api/booth/{booth_id}/transcription/start')
async def api_transcription_start(
    booth_id: str,
    request: Request,
    token: str = Query(''),
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    _require_access(credentials, token)
    data = await request.json()
    event_slug = data.get('event_slug')
    language_code = data.get('language_code')
    if not event_slug or not language_code:
        raise HTTPException(status_code=400, detail="Missing event_slug or language_code")

    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from portal.database import get_session
    from portal.models import DBBooth, Event

    async with get_session() as session:
        stmt = select(DBBooth).join(Event).options(selectinload(DBBooth.event)).where(
            Event.slug == event_slug,
            DBBooth.language_code == language_code
        )
        db_booth = await session.scalar(stmt)

        if not db_booth or not db_booth.transcription_enabled:
            return {"status": "disabled", "message": "Transcription is not enabled for this booth."}

        provider = db_booth.transcription_provider
        model_size = db_booth.transcription_model

        from portal.transcription import ProviderEnum, get_api_key
        try:
            provider_enum = ProviderEnum(provider)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid transcription provider")

        if not db_booth.event.transcription_api_enabled and provider_enum != ProviderEnum.LOCAL:
            raise HTTPException(status_code=400, detail="External API transcription is disabled for this event.")

        try:
            api_key = get_api_key(db_booth.event, provider_enum)
        except ValueError:
            raise HTTPException(status_code=400, detail="API Key decryption failed. The encryption key has rotated. Please go to the Admin portal, clear your existing keys, and re-enter them.")

        if provider_enum != ProviderEnum.LOCAL and not api_key:
            raise HTTPException(status_code=400, detail=f"{provider} API key missing. Cannot start transcription.")

        from portal.transcription import ProviderConfig
        config = ProviderConfig(api_key=api_key)
        room_id = db_booth.room_id

    try:
        await start_transcription_worker(event_slug, language_code, booth_id, broadcast_transcription, provider, model_size, config, room_id=room_id)
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e))
    return {"status": "started", "provider": provider, "model": model_size}

@app.post('/api/booth/{booth_id}/transcription/stop')
async def api_transcription_stop(
    booth_id: str,
    token: str = Query(''),
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    _require_access(credentials, token)
    await stop_transcription_worker(booth_id)
    return {"status": "stopped"}

@app.get('/api/admin/events/{event_id}/rooms/{room_id}/transcripts/{language_code}')
async def api_admin_get_transcripts(
    event_id: int,
    room_id: int,
    language_code: str,
    target_lang: str = Query(None),
    admin: bool = Depends(require_admin)
):
    from sqlalchemy import select

    from portal.database import get_session
    from portal.models import TranscriptSegment, TranscriptTranslation

    async with get_session() as session:
        if target_lang:
            stmt = select(TranscriptTranslation).join(TranscriptSegment).where(
                TranscriptSegment.room_id == room_id,
                TranscriptSegment.language_code == language_code,
                TranscriptTranslation.language_code == target_lang
            ).order_by(TranscriptSegment.created_at)

            result = await session.execute(stmt)
            translations = result.scalars().all()

            return [
                {
                    "id": t.id,
                    "text": t.text,
                    "created_at": t.created_at.isoformat()
                }
                for t in translations
            ]
        else:
            stmt = select(TranscriptSegment).where(
                TranscriptSegment.room_id == room_id,
                TranscriptSegment.language_code == language_code
            ).order_by(TranscriptSegment.created_at)

            result = await session.execute(stmt)
            segments = result.scalars().all()

            return [
                {
                    "id": s.id,
                    "text": s.text,
                    "created_at": s.created_at.isoformat()
                }
                for s in segments
            ]


def main() -> None:
    import uvicorn
    uvicorn.run('fastapi_app:app', host=settings.host, port=settings.port, reload=settings.debug)


if __name__ == '__main__':
    main()
