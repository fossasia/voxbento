from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from portal.auth import security
from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.config import settings
from portal.database import get_session
from portal.globals import booths
from portal.models import DBBooth, Event
from portal.schemas.booth import CreateBoothRequest
from portal.transcription import ProviderConfig, ProviderEnum, get_api_key
from portal.transcription.worker import start_transcription_worker, stop_transcription_worker
from portal.utils import _check_mediamtx, _ensure_mediamtx_path, _require_access, _resolve_whip_url
from portal.websockets.manager import broadcast_transcription

router = APIRouter(prefix="/api")




@router.post("/events/{event_slug}/booths", status_code=status.HTTP_201_CREATED)
async def create_event_booth(
    event_slug: str,
    body: CreateBoothRequest,
    token: str = Query(""),
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
    mediamtx_path = state["mediamtx_path"]
    await _ensure_mediamtx_path(mediamtx_path)
    state["whip_url"] = f"{settings.mediamtx_whip_base}/{mediamtx_path}/whip"
    state["whep_url"] = f"{settings.mediamtx_whip_base}/{mediamtx_path}/whep"
    return state


@router.get("/events/{event_slug}/booths")
async def list_event_booths(
    event_slug: str, token: str = Query(""), credentials: HTTPAuthorizationCredentials | None = Depends(security)
) -> dict:
    """List all booths for an event."""
    _require_access(credentials, token)
    booth_list = await booths.list_booths_for_event(event_slug)
    for b in booth_list:
        mtx = b.get("mediamtx_path", "")
        if mtx:
            b["whip_url"] = f"{settings.mediamtx_whip_base}/{mtx}/whip"
            b["whep_url"] = f"{settings.mediamtx_whip_base}/{mtx}/whep"
    return {"event_slug": event_slug, "booths": booth_list}


@router.get("/events/{event_slug}/booths/{language_code}/state")
async def event_booth_state(
    event_slug: str,
    language_code: str,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Event-scoped booth state — never auto-creates a booth."""
    _require_access(credentials, token)
    state = await booths.get_booth_for_event(event_slug, language_code)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No booth for language '{language_code}' in event '{event_slug}'.",
        )
    return state


@router.get("/events/{event_slug}/booths/{language_code}/whip-url")
async def event_booth_whip_url(
    event_slug: str,
    language_code: str,
    participant_id: str = Query(...),
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Event-scoped WHIP URL — validates event ownership before returning."""
    _require_access(credentials, token)
    booth_id = make_booth_id(event_slug, language_code)
    channel_id = make_mediamtx_path(event_slug, language_code)
    try:
        await booths.validate_booth_event(booth_id, event_slug)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return await _resolve_whip_url(booth_id, participant_id, language_code.upper(), channel_id)


@router.get("/interpreter/status/{channel_id:path}")
async def ingest_status_api(channel_id: str) -> dict:
    """Returns MediaMTX reachability — used by the frontend preflight check."""
    return {"channel_id": channel_id, "state": "mediamtx", "reachable": await _check_mediamtx()}


@router.post("/events/{event_slug}/booths/{language_code}/transcription/start")
async def api_transcription_start(
    event_slug: str,
    language_code: str,
    request: Request,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    _require_access(credentials, token)
    booth_id = make_booth_id(event_slug, language_code)
    async with get_session() as session:
        stmt = (
            select(DBBooth)
            .join(Event)
            .options(selectinload(DBBooth.event))
            .where(Event.slug == event_slug, DBBooth.language_code == language_code)
        )
        db_booth = await session.scalar(stmt)
        if not db_booth or not db_booth.transcription_enabled:
            return {"status": "disabled", "message": "Transcription is not enabled for this booth."}
        provider = db_booth.transcription_provider
        model_size = db_booth.transcription_model
        try:
            provider_enum = ProviderEnum(provider)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid transcription provider")
        if not db_booth.event.transcription_api_enabled and provider_enum != ProviderEnum.LOCAL:
            raise HTTPException(status_code=400, detail="External API transcription is disabled for this event.")
        try:
            api_key = get_api_key(db_booth.event, provider_enum)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="API Key decryption failed. The encryption key has rotated. Please go to the Admin portal, clear your existing keys, and re-enter them.",
            )
        if provider_enum != ProviderEnum.LOCAL and (not api_key):
            raise HTTPException(status_code=400, detail=f"{provider} API key missing. Cannot start transcription.")
        config = ProviderConfig(api_key=api_key)
        room_id = db_booth.room_id
    try:
        await start_transcription_worker(
            event_slug, language_code, booth_id, broadcast_transcription, provider, model_size, config, room_id=room_id
        )
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e))
    return {"status": "started", "provider": provider, "model": model_size}


@router.post("/events/{event_slug}/booths/{language_code}/transcription/stop")
async def api_transcription_stop(
    event_slug: str,
    language_code: str,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security)
):
    _require_access(credentials, token)
    booth_id = make_booth_id(event_slug, language_code)
    await stop_transcription_worker(booth_id)
    return {"status": "stopped"}
