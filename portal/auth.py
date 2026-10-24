from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from portal.config import settings
from portal.database import get_db_session
from portal.models import User
from portal.roles import ADMIN, EVENT_OWNER, INTERPRETER, ROLE_RANK, ROOM_COORDINATOR, highest_role, is_at_least

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_token() -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_expiry_seconds),
        "purpose": "legacy_api",
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


def create_participant_token(*, booth_id: int, role: str, event_slug: str, room_id: int, language_code: str) -> str:
    """Create a JWT with role claims for a participant who joined via invite link."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "booth_id": booth_id,
        "role": role,
        "event_slug": event_slug,
        "room_id": room_id,
        "language_code": language_code,
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


# Listening is open to everyone with the event link or join code; it is not a
# role (#757). Listener tokens are marked with this "kind" instead.
LISTENER_TOKEN_KIND = "listener"


def is_listener_token(payload: dict | None) -> bool:
    """True for listener and embed tokens, including ones issued before #757 (``role: "listener"``)."""
    if not isinstance(payload, dict):
        return False
    return payload.get("kind") == LISTENER_TOKEN_KIND or payload.get("role") == LISTENER_TOKEN_KIND


def create_listener_token(*, event_slug: str) -> str:
    """Create a short-lived JWT for listener access via API."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "kind": LISTENER_TOKEN_KIND,
        "event_slug": event_slug,
        "iat": now,
        "exp": now + timedelta(seconds=settings.listener_token_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


def create_embed_token(*, event_slug: str) -> str:
    """Create a short-lived JWT for embed iframe access.

    Identical claim shape to create_listener_token (kind='listener', event_slug)
    so it passes both the embed route's claim checks and the /ws/captions
    WebSocket auth's booth_id.startswith(event_slug + '-') check.
    Uses a shorter expiry (embed_token_expiry_seconds, default 30 min) because
    the token is visible in the iframe src= attribute in the third party's HTML.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "kind": LISTENER_TOKEN_KIND,
        "purpose": "embed",
        "event_slug": event_slug,
        "iat": now,
        "exp": now + timedelta(seconds=settings.embed_token_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.effective_jwt_secret, algorithms=["HS256"])


class WSAuthError(Exception):
    """Raised when WebSocket authentication fails."""

    pass


async def _current_user(payload: dict) -> User | None:
    """Resolve the live ``User`` row for a decoded ``user_token`` payload.

    Authorization must reflect current database state, not the claims frozen
    into a token at sign-in time. A ``user_token`` lives for
    ``jwt_expiry_seconds`` (24h by default), so trusting its ``is_admin`` claim
    on its own would let a demoted, deactivated, or deleted administrator keep
    full access until the token expires.

    Returns the ``User`` only when the row still exists **and** is active;
    otherwise ``None`` (which every caller treats as "not authorised").
    """
    sub = payload.get("sub")
    if not sub:
        return None
    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        return None
    from portal.database import get_session, get_user_by_id

    async with get_session() as db_session:
        user = await get_user_by_id(db_session, user_id)
    if user is None or not user.is_active:
        return None
    return user


async def load_membership_scope(db_session: AsyncSession, user_id: int) -> dict[str, set[int]]:
    """Load the events a user owns or interprets in and the events/rooms they coordinate."""
    from portal.database import (
        list_booth_memberships_for_user,
        list_memberships_for_user,
        list_room_memberships_for_user,
    )

    memberships = await list_memberships_for_user(db_session, user_id)
    rms = await list_room_memberships_for_user(db_session, user_id)
    bms = await list_booth_memberships_for_user(db_session, user_id)

    owned_event_ids = {m.event_id for m in memberships if m.role == EVENT_OWNER}
    # A Room coordinator can be given per event, room or booth (#757): an event-level
    # one coordinates every room of the event, a booth-level one that booth's room.
    whole_event_coordinator_ids = {m.event_id for m in memberships if m.role == ROOM_COORDINATOR}
    coordinated_rooms = {(rm.room_id, rm.room.event_id) for rm in rms if rm.role == ROOM_COORDINATOR and rm.room}
    coordinated_rooms |= {
        (bm.booth.room_id, bm.booth.event_id) for bm in bms if bm.role == ROOM_COORDINATOR and bm.booth
    }
    coordinated_room_ids = {room_id for room_id, _ in coordinated_rooms}
    coordinated_event_ids = {event_id for _, event_id in coordinated_rooms} | whole_event_coordinator_ids
    # Events where the person interprets (given for the whole event or a booth): they work from the booth lobby.
    interpreting_event_ids = {m.event_id for m in memberships if m.role == INTERPRETER}
    interpreting_event_ids |= {bm.booth.event_id for bm in bms if bm.role == INTERPRETER and bm.booth}

    return {
        "owned_event_ids": owned_event_ids,
        "coordinated_event_ids": coordinated_event_ids,
        "coordinated_room_ids": coordinated_room_ids,
        "whole_event_coordinator_ids": whole_event_coordinator_ids,
        "interpreting_event_ids": interpreting_event_ids,
    }


async def resolve_request_principal(request: Request, db_session: AsyncSession) -> dict:
    """Resolve and memoize the principal for the current request.

    Checks the user_token cookie, resolves the live User record, and loads
    the user's owned and coordinated events/rooms into memory once per request.
    """
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached

    user = None
    user_cookie = request.cookies.get("user_token", "")
    if user_cookie:
        try:
            payload = decode_token(user_cookie)
            if payload.get("user"):
                sub = payload.get("sub")
                if sub:
                    try:
                        uid = int(sub)
                        from portal.database import get_user_by_id

                        found = await get_user_by_id(db_session, uid)
                        if found and found.is_active:
                            user = found
                    except (ValueError, TypeError):
                        user = None
        except jwt.InvalidTokenError:
            user = None

    if user is None:
        principal = {
            "user": None,
            "user_id": None,
            "can_host": False,
            "is_admin": False,
            "owned_event_ids": set(),
            "coordinated_event_ids": set(),
            "coordinated_room_ids": set(),
            "whole_event_coordinator_ids": set(),
            "interpreting_event_ids": set(),
            "open_hosting_requests": 0,
            "pending_developer_accounts": 0,
            "has_developer_app": False,
        }
        request.state.principal = principal
        return principal

    from portal.database import (
        count_open_hosting_requests,
        count_pending_developer_accounts,
        has_approved_developer_app,
    )

    scope = await load_membership_scope(db_session, user.id)

    principal = {
        "user": user,
        "user_id": user.id,
        "can_host": bool(user.can_host),
        "hosting_revoked_at": getattr(user, "hosting_revoked_at", None),
        "is_admin": bool(user.is_admin),
        **scope,
        # Shown as the count on the Admin "Hosting access" menu item.
        "open_hosting_requests": await count_open_hosting_requests(db_session) if user.is_admin else 0,
        # Shown as the count on the Admin "Dev Accounts" menu item.
        "pending_developer_accounts": await count_pending_developer_accounts(db_session) if user.is_admin else 0,
        # Shows "Developer API" in the user menu.
        "has_developer_app": await has_approved_developer_app(db_session, user.id),
    }
    request.state.principal = principal
    request.state.user = user
    return principal


def coordinates_room(principal: dict, room_id: int, event_id: int | None) -> bool:
    """True if the person is a Room coordinator of this room (given per room, booth or whole event)."""
    return room_id in principal.get("coordinated_room_ids", set()) or (
        event_id is not None and event_id in principal.get("whole_event_coordinator_ids", set())
    )


async def resolve_principal(
    request: Request, db_session: AsyncSession, error_detail: str = "Sign-in required."
) -> dict:
    """Extract and verify user token to determine the principal.

    Always resolves the database record so that admin-flag revocations
    or deactivations take effect immediately without waiting for the JWT to expire.
    Raises HTTP 403 on failure.
    """
    principal = await resolve_request_principal(request, db_session)
    if principal["user"] is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=error_detail)
    return principal


async def require_room_event_access(
    room_id: int, request: Request, db_session: AsyncSession = Depends(get_db_session)
) -> None:
    """Admits Admins, owners of the room's event and coordinators of the room (e.g. floor transcription).

    Raises HTTP 403 (also for unknown rooms, to prevent room enumeration).
    """
    from portal.database import get_room_by_id

    principal = await resolve_principal(request, db_session)
    if principal["is_admin"]:
        return
    room = await get_room_by_id(db_session, room_id)
    if room is not None and (
        room.event_id in principal["owned_event_ids"] or coordinates_room(principal, room.id, room.event_id)
    ):
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have access to this room.")


async def require_owner_or_coordinator(request: Request) -> None:
    """Admits Admins, owners of the event and coordinators of the room in the path.

    Workspace requests use the event-owner policy. Legacy /admin/ paths keep
    letting room coordinators reach their rooms.
    Returns None on success; raises HTTP 403 on failure.
    """
    if getattr(request.state, "is_workspace", False):
        await require_event_owner(request)
        return

    event_id_str = request.path_params.get("event_id")
    event_id = int(event_id_str) if event_id_str and event_id_str.isdigit() else None
    room_id_str = request.path_params.get("room_id")
    room_id = int(room_id_str) if room_id_str and room_id_str.isdigit() else None

    from portal.database import get_session

    async with get_session() as db_session:
        principal = await resolve_principal(request, db_session)
        if principal["is_admin"]:
            return

        if room_id is not None:
            if coordinates_room(principal, room_id, event_id):
                return
            if event_id is not None:
                if event_id in principal["owned_event_ids"]:
                    return
            else:
                from portal.database import get_room_by_id

                room = await get_room_by_id(db_session, room_id)
                if room is not None and (
                    room.event_id in principal["owned_event_ids"] or coordinates_room(principal, room_id, room.event_id)
                ):
                    return
        elif event_id is not None:
            if event_id in principal["owned_event_ids"]:
                return
            if event_id in principal["coordinated_event_ids"]:
                return
        if event_id is None and room_id is None:
            if request.url.path.startswith("/admin/models/") or request.url.path.startswith("/workspace/models/"):
                if bool(principal["owned_event_ids"]):
                    return
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have access to this event.")
            if principal.get("can_host") or bool(principal["owned_event_ids"]) or bool(principal["coordinated_room_ids"]):
                return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have access to this event.")


async def require_admin(request: Request) -> None:
    """Admits Admins only (the platform-wide role)."""
    from portal.database import get_session

    async with get_session() as db_session:
        principal = await resolve_principal(request, db_session, error_detail="Admin access required.")
        if principal["is_admin"]:
            return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")


async def require_event_owner(request: Request) -> None:
    """FastAPI dependency that guards event owner routes.

    Checks for a valid ``user_token`` whose owner currently has ``is_admin=True``,
    or an event_owner membership for the specified event, in the database.
    Routes without an ``event_id`` are general workspace entry points and accept
    a user who owns at least one event; event-scoped routes require ownership of
    that specific event.
    Returns None on success; raises HTTP 403 on failure.
    """
    event_id_str = request.path_params.get("event_id")
    event_id = int(event_id_str) if event_id_str and event_id_str.isdigit() else None
    room_id_str = request.path_params.get("room_id")
    room_id = int(room_id_str) if room_id_str and room_id_str.isdigit() else None

    from portal.database import get_session

    async with get_session() as db_session:
        principal = await resolve_principal(request, db_session)
        if principal["is_admin"]:
            return

        if event_id is not None:
            if event_id in principal["owned_event_ids"]:
                return
        elif room_id is not None:
            from portal.database import get_room_by_id

            room = await get_room_by_id(db_session, room_id)
            if room is not None and room.event_id in principal["owned_event_ids"]:
                return
        else:
            if request.url.path.startswith("/admin/models/") or request.url.path.startswith("/workspace/models/"):
                if bool(principal["owned_event_ids"]):
                    return
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have access to this event.")
            if principal.get("can_host") or bool(principal["owned_event_ids"]):
                return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You don't have access to this event.")


async def require_hosting_access(request: Request) -> dict:
    """FastAPI dependency for event creation routes.

    Allows Admins and users with hosting access (can_host). Owning an event does
    not by itself allow creating more (#757, #761).
    Non-hosts receive a redirect to /account with an error message (or 403 on API).
    Unauthenticated users receive a redirect to /login?next=... (or 401 on API).
    """
    from portal.database import get_session
    from portal.exceptions import RedirectRequired

    async with get_session() as db_session:
        principal = await resolve_request_principal(request, db_session)
        if principal["user"] is None:
            if "application/json" in request.headers.get("accept", "") or request.url.path.startswith("/api/"):
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
            raise RedirectRequired(f"/login?next={request.url.path}")

        is_hosting_revoked = bool(principal.get("hosting_revoked_at") is not None)
        if principal["is_admin"]:
            return principal

        if is_hosting_revoked:
            if "application/json" in request.headers.get("accept", "") or request.url.path.startswith("/api/"):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hosting access revoked.")
            raise RedirectRequired("/account?error=hosting_revoked")

        if principal["can_host"]:
            return principal

        if "application/json" in request.headers.get("accept", "") or request.url.path.startswith("/api/"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hosting access required.")
        raise RedirectRequired("/account?error=hosting_access_required")


async def get_access_flags(request: Request, event_id: int | None = None, room_id: int | None = None) -> dict[str, bool]:
    """Which roles the signed-in person holds here, for templates (is_admin, is_event_owner, is_room_coordinator).

    Flags are derived from the user's current database state, so a demoted or
    deactivated administrator does not keep admin flags from a stale token.
    """
    flags = {"is_admin": False, "is_event_owner": False, "is_room_coordinator": False}
    from portal.database import get_session

    async with get_session() as db_session:
        principal = await resolve_request_principal(request, db_session)
        if principal["user"] is None:
            return flags

        if principal["is_admin"]:
            flags["is_admin"] = True
            flags["is_event_owner"] = True
            flags["is_room_coordinator"] = True
            return flags

        owned_events = principal["owned_event_ids"]
        coord_events = principal["coordinated_event_ids"]
        coord_rooms = principal["coordinated_room_ids"]

        if event_id is None and room_id is None:
            if principal.get("can_host") or bool(owned_events):
                flags["is_event_owner"] = True
                flags["is_room_coordinator"] = True
            if bool(coord_rooms):
                flags["is_room_coordinator"] = True
        if event_id is not None:
            if event_id in owned_events:
                flags["is_event_owner"] = True
                flags["is_room_coordinator"] = True
        if room_id is not None:
            if coordinates_room(principal, room_id, event_id):
                flags["is_room_coordinator"] = True
            if not flags["is_event_owner"] and event_id is None:
                from portal.database import get_room_by_id

                room = await get_room_by_id(db_session, room_id)
                if room is not None and room.event_id in owned_events:
                    flags["is_event_owner"] = True
                    flags["is_room_coordinator"] = True
        if not flags["is_event_owner"] and event_id is not None:
            if event_id in coord_events:
                flags["is_room_coordinator"] = True
    return flags


def create_user_token(*, user_id: int, email: str, display_name: str = "", is_admin: bool = False) -> str:
    """Create a JWT for a registered user session."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "email": email,
        "display_name": display_name,
        "is_admin": is_admin,
        "user": True,
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


async def get_current_user(request: Request) -> dict | None:
    """Extract current user from user_token cookie. Returns None if not logged in."""
    cookie = request.cookies.get("user_token", "")
    if not cookie:
        return None
    try:
        payload = decode_token(cookie)
    except jwt.InvalidTokenError:
        return None
    if not payload.get("user"):
        return None
    if await _current_user(payload) is None:
        return None
    return payload


async def get_accessible_event_ids(request: Request, *, user_id: int | None) -> tuple[bool, set[int] | None]:
    """Return (is_admin, allowed_event_ids) for the current request.

    Checks the user's active database record to determine Admin status.
    For non-Admins with a user_id, returns the set of event IDs the user
    may access (as event_owner or room_coordinator). Admins get None,
    meaning "all events".

    Args:
        request: The FastAPI Request (used to read cookies).
        user_id: The authenticated user's ID, or None for anonymous callers.

    Returns:
        (is_admin, allowed_event_ids) where allowed_event_ids is None for
        Admins (unrestricted) or a set[int] for regular users.
    """
    from portal.database import (
        get_session,
        get_user_by_id,
    )

    if user_id is None:
        # Anonymous/unauthenticated: no filter is applied (matches prior behaviour).
        return (False, None)

    async with get_session() as session:
        user = await get_user_by_id(session, user_id)
        if user is None or not user.is_active:
            # A missing or deactivated user gets no access at all, regardless of
            # the is_admin claim carried by their (possibly stale) token.
            return (False, set())
        if user.is_admin:
            return (True, None)
        scope = await load_membership_scope(session, user_id)
    return (False, scope["owned_event_ids"] | scope["coordinated_event_ids"])


async def require_user(request: Request) -> dict:
    """FastAPI dependency that requires a logged-in user.

    Returns the JWT payload dict with user_id, email, and is_admin.
    Raises HTTP 403 if not logged in.
    """
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Login required.")
    return user


def get_booth_sessions(request: Request | WebSocket) -> dict[str, dict]:
    """Return decoded JWT payloads for all valid session cookies:
    'user_token' (registered user) and/or 'session_token' (invite link).
    """
    sessions: dict[str, dict] = {}
    for cookie_name in ("user_token", "session_token"):
        cookie = request.cookies.get(cookie_name, "")
        if not cookie:
            continue
        try:
            payload = decode_token(cookie)
            sessions[cookie_name] = payload
        except jwt.InvalidTokenError:
            continue
    return sessions


def booth_session_key(payload: dict | None) -> str | None:
    """Identify a booth session: a user account, or one invite-link visit.

    Every visit to an invite link gets its own ``sub``, so two people sharing a link stay apart.
    """
    if not isinstance(payload, dict) or not payload.get("sub") or is_listener_token(payload):
        return None
    if payload.get("user"):
        return f"user:{payload['sub']}"
    if payload.get("role"):
        return f"invite:{payload['sub']}"
    return None


def get_booth_session(request: Request | WebSocket) -> dict | None:
    """Return decoded JWT payload from either user_token (registered user) or session_token (invite link).

    Registered-user tokens (user_token) are checked FIRST so that a logged-in
    admin or event_admin is never shadowed by an older invite-link session_token
    cookie that may carry a lower role (for example an interpreter invite).

    Returns None if neither cookie exists or both are invalid.
    """
    for cookie_name in ("user_token", "session_token"):
        cookie = request.cookies.get(cookie_name, "")
        if not cookie:
            continue
        try:
            payload = decode_token(cookie)
            return payload
        except jwt.InvalidTokenError:
            continue
    return None


async def resolve_ws_auth(websocket: WebSocket, booth_id: str) -> dict:
    """Resolves authentication for a WebSocket connection"""

    token = websocket.query_params.get("token", "")
    if token:
        try:
            payload = decode_token(token)
        except jwt.InvalidTokenError:
            await websocket.close(code=4001)
            raise WSAuthError("Invalid WebSocket token.")

        # Check Token Scope
        if payload.get("is_admin") or payload.get("admin"):
            return payload

        token_event = payload.get("event_slug", "")
        if is_listener_token(payload):
            if not token_event or not (
                booth_id.startswith(f"{token_event}-") or booth_id.startswith(f"{token_event}/")
            ):
                await websocket.close(code=4003)
                raise WSAuthError("Listener token event_slug does not match booth_id.")
            return payload

        token_lang = payload.get("language_code", "")
        token_room = payload.get("room_id")
        if token_event and token_lang:
            from portal.booth_identity import parse_booth_id

            try:
                actual_event, actual_room, actual_lang = parse_booth_id(booth_id)
            except ValueError:
                await websocket.close(code=4003)
                raise WSAuthError("Invalid booth_id format.")

            if (
                token_event != actual_event
                or token_lang != actual_lang
                or (token_room is not None and str(token_room) != str(actual_room))
            ):
                await websocket.close(code=4003)
                raise WSAuthError("Participant token scope does not match booth_id.")
            return payload

        if payload.get("role") or payload.get("is_admin") or payload.get("admin"):
            return payload

    sessions = get_booth_sessions(websocket)
    user_sess = sessions.get("user_token")
    invite_sess = sessions.get("session_token")
    if user_sess and (user_sess.get("is_admin") or user_sess.get("admin")):
        payload = user_sess
    elif user_sess and invite_sess:
        user_role = await resolve_booth_role(user_sess, booth_id)
        invite_role = await resolve_booth_role(invite_sess, booth_id)
        if ROLE_RANK.get(user_role or "", 0) >= ROLE_RANK.get(invite_role or "", 0):
            payload = user_sess
        else:
            payload = invite_sess
    elif invite_sess:
        payload = invite_sess
    else:
        payload = user_sess or {}

    if not settings.booth_access_token:
        return payload

    # Origin Check for Cookie fallback
    origin = websocket.headers.get("origin")
    if origin:
        from urllib.parse import urlparse

        expected_host = urlparse(settings.public_base_url).netloc
        actual_host = urlparse(origin).netloc or origin
        allowed_hosts = {expected_host, websocket.url.netloc}

        if actual_host not in allowed_hosts:
            await websocket.close(code=4003)
            raise WSAuthError("Origin mismatch.")

    # Cookie Fallback
    if not payload:
        await websocket.close(code=4001)
        raise WSAuthError("Missing WebSocket token and session cookie.")

    # Check Cookie Scope
    if payload.get("is_admin") or payload.get("admin"):
        return payload

    token_event = payload.get("event_slug", "")
    if is_listener_token(payload):
        if not token_event or not (booth_id.startswith(f"{token_event}-") or booth_id.startswith(f"{token_event}/")):
            await websocket.close(code=4003)
            raise WSAuthError("Listener cookie event_slug does not match booth_id.")
        return payload

    token_lang = payload.get("language_code", "")
    token_room = payload.get("room_id")
    if token_event and token_lang:
        from portal.booth_identity import parse_booth_id

        try:
            actual_event, actual_room, actual_lang = parse_booth_id(booth_id)
        except ValueError:
            await websocket.close(code=4003)
            raise WSAuthError("Invalid booth_id format.")

        if (
            token_event != actual_event
            or token_lang != actual_lang
            or (token_room is not None and str(token_room) != str(actual_room))
        ):
            await websocket.close(code=4003)
            raise WSAuthError("Participant cookie scope does not match booth_id.")

    return payload


async def resolve_booth_role(
    sessions: list[dict] | dict | None,
    booth_id: str | None = None,
) -> str | None:
    """Extract the highest applicable role claim from session payloads.

    Accepts a list of session payloads (or a single session payload dict) and
    iterates over them to evaluate:
    - User account memberships in the database (BoothMembership, RoomMembership, EventMembership).
    - The Admin role, read from the database so a demoted Admin loses it at once.
    - Scoped invitation role claims.

    The highest privilege among all valid grants wins.
    """
    if not sessions:
        return None

    session_list: list[dict] = (
        [sessions] if isinstance(sessions, dict) else [s for s in sessions if isinstance(s, dict)]
    )
    if not session_list:
        return None

    roles: list[str] = []

    for sess in session_list:
        # 1. Admin: trust the account's current state, not the claim in the token.
        if sess.get("sub") and sess.get("user"):
            if await user_is_active_admin(sess["sub"]):
                roles.append(ADMIN)
        elif sess.get("is_admin") or sess.get("admin"):
            roles.append(ADMIN)

        # 2. Direct role claim (e.g. invite token or mock payload)
        if "role" in sess:
            role = sess["role"]
            scope_matches = True
            token_event = sess.get("event_slug")
            token_lang = sess.get("language_code")
            token_room = sess.get("room_id")
            if booth_id and (token_event or token_lang):
                from portal.booth_identity import parse_booth_id

                try:
                    actual_event, actual_room, actual_lang = parse_booth_id(booth_id)
                    if token_event and token_event != actual_event:
                        scope_matches = False
                    if token_lang and token_lang != actual_lang:
                        scope_matches = False
                    if token_room is not None and str(token_room) != str(actual_room):
                        scope_matches = False
                except ValueError:
                    scope_matches = False
            if scope_matches:
                roles.append(role)

        # 3. Database memberships for registered user session
        if sess.get("sub") and booth_id:
            from sqlalchemy import select

            from portal.booth_identity import parse_booth_id
            from portal.database import (
                get_session,
                list_booth_memberships_for_user,
                list_memberships_for_user,
                list_room_memberships_for_user,
            )
            from portal.models import DBBooth, Event

            try:
                event_slug, room_id, lang_code = parse_booth_id(booth_id)
                uid_int = int(sess["sub"])
                async with get_session() as db_session:
                    stmt = (
                        select(DBBooth)
                        .join(Event)
                        .where(
                            Event.slug == event_slug,
                            Event.deleted_at.is_(None),
                            DBBooth.room_id == room_id,
                            DBBooth.language_code == lang_code,
                        )
                    )
                    booth = (await db_session.scalars(stmt)).first()
                    if booth:
                        bms = await list_booth_memberships_for_user(db_session, uid_int)
                        for bm in bms:
                            if bm.booth_id == booth.id:
                                roles.append(bm.role)
                                break
                        rms = await list_room_memberships_for_user(db_session, uid_int)
                        for rm in rms:
                            if rm.room_id == booth.room_id:
                                roles.append(rm.role)
                                break
                        memberships = await list_memberships_for_user(db_session, uid_int)
                        for m in memberships:
                            if m.event_id == booth.event_id:
                                roles.append(m.role)
                                break
            except ValueError:
                pass
            except Exception:
                logger.exception("Failed to resolve booth role from database")

    return highest_role(roles)


async def user_is_active_admin(user_id: object) -> bool:
    from portal.database import get_session, get_user_by_id

    try:
        uid = int(user_id)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return False
    async with get_session() as db_session:
        user = await get_user_by_id(db_session, uid)
    return bool(user and user.is_admin and user.is_active)


def can_perform_role(granted_role: str | None, requested_role: str) -> bool:
    """Return True if *granted_role* can do everything *requested_role* can.

    A coordinator can join as interpreter, but an interpreter cannot join as coordinator.
    """
    return is_at_least(granted_role, requested_role)


oauth2_bearer = HTTPBearer(auto_error=False)


def require_oauth_scope(required_scope: str):
    async def dependency(
        request: Request,
        db: AsyncSession = Depends(get_db_session),
        token: HTTPAuthorizationCredentials | None = Depends(oauth2_bearer),
    ):
        if not token:
            raise HTTPException(status_code=401, detail="Missing Bearer Token")

        from datetime import datetime, timezone

        from sqlalchemy import select
        from sqlalchemy.orm import joinedload

        from portal.models import OAuthToken

        token_hash = hashlib.sha256(token.credentials.encode()).hexdigest()
        result = await db.execute(
            select(OAuthToken).options(joinedload(OAuthToken.client)).where(OAuthToken.access_token_hash == token_hash)
        )
        oauth_token = result.scalars().first()

        if not oauth_token:
            raise HTTPException(status_code=401, detail="Invalid token")

        if oauth_token.revoked:
            raise HTTPException(status_code=401, detail="Token revoked")

        if not oauth_token.client or oauth_token.client.status != "active":
            raise HTTPException(status_code=401, detail="Client is not active")

        # SQLAlchemy with SQLite might return naive datetimes for expires_at
        expires_at_aware = (
            oauth_token.expires_at.replace(tzinfo=timezone.utc)
            if oauth_token.expires_at.tzinfo is None
            else oauth_token.expires_at
        )
        if expires_at_aware < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Token expired")

        if required_scope not in oauth_token.scopes:
            raise HTTPException(status_code=403, detail=f"Token missing required scope: {required_scope}")

        # Optional: Update last_used_at
        # oauth_token.last_used_at = datetime.now(timezone.utc)
        # await db.commit()

        return oauth_token

    return dependency
