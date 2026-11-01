"""Event-owner routes for "Sign in with YouTube" (#1087).

Read ``docs/youtube-connect.md`` first. The OAuth state (nonce, PKCE verifier, event,
room and user) travels in a short-lived signed cookie scoped to the callback path; the
callback re-checks the signed-in user and their ownership before storing anything.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from portal import youtube_account as yt
from portal.auth import get_current_user, require_event_owner, resolve_principal
from portal.config import settings
from portal.crypto import decrypt_val, encrypt_val
from portal.database import get_event_by_id, get_room_by_id, get_session
from portal.models import YouTubeConnection
from portal.program_ingest import room_lock
from portal.routers.program_ingest import _check_origin
from portal.utils import safe_redirect

router = APIRouter()
logger = logging.getLogger(__name__)

EVENT_PATH = "/admin/events/{event_id}/youtube"
BROADCAST_PATH = "/admin/events/{event_id}/rooms/{room_id}/youtube/broadcast"
CALLBACK_PATH = "/youtube/oauth/callback"
STATE_COOKIE = "vb_yt_oauth"
STATE_COOKIE_PATH = "/youtube/oauth"
STATE_MAX_AGE = 600
STATE_AUDIENCE = "voxbento:youtube-oauth"
NO_STORE = {"Cache-Control": "no-store"}


class BroadcastChoice(BaseModel):
    # A pasted watch/live/Studio link or a bare video id.
    broadcast: str = Field(min_length=1, max_length=300)


def _require_enabled() -> None:
    if not settings.youtube_connect_enabled:
        raise HTTPException(404, "Not Found")


def _room_page(event_id: int, room_id: int, outcome: str, owner: bool) -> str:
    # Event owners use the workspace namespace; an Admin without ownership stays in /admin.
    prefix = "/workspace" if owner else "/admin"
    return f"{prefix}/events/{event_id}/rooms/{room_id}/?youtube={outcome}#audio"


def _finish(target: str) -> Response:
    response = safe_redirect(target)
    response.delete_cookie(
        STATE_COOKIE, path=STATE_COOKIE_PATH, secure=not settings.debug, httponly=True, samesite="lax"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get(EVENT_PATH, dependencies=[Depends(require_event_owner)])
async def youtube_connection_status(event_id: int) -> JSONResponse:
    if not settings.youtube_connect_enabled:
        return JSONResponse({"available": False, "connected": False}, headers=NO_STORE)
    async with get_session() as session:
        if await get_event_by_id(session, event_id) is None:
            raise HTTPException(404, "Event not found")
        connection = await yt.get_connection(session, event_id)
    return JSONResponse(
        {
            "available": True,
            "connected": connection is not None,
            "channel_title": connection.channel_title if connection else None,
            "channel_id": connection.channel_id if connection else None,
            "connected_at": connection.updated_at.isoformat() if connection else None,
            "last_error": connection.last_error if connection else None,
        },
        headers=NO_STORE,
    )


@router.get(f"{EVENT_PATH}/connect", dependencies=[Depends(require_event_owner)])
async def youtube_connect(request: Request, event_id: int, room_id: int) -> Response:
    """Start the Google consent flow; Google sends the owner back to the callback."""
    _require_enabled()
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(403, "Sign-in required.")
    async with get_session() as session:
        room = await get_room_by_id(session, room_id)
        if not room or room.event_id != event_id:
            raise HTTPException(404, "Room not found")
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = yt.pkce_pair()
    state = jwt.encode(
        {
            "nonce": nonce,
            "verifier": verifier,
            "event_id": event_id,
            "room_id": room_id,
            "sub": str(user["sub"]),
            "aud": STATE_AUDIENCE,
            "exp": datetime.now(timezone.utc) + timedelta(seconds=STATE_MAX_AGE),
        },
        settings.effective_jwt_secret,
        algorithm="HS256",
    )
    response = RedirectResponse(yt.authorization_url(nonce, challenge), status_code=303)
    response.set_cookie(
        STATE_COOKIE,
        state,
        max_age=STATE_MAX_AGE,
        path=STATE_COOKIE_PATH,
        httponly=True,
        secure=not settings.debug,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    logger.info("youtube event_id=%s room_id=%s action=connect_started", event_id, room_id)
    return response


@router.get(CALLBACK_PATH, include_in_schema=False)
async def youtube_oauth_callback(request: Request, state: str = "", code: str = "", error: str = "") -> Response:
    _require_enabled()
    try:
        claims = jwt.decode(
            request.cookies.get(STATE_COOKIE, ""),
            settings.effective_jwt_secret,
            algorithms=["HS256"],
            audience=STATE_AUDIENCE,
        )
        event_id, room_id, nonce = int(claims["event_id"]), int(claims["room_id"]), str(claims["nonce"])
        verifier, sub = str(claims["verifier"]), str(claims["sub"])
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise HTTPException(400, "The YouTube sign-in expired. Start again from the room page.") from None
    if not state or not secrets.compare_digest(state.encode(), nonce.encode()):
        raise HTTPException(400, "The YouTube sign-in did not match. Start again from the room page.")
    user = await get_current_user(request)
    if user is None or str(user.get("sub")) != sub:
        raise HTTPException(403, "Sign in to VoxBento as the person who started the YouTube sign-in.")
    async with get_session() as session:
        principal = await resolve_principal(request, session)
    owner = event_id in principal["owned_event_ids"]
    if not (principal["is_admin"] or owner):
        raise HTTPException(403, "You don't have access to this event.")

    if error or not code:
        # The owner cancelled on Google's consent screen (error=access_denied) or Google sent nothing.
        return _finish(_room_page(event_id, room_id, "cancelled", owner))
    try:
        tokens = await yt.exchange_code(code, verifier)
        channel = await yt.my_channel(tokens["access_token"])
    except (yt.YouTubeAuthError, yt.YouTubeAPIError) as exc:
        logger.warning("youtube event_id=%s action=connect_failed reason=%s", event_id, type(exc).__name__)
        return _finish(_room_page(event_id, room_id, "error", owner))
    if channel is None:
        await yt.revoke(tokens["refresh_token"])
        return _finish(_room_page(event_id, room_id, "no-channel", owner))
    channel_id, channel_title = channel

    old_token = None
    try:
        async with get_session() as session:
            if await get_event_by_id(session, event_id) is None:
                raise HTTPException(404, "Event not found")
            connection = await yt.get_connection(session, event_id)
            if connection is None:
                connection = YouTubeConnection(event_id=event_id)
                session.add(connection)
            elif connection.channel_id != channel_id and not await yt.channel_shared(session, connection):
                # Same channel: Google keeps one grant, so revoking the old token would revoke the new one.
                try:
                    old_token = decrypt_val(connection.refresh_token_enc)
                except ValueError:
                    old_token = None
            connection.channel_id = channel_id
            connection.channel_title = channel_title
            connection.refresh_token_enc = encrypt_val(tokens["refresh_token"])
            connection.scope = tokens["scope"][:500]
            connection.connected_by_user_id = int(sub)
            connection.last_error = None
            room_ids = await yt.room_ids_for_event(session, event_id)
    except RuntimeError:
        # No API_KEY_ENCRYPTION_KEY: the token cannot be stored, so give it back.
        await yt.revoke(tokens["refresh_token"])
        logger.warning("youtube event_id=%s action=connect_failed reason=encryption_key", event_id)
        return _finish(_room_page(event_id, room_id, "error", owner))
    yt.forget_event(event_id, room_ids)
    await yt.revoke(old_token)
    logger.info("youtube event_id=%s action=connected", event_id)
    return _finish(_room_page(event_id, room_id, "connected", owner))


@router.get(f"{EVENT_PATH}/broadcasts", dependencies=[Depends(require_event_owner)])
async def youtube_broadcasts(event_id: int) -> JSONResponse:
    _require_enabled()
    auth_error = None
    async with get_session() as session:
        connection = await yt.get_connection(session, event_id)
        if connection is None:
            raise HTTPException(409, "Sign in with YouTube first.")
        try:
            broadcasts = await yt.list_broadcasts(connection)
        except yt.YouTubeAuthError as exc:
            # Commit last_error so the room page shows why the list is empty.
            auth_error = str(exc)
            connection.last_error = auth_error
        except yt.YouTubeAPIError as exc:
            raise HTTPException(502, yt.describe(exc)) from None
    if auth_error:
        return JSONResponse({"detail": auth_error, "last_error": auth_error}, status_code=409, headers=NO_STORE)
    return JSONResponse({"broadcasts": [b.summary() for b in broadcasts]}, headers=NO_STORE)


@router.put(BROADCAST_PATH, dependencies=[Depends(require_event_owner)])
async def youtube_broadcast_set(request: Request, event_id: int, room_id: int, choice: BroadcastChoice) -> JSONResponse:
    """Link the room to a broadcast; in relay mode this also fills in the relay destination."""
    _check_origin(request)
    _require_enabled()
    auth_error = None
    async with room_lock(room_id):
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            if not room or room.event_id != event_id:
                raise HTTPException(404, "Room not found")
            connection = await yt.get_connection(session, event_id)
            if connection is None:
                raise HTTPException(409, "Sign in with YouTube first.")
            try:
                broadcast = await yt.link_broadcast(session, room, connection, choice.broadcast)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            except RuntimeError:
                raise HTTPException(409, "The server encryption key is not configured; ask the operator.") from None
            except yt.YouTubeAuthError as exc:
                # Nothing on the room changed; commit last_error only.
                auth_error = str(exc)
                connection.last_error = auth_error
            except yt.YouTubeAPIError as exc:
                raise HTTPException(502, yt.describe(exc)) from None
            else:
                body = {
                    "broadcast_id": broadcast.id,
                    "title": broadcast.title,
                    "life_cycle_status": broadcast.life_cycle_status,
                    "stream_bound": bool(room.youtube_stream_id),
                    # Write-only like the manual form: whether a destination exists, never the key.
                    "destination_configured": bool(room.program_relay_server and room.program_relay_key_enc),
                }
    if auth_error:
        return JSONResponse({"detail": auth_error, "last_error": auth_error}, status_code=409, headers=NO_STORE)
    return JSONResponse(body, headers=NO_STORE)


@router.delete(BROADCAST_PATH, dependencies=[Depends(require_event_owner)])
async def youtube_broadcast_clear(request: Request, event_id: int, room_id: int) -> JSONResponse:
    """Unlink the broadcast. The relay destination stays until the owner removes it."""
    _check_origin(request)
    _require_enabled()
    async with room_lock(room_id):
        async with get_session() as session:
            room = await get_room_by_id(session, room_id)
            if not room or room.event_id != event_id:
                raise HTTPException(404, "Room not found")
            room.youtube_broadcast_id = None
            room.youtube_stream_id = None
    yt.forget_room(room_id)
    logger.info("youtube event_id=%s room_id=%s action=broadcast_unlinked", event_id, room_id)
    return JSONResponse({"broadcast_id": None}, headers=NO_STORE)


@router.delete(EVENT_PATH, dependencies=[Depends(require_event_owner)])
async def youtube_disconnect(request: Request, event_id: int) -> JSONResponse:
    """Revoke VoxBento's access at Google and forget the channel for this event."""
    _check_origin(request)
    _require_enabled()
    token = None
    room_ids: list[int] = []
    async with get_session() as session:
        connection = await yt.get_connection(session, event_id)
        if connection is not None:
            if not await yt.channel_shared(session, connection):
                try:
                    token = decrypt_val(connection.refresh_token_enc)
                except ValueError:
                    token = None
            room_ids = await yt.unlink_event_rooms(session, event_id)
            await session.delete(connection)
    yt.forget_event(event_id, room_ids)
    await yt.revoke(token)
    logger.info("youtube event_id=%s action=disconnected", event_id)
    return JSONResponse({"connected": False}, headers=NO_STORE)
