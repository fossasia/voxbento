from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from portal.auth import create_embed_token, create_listener_token, security
from portal.booth_identity import make_booth_id, make_mediamtx_path
from portal.config import settings
from portal.database import (
    create_invite_token,
    delete_booth,
    get_event_by_slug,
    get_session,
    list_rooms_for_event,
    log_usage_metric,
    verify_api_key,
)
from portal.globals import booths
from portal.models import DBBooth, Event, Room
from portal.rate_limit import check_rate_limit
from portal.schemas.booth import CreateBoothRequest
from portal.transcription import ProviderConfig, ProviderEnum, get_api_key
from portal.transcription.worker import start_transcription_worker, stop_transcription_worker
from portal.utils import _check_mediamtx, _ensure_mediamtx_path, _require_access, _resolve_whip_url
from portal.websockets.manager import broadcast_transcription

router = APIRouter(prefix="/api")
bearer_scheme = HTTPBearer(auto_error=False)


@router.post("/v1/tokens/listener", status_code=status.HTTP_201_CREATED)
async def provision_listener_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    purpose: str | None = Query(None),
):
    """Issue a listener JWT for a third-party client.

    - Default (no purpose param): 4-hour token for WebSocket listener use.
    - purpose=embed: 30-minute token for embedding in an iframe src= attribute.
      The shorter lifetime limits exposure since the token is visible in the
      third party's page HTML source.
    """
    ip_address = request.client.host if request.client else "unknown"
    if not check_rate_limit("api_token_provision", ip_address, max_requests=60, window_seconds=60):
        raise HTTPException(status_code=429, detail="Too many requests")

    if not credentials or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing or invalid Bearer token")

    async with get_session() as session:
        key = await verify_api_key(session, credentials.credentials)
        if not key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API Key")

        await log_usage_metric(session, key.event_id, "listener_token_issued")
        if purpose == "embed":
            token = create_embed_token(event_slug=key.event.slug)
        else:
            token = create_listener_token(event_slug=key.event.slug)
        return {"token": token}


@router.delete("/events/{event_slug}/rooms/{room_id}/booths/{language_code}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_booth_by_language(
    request: Request,
    event_slug: str,
    room_id: int,
    language_code: str,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Delete a booth from an event."""
    _require_access(request, credentials, token)

    booth_id = make_booth_id(event_slug, room_id, language_code)
    await stop_transcription_worker(booth_id)

    # Delete from DB
    async with get_session() as session:
        stmt = (
            select(DBBooth)
            .join(Event)
            .where(Event.slug == event_slug, DBBooth.room_id == room_id, DBBooth.language_code == language_code)
        )
        db_booth = await session.scalar(stmt)
        if db_booth:
            await delete_booth(session, db_booth.id)

    # Remove from in memory state
    await booths.remove_booth(event_slug, room_id, language_code)


@router.post("/events/{event_slug}/booths", status_code=status.HTTP_201_CREATED)
async def create_event_booth(
    request: Request,
    event_slug: str,
    body: CreateBoothRequest,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Create a booth for an event.

    Returns the booth state including derived booth_id, MediaMTX path,
    WHIP URL, and WHEP URL.
    """
    _require_access(request, credentials, token)

    async with get_session() as session:
        # Get or Create Event
        event_query = await session.execute(select(Event).where(Event.slug == event_slug))
        event = event_query.scalar_one_or_none()
        if not event:
            try:
                event = Event(slug=event_slug, display_name=event_slug.title())
            except ValueError as exc:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
            session.add(event)
            await session.flush()

        # Get or Create Room
        room_id = body.room_id
        if room_id is None:
            rooms = await list_rooms_for_event(session, event.id)
            if len(rooms) > 1:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST, detail="Event has multiple rooms. room_id is required."
                )
            if rooms:
                room = rooms[0]
            else:
                room = Room(event_id=event.id, display_name=body.room_name or "Main Room")
                session.add(room)
                await session.flush()
        else:
            room_query = await session.execute(select(Room).where(Room.event_id == event.id, Room.id == room_id))
            room = room_query.scalar_one_or_none()
            display_name = body.room_name or f"Room {room_id}"
            if not room:
                room = Room(event_id=event.id, display_name=display_name)
                # Don't force id=room_id to avoid unique constraint violations
                session.add(room)
                await session.flush()
            elif room.display_name != display_name:
                room.display_name = display_name
                await session.flush()

        db_room_id = room.id

        # Create Booth in memory state
        try:
            state = await booths.create_booth(
                event_slug=event_slug,
                language_code=body.language_code,
                language=body.language or body.language_code.upper(),
                instance=body.instance,
                room_id=db_room_id,
            )
        except ValueError as exc:
            if "already exists" in str(exc):
                booth_id = make_booth_id(event_slug, db_room_id, body.language_code)
                mtx_path = make_mediamtx_path(event_slug, db_room_id, body.language_code)
                state = await booths.snapshot(
                    booth_id=booth_id,
                    language=body.language or body.language_code.upper(),
                    channel_id=mtx_path,
                    room_id=db_room_id,
                )
            else:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

        mediamtx_path = state["mediamtx_path"]
        await _ensure_mediamtx_path(mediamtx_path)
        state["whip_url"] = f"{settings.mediamtx_whip_base}/{mediamtx_path}/whip"
        state["whep_url"] = f"{settings.mediamtx_whip_base}/{mediamtx_path}/whep"

        # Get or Create DBBooth
        booth_query = await session.execute(
            select(DBBooth).where(
                DBBooth.event_id == event.id, DBBooth.room_id == db_room_id, DBBooth.language_code == body.language_code
            )
        )
        db_booth = booth_query.scalar_one_or_none()
        if not db_booth:
            db_booth = DBBooth(
                event_id=event.id,
                room_id=db_room_id,
                language_code=body.language_code,
                language_name=body.language or body.language_code.upper(),
            )
            session.add(db_booth)
            await session.flush()

        # Generate InviteToken
        invite = await create_invite_token(
            session,
            booth_id=db_booth.id,
            role="interpreter",
            label="API Provisioned",
        )
        await session.flush()

        state["interpreter_invite_url"] = f"{settings.public_base_url}/join/{invite.token}"

    state["caption_url"] = (
        f"wss://{settings.public_base_url.replace('https://', '').replace('http://', '')}/ws/captions/{state['booth_id']}"
    )
    return state


@router.delete(
    "/events/{event_slug}/rooms/{eventyay_room_id}/booths/{language_code}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_event_booth(
    event_slug: str,
    eventyay_room_id: str,
    language_code: str,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Delete a booth provisioned via API."""
    _require_access(credentials, token)

    async with get_session() as session:
        # Find the event
        event_query = await session.execute(select(Event).where(Event.slug == event_slug))
        event = event_query.scalar_one_or_none()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")

        # Find the room
        room_query = await session.execute(
            select(Room).where(Room.event_id == event.id, Room.eventyay_room_id == eventyay_room_id)
        )
        room = room_query.scalar_one_or_none()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")

        # Find the booth
        booth_query = await session.execute(
            select(DBBooth).where(
                DBBooth.event_id == event.id, DBBooth.room_id == room.id, DBBooth.language_code == language_code
            )
        )
        booth = booth_query.scalar_one_or_none()

        if booth:
            await session.delete(booth)
            await session.flush()


@router.get("/events/{event_slug}/booths")
async def list_event_booths(
    request: Request,
    event_slug: str,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """List all booths for an event."""
    try:
        _require_access(request, credentials, token)
    except HTTPException as orig_exc:
        # Fallback to checking API Key
        async with get_session() as session:
            key = None
            if credentials and credentials.scheme.lower() == 'bearer':
                key = await verify_api_key(session, credentials.credentials)
            if not key:
                raise orig_exc

    booth_list = await booths.list_booths_for_event(event_slug)
    for b in booth_list:
        mtx = b.get("mediamtx_path", "")
        if mtx:
            b["whip_url"] = f"{settings.mediamtx_whip_base}/{mtx}/whip"
            b["whep_url"] = f"{settings.mediamtx_whip_base}/{mtx}/whep"
        b["type"] = "human"
        b["label"] = f"{b.get('language_name', b.get('language_code', ''))} (Human)"

    async with get_session() as session:
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        from portal.models import Event, Room

        stmt = select(Event).where(Event.slug == event_slug).options(
            selectinload(Event.rooms).selectinload(Room.translation_languages)
        )
        event = await session.scalar(stmt)
        if event:
            for room in event.rooms:
                if room.floor_translation_enabled:
                    for tl in room.translation_languages:
                        ai_stream = {
                            "id": f"ai_{room.id}_{tl.language_code}",
                            "room_id": room.id,
                            "eventyay_room_id": room.eventyay_room_id,
                            "language_code": tl.language_code,
                            "language_name": tl.language_name,
                            "type": "ai",
                            "label": f"{tl.language_name} (AI)",
                            "is_ai": True,
                            "floor_tts_enabled": room.floor_tts_enabled,
                        }
                        booth_list.append(ai_stream)

    return {"event_slug": event_slug, "booths": booth_list}


@router.get("/events/{event_slug}/booths/{language_code}/state")
async def event_booth_state(
    request: Request,
    event_slug: str,
    language_code: str,
    room_id: int | None = Query(None),
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Event-scoped booth state — never auto-creates a booth."""
    _require_access(request, credentials, token)

    if room_id is None:
        async with get_session() as session:
            ev = await get_event_by_slug(session, event_slug)
            if ev:
                rooms = await list_rooms_for_event(session, ev.id)
                if len(rooms) > 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail="Event has multiple rooms. room_id is required."
                    )
                if rooms:
                    room_id = rooms[0].id
        if room_id is None:
            room_id = 1

    state = await booths.get_booth_for_event(event_slug, room_id, language_code)
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No booth for language '{language_code}' in event '{event_slug}'.",
        )
    return state


@router.get("/events/{event_slug}/booths/{language_code}/whip-url")
async def event_booth_whip_url(
    request: Request,
    event_slug: str,
    language_code: str,
    participant_id: str = Query(...),
    room_id: int | None = Query(None),
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """Event-scoped WHIP URL — validates event ownership before returning."""
    _require_access(request, credentials, token)

    if room_id is None:
        async with get_session() as session:
            ev = await get_event_by_slug(session, event_slug)
            if ev:
                rooms = await list_rooms_for_event(session, ev.id)
                if len(rooms) > 1:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST, detail="Event has multiple rooms. room_id is required."
                    )
                if rooms:
                    room_id = rooms[0].id
        if room_id is None:
            room_id = 1

    booth_id = make_booth_id(event_slug, room_id, language_code)
    channel_id = make_mediamtx_path(event_slug, room_id, language_code)
    try:
        await booths.validate_booth_event(booth_id, event_slug)
    except PermissionError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return await _resolve_whip_url(booth_id, participant_id, language_code.upper(), channel_id)


@router.get("/interpreter/status/{channel_id:path}")
async def ingest_status_api(channel_id: str) -> dict:
    """Returns MediaMTX reachability — used by the frontend preflight check."""
    return {"channel_id": channel_id, "state": "mediamtx", "reachable": await _check_mediamtx()}


@router.post("/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcription/start")
async def api_transcription_start(
    event_slug: str,
    room_id: int,
    language_code: str,
    request: Request,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    _require_access(request, credentials, token)
    booth_id = make_booth_id(event_slug, room_id, language_code)
    async with get_session() as session:
        stmt = (
            select(DBBooth)
            .join(Event)
            .options(selectinload(DBBooth.event))
            .where(Event.slug == event_slug, DBBooth.room_id == room_id, DBBooth.language_code == language_code)
        )
        db_booth = await session.scalar(stmt)
        if not db_booth or not db_booth.transcription_enabled:
            print(
                "API START: Transcription disabled for booth",
                booth_id,
                "db_booth=",
                db_booth,
                "enabled=",
                getattr(db_booth, "transcription_enabled", None),
            )
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
    try:
        await start_transcription_worker(
            event_slug, language_code, booth_id, broadcast_transcription, provider, model_size, config, room_id=room_id
        )
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e))
    print("API START: Successfully started worker for booth", booth_id)
    return {"status": "started", "provider": provider, "model": model_size}


@router.post("/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcription/stop")
async def api_transcription_stop(
    request: Request,
    event_slug: str,
    room_id: int,
    language_code: str,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    _require_access(request, credentials, token)
    booth_id = make_booth_id(event_slug, room_id, language_code)
    await stop_transcription_worker(booth_id)
    return {"status": "stopped"}
