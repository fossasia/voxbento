from __future__ import annotations

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from portal.auth import (
    booth_session_key,
    coordinates_room,
    create_embed_token,
    create_listener_token,
    decode_token,
    get_booth_session,
    get_booth_sessions,
    load_membership_scope,
    resolve_booth_role,
    security,
)
from portal.booth_identity import make_booth_id, make_mediamtx_path, validate_event_slug, validate_language_code
from portal.config import settings
from portal.database import (
    create_invite_token,
    delete_booth,
    get_event_by_slug,
    get_session,
    get_user_by_id,
    list_rooms_for_event,
    log_usage_metric,
    verify_api_key,
)
from portal.globals import booths
from portal.models import DBBooth, Event, Room
from portal.rate_limit import check_rate_limit
from portal.roles import INTERPRETER, Permission, can_set_active, has_permission
from portal.schemas.booth import CreateBoothRequest
from portal.transcription import ProviderConfig, ProviderEnum, get_api_key
from portal.transcription.worker import start_transcription_worker, stop_transcription_worker
from portal.utils import (
    _check_mediamtx,
    _ensure_mediamtx_path,
    _require_access,
    _resolve_whip_url,
    verify_same_origin,
)
from portal.websockets.manager import broadcast_transcription

router = APIRouter(prefix="/api")
bearer_scheme = HTTPBearer(auto_error=False)


async def _verify_event_access(
    session: AsyncSession,
    request: Request,
    event_id: int | None,
    *,
    room_id: int | None = None,
    credentials: HTTPAuthorizationCredentials | None = None,
    token: str = "",
    require_booth_management: bool = False,
    booth_id: str | None = None,
    booth_permission: Permission | None = None,
) -> dict | None:
    """Authorize a legacy API operation against its event and optional room.

    With ``booth_id`` and ``booth_permission``, a signed-in user whose role in that
    booth grants the permission is also allowed.
    """
    if credentials is None and request.headers.get("authorization"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid auth token.")
    configured_token = settings.booth_access_token
    if configured_token and (
        (credentials is not None and credentials.credentials == configured_token) or token == configured_token
    ):
        return

    user_token = request.cookies.get("user_token", "")
    if credentials is not None and credentials.credentials != configured_token:
        user_token = credentials.credentials

    if user_token:
        try:
            payload = decode_token(user_token)
        except pyjwt.InvalidTokenError:
            if credentials is not None and credentials.scheme.lower() == "bearer":
                api_key = await verify_api_key(session, credentials.credentials)
                if api_key is not None and event_id is not None and api_key.event_id == event_id:
                    return
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid auth token.")
        if payload.get("purpose") == "legacy_api" and configured_token:
            return
        if not payload.get("user"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Participant tokens cannot use this API.")

        try:
            user_id = int(payload.get("sub", ""))
        except (TypeError, ValueError):
            user_id = 0
        user = await get_user_by_id(session, user_id)
        if user is None or not user.is_active:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or inactive user.")
        if user.is_admin:
            return

        if event_id is not None:
            scope = await load_membership_scope(session, user.id)
            if event_id in scope["owned_event_ids"]:
                return
            # Room coordinators may be given per event, room or booth (#757).
            if room_id is not None and not require_booth_management:
                room = await session.get(Room, room_id)
                if room is not None and room.event_id == event_id and coordinates_room(scope, room_id, event_id):
                    return
            if booth_id is not None and booth_permission is not None:
                # Same role lookup as the booth page and WebSocket. Pass only the user id
                # so the role comes from the database, never from claims like is_admin.
                booth_role = await resolve_booth_role({"sub": str(user.id)}, booth_id)
                if booth_role is not None and has_permission(booth_role, booth_permission):
                    return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Event access required.")

    if not configured_token and settings.debug:
        return
    if not configured_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="BOOTH_ACCESS_TOKEN is not configured on this server.",
        )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or missing auth token.")


def _verify_participant_scope(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    *,
    event_slug: str,
    room_id: int,
    language_code: str,
    session_payload: dict | None = None,
) -> None:
    """Prevent a participant JWT from being replayed against another booth."""
    payload = None
    if credentials is not None and credentials.credentials != settings.booth_access_token:
        try:
            payload = decode_token(credentials.credentials)
        except pyjwt.InvalidTokenError:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid auth token.")
    if payload is None:
        payload = session_payload if session_payload is not None else get_booth_session(request)
    if not payload or payload.get("user") or payload.get("purpose") == "legacy_api":
        return
    if payload.get("event_slug") != event_slug:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booth event access required.")
    if payload.get("room_id") is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid booth scope.")
    try:
        participant_room_id = int(payload["room_id"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid booth scope.")
    if participant_room_id != room_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booth room access required.")
    if payload.get("language_code") and payload["language_code"] != language_code:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Booth language access required.")


def _require_same_origin_for_cookies(
    request: Request, credentials: HTTPAuthorizationCredentials | None, token: str
) -> None:
    """Reject a cross-site POST that only browser cookies would authorize (CSRF)."""
    # Any other query token falls back to the cookies, so only the configured one skips the check.
    has_configured_token = bool(settings.booth_access_token) and token == settings.booth_access_token
    if credentials is None and not has_configured_token and not verify_same_origin(request):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request origin.")


async def _require_live_interpreter_to_stop(
    request: Request, credentials: HTTPAuthorizationCredentials | None, token: str, booth_id: str
) -> None:
    """Let a booth role that can only go live stop transcription only while it is live (#941).

    Coordinators, event owners and Admins can reassign the live slot, so they can always
    stop, as can the configured token and event API keys.
    """
    configured_token = settings.booth_access_token
    if configured_token and (
        token == configured_token or (credentials is not None and credentials.credentials == configured_token)
    ):
        return
    sessions = list(get_booth_sessions(request).values())
    if credentials is not None:
        try:
            payload = decode_token(credentials.credentials)
        except pyjwt.InvalidTokenError:
            # An event API key; _verify_resource_access already scoped it to this event.
            return
        # Without a configured token anyone can get a legacy_api token, so it proves nothing.
        if payload.get("purpose") == "legacy_api" and configured_token:
            return
        sessions.append(payload)
    if can_set_active(await resolve_booth_role(sessions, booth_id)):
        return
    session_keys = {key for key in map(booth_session_key, sessions) if key}
    if not await booths.can_stop_live(booth_id, session_keys):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the live interpreter can stop transcription."
        )


async def _verify_resource_access(
    session: AsyncSession,
    request: Request,
    event: Event,
    *,
    room_id: int,
    language_code: str,
    credentials: HTTPAuthorizationCredentials | None,
    token: str,
    booth_permission: Permission | None = None,
) -> dict | None:
    """Apply user membership checks while retaining scoped participant access.

    ``booth_permission`` lets a signed-in member of this booth in, and is also
    required of a participant (invite link) session's role.
    """
    if credentials is None and request.headers.get("authorization"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid auth token.")
    booth_id = make_booth_id(event.slug, room_id, language_code) if booth_permission is not None else None
    payload = None
    is_global_credential = bool(
        settings.booth_access_token
        and (
            (credentials is not None and credentials.credentials == settings.booth_access_token)
            or token == settings.booth_access_token
        )
    )
    if credentials is not None and not is_global_credential:
        try:
            payload = decode_token(credentials.credentials)
        except pyjwt.InvalidTokenError:
            # Let the event-scoped verifier distinguish a valid API key from
            # an invalid bearer; never fall back to a cookie for the latter.
            await _verify_event_access(
                session,
                request,
                event.id,
                room_id=room_id,
                credentials=credentials,
                token=token,
            )
            return None
    if payload is None and not is_global_credential:
        payload = get_booth_session(request)
    if credentials is None and payload and payload.get("user") and request.cookies.get("session_token"):
        try:
            await _verify_event_access(
                session, request, event.id, room_id=room_id, booth_id=booth_id, booth_permission=booth_permission
            )
        except HTTPException:
            try:
                participant_payload = decode_token(request.cookies["session_token"])
            except pyjwt.InvalidTokenError:
                raise
            if participant_payload.get("user"):
                raise
            payload = participant_payload
        else:
            return payload
    if credentials is None and payload and not payload.get("user") and request.cookies.get("user_token"):
        try:
            await _verify_event_access(
                session, request, event.id, room_id=room_id, booth_id=booth_id, booth_permission=booth_permission
            )
        except HTTPException:
            # A user cookie without membership must not prevent a valid,
            # independently scoped participant session from being used.
            pass
        else:
            return payload
    if payload and payload.get("user") and not (settings.debug and payload.get("is_admin")):
        await _verify_event_access(
            session,
            request,
            event.id,
            room_id=room_id,
            credentials=credentials,
            token=token,
            booth_id=booth_id,
            booth_permission=booth_permission,
        )
    elif credentials is not None and payload is None:
        # A bearer API key is not a JWT. Resolve it through the event-scoped
        # authorization path instead of falling through to the generic gate.
        await _verify_event_access(
            session, request, event.id, room_id=room_id, credentials=credentials, token=token
        )
    else:
        _require_access(request, credentials, token, session_payload=payload)
    if not is_global_credential:
        _verify_participant_scope(
            request,
            credentials,
            event_slug=event.slug,
            room_id=room_id,
            language_code=language_code,
            session_payload=payload,
        )
    if (
        booth_permission is not None
        and payload
        and not payload.get("user")
        and payload.get("purpose") != "legacy_api"
        and not has_permission(payload.get("role"), booth_permission)
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Your booth role does not permit this.")
    return payload


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
    booth_id = make_booth_id(event_slug, room_id, language_code)
    room_db_id = room_id
    async with get_session() as session:
        event = await session.scalar(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
        if not event:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        await _verify_event_access(
            session,
            request,
            event.id,
            room_id=room_id,
            credentials=credentials,
            token=token,
            require_booth_management=True,
        )
        stmt = (
            select(DBBooth)
            .join(Event)
            .where(Event.slug == event_slug, DBBooth.room_id == room_id, DBBooth.language_code == language_code)
        )
        db_booth = await session.scalar(stmt)
        if db_booth:
            try:
                async with session.begin_nested():
                    await delete_booth(session, db_booth.id)
            except (StaleDataError, ObjectDeletedError):
                pass

    try:
        await stop_transcription_worker(booth_id)
    finally:
        await booths.remove_booth(event_slug, room_db_id, language_code)


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
    async with get_session() as session:
        # Get or Create Event
        event_query = await session.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
        event = event_query.scalar_one_or_none()
        if not event:
            deleted_event = await session.scalar(select(Event).where(Event.slug == event_slug))
            if deleted_event:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
            await _verify_event_access(session, request, None, credentials=credentials, token=token)
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

        await _verify_event_access(
            session,
            request,
            event.id,
            room_id=room.id,
            credentials=credentials,
            token=token,
            require_booth_management=True,
        )

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
            role=INTERPRETER,
            label="API Provisioned",
        )
        await session.flush()

        state["interpreter_invite_url"] = f"{settings.public_base_url}/join/{invite.token}"

    state["caption_url"] = (
        f"wss://{settings.public_base_url.replace('https://', '').replace('http://', '')}/ws/captions/{state['booth_id']}"
    )
    return state


@router.delete(
    "/events/{event_slug}/eventyay-rooms/{eventyay_room_id}/booths/{language_code}",
    status_code=status.HTTP_204_NO_CONTENT,
)
@router.delete(
    "/events/{event_slug}/rooms/{eventyay_room_id}/booths/{language_code}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_event_booth(
    request: Request,
    event_slug: str,
    eventyay_room_id: str,
    language_code: str,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
):
    """Delete a booth provisioned via API."""
    booth_id = ""
    room_id = 0
    async with get_session() as session:
        # Find the event
        event_query = await session.execute(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
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

        await _verify_event_access(
            session,
            request,
            event.id,
            room_id=room.id,
            credentials=credentials,
            token=token,
            require_booth_management=True,
        )
        room_id = room.id
        booth_id = make_booth_id(event_slug, room.id, language_code)

        # Find the booth
        booth_query = await session.execute(
            select(DBBooth).where(
                DBBooth.event_id == event.id, DBBooth.room_id == room.id, DBBooth.language_code == language_code
            )
        )
        booth = booth_query.scalar_one_or_none()

        if booth:
            try:
                async with session.begin_nested():
                    await delete_booth(session, booth.id)
            except (StaleDataError, ObjectDeletedError):
                pass

    try:
        await stop_transcription_worker(booth_id)
    finally:
        await booths.remove_booth(event_slug, room_id, language_code)


@router.get("/events/{event_slug}/booths")
async def list_event_booths(
    request: Request,
    event_slug: str,
    token: str = Query(""),
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict:
    """List all booths for an event."""
    async with get_session() as session:
        event = await session.scalar(select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)))
        if not event:
            await _verify_event_access(session, request, None, credentials=credentials, token=token)
            return {"event_slug": event_slug, "booths": []}
        await _verify_event_access(session, request, event.id, credentials=credentials, token=token)

    booth_list = await booths.list_booths_for_event(event_slug)
    for b in booth_list:
        mtx = b.get("mediamtx_path", "")
        if mtx:
            b["whip_url"] = f"{settings.mediamtx_whip_base}/{mtx}/whip"
            b["whep_url"] = f"{settings.mediamtx_whip_base}/{mtx}/whep"
        b["type"] = "human"
        b["label"] = f"{b.get('language_name', b.get('language_code', ''))} (Human)"

    async with get_session() as session:

        stmt = select(Event).where(Event.slug == event_slug, Event.deleted_at.is_(None)).options(
            selectinload(Event.rooms).selectinload(Room.translation_languages)
        )
        event = await session.scalar(stmt)
        if event:
            for room in event.rooms:
                if room.floor_translation_enabled:
                    for tl in room.translation_languages:
                        if not tl.enabled:
                            continue
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
    try:
        slug = validate_event_slug(event_slug)
        code = validate_language_code(language_code)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if room_id is None:
        async with get_session() as session:
            ev = await get_event_by_slug(session, slug)
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

    async with get_session() as session:
        event = await get_event_by_slug(session, slug)
        if event is None:
            deleted = await session.scalar(select(Event).where(Event.slug == slug))
            if deleted is not None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
            await _verify_event_access(session, request, None, credentials=credentials, token=token)
        else:
            await _verify_resource_access(
                session,
                request,
                event,
                room_id=room_id,
                language_code=code,
                credentials=credentials,
                token=token,
                booth_permission=Permission.BOOTH_VIEW,
            )
    state = await booths.get_booth_for_event(slug, room_id, code)
    if state is None:
        # Booth state lives in memory, so it is empty after a restart and for
        # booths added in the admin UI. Load booths that exist in the database
        # instead of failing the booth page; unknown booths still get a 404.
        # Match the in-memory lookup: normalised coordinates, live events only,
        # and the room must belong to the event.
        async with get_session() as session:
            stmt = (
                select(DBBooth)
                .join(Event, Event.id == DBBooth.event_id)
                .join(Room, Room.id == DBBooth.room_id)
                .where(
                    Event.slug == slug,
                    Event.deleted_at.is_(None),
                    Room.event_id == Event.id,
                    DBBooth.room_id == room_id,
                    DBBooth.language_code == code,
                )
            )
            db_booth = await session.scalar(stmt)
        if db_booth is not None:
            state = await booths.snapshot(
                booth_id=make_booth_id(slug, room_id, code),
                language=db_booth.language_name,
                channel_id=make_mediamtx_path(slug, room_id, code),
                room_id=room_id,
            )
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

    async with get_session() as session:
        event = await get_event_by_slug(session, event_slug)
        if event is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        auth_payload = await _verify_resource_access(
            session,
            request,
            event,
            room_id=room_id,
            language_code=language_code,
            credentials=credentials,
            token=token,
        )
        if auth_payload and not auth_payload.get("user") and auth_payload.get("purpose") != "legacy_api":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Participant credentials cannot request an unbound WHIP URL.",
            )

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
    booth_id = make_booth_id(event_slug, room_id, language_code)
    async with get_session() as session:
        event = await get_event_by_slug(session, event_slug)
        if event is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        await _verify_resource_access(
            session,
            request,
            event,
            room_id=room_id,
            language_code=language_code,
            credentials=credentials,
            token=token,
            booth_permission=Permission.BOOTH_GO_LIVE,
        )
        _require_same_origin_for_cookies(request, credentials, token)
        stmt = (
            select(DBBooth)
            .join(Event)
            .options(selectinload(DBBooth.event))
            .where(
                Event.slug == event_slug,
                Event.deleted_at.is_(None),
                DBBooth.room_id == room_id,
                DBBooth.language_code == language_code,
            )
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
    booth_id = make_booth_id(event_slug, room_id, language_code)
    async with get_session() as session:
        event = await get_event_by_slug(session, event_slug)
        if event is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found")
        await _verify_resource_access(
            session,
            request,
            event,
            room_id=room_id,
            language_code=language_code,
            credentials=credentials,
            token=token,
            booth_permission=Permission.BOOTH_GO_LIVE,
        )
        _require_same_origin_for_cookies(request, credentials, token)
    await _require_live_interpreter_to_stop(request, credentials, token, booth_id)
    await stop_transcription_worker(booth_id)
    return {"status": "stopped"}
