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
from portal.access_policy import AccessPolicy, Principal
from portal.roles import _ROLE_RANK
logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_token() -> str:
    now = datetime.now(timezone.utc)
    payload = {"iat": now, "exp": now + timedelta(seconds=settings.jwt_expiry_seconds)}
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


def create_listener_token(*, event_slug: str) -> str:
    """Create a short-lived JWT for listener access via API."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "listener",
        "event_slug": event_slug,
        "iat": now,
        "exp": now + timedelta(seconds=settings.listener_token_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm="HS256")


def create_embed_token(*, event_slug: str) -> str:
    """Create a short-lived JWT for embed iframe access.

    Identical claim shape to create_listener_token (role='listener', event_slug)
    so it passes both the embed route's claim checks and the /ws/captions
    WebSocket auth's booth_id.startswith(event_slug + '-') check.
    Uses a shorter expiry (embed_token_expiry_seconds, default 30 min) because
    the token is visible in the iframe src= attribute in the third party's HTML.
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "listener",
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



async def resolve_principal(
    request: Request,
    db_session: AsyncSession,
    error_detail: str = "Admin access required.",
) -> Principal:
    """Resolve an active user and build their policy from current DB state."""
    user_cookie = request.cookies.get("user_token", "")
    if not user_cookie:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail,
        )

    try:
        payload = decode_token(user_cookie)
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail,
        )

    if not payload.get("user"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail,
        )

    try:
        user_id = int(payload.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail,
        )

    from portal.database import (
        get_user_by_id,
        list_booth_memberships_for_user,
        list_memberships_for_user,
        list_room_memberships_for_user,
    )

    user = await get_user_by_id(db_session, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_detail,
        )

    event_memberships = await list_memberships_for_user(db_session, user.id)
    room_memberships = await list_room_memberships_for_user(db_session, user.id)
    booth_memberships = await list_booth_memberships_for_user(db_session, user.id)

    policy = AccessPolicy.from_memberships(
        user,
        event_memberships,
        room_memberships,
        booth_memberships,
    )
    return Principal(user_id=user.id, policy=policy)



async def require_room_event_access(
    room_id: int,
    request: Request,
    db_session: AsyncSession = Depends(get_db_session),
) -> None:
    """Allow global admins, parent-event owners, or coordinators of this room."""
    principal = await resolve_principal(request, db_session)

    if principal.is_global_admin:
        return

    from portal.database import get_room_by_id

    room = await get_room_by_id(db_session, room_id)
    if room is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )

    if (
        principal.policy.has_role("event_owner", event_id=room.event_id)
        or principal.policy.room_roles.get(room_id) == "room_coordinator"
    ):
        return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required.",
    )




async def require_admin(request: Request) -> None:
    """Guard management routes using the existing scoped access rules."""
    if getattr(request.state, "is_workspace", False):
        await require_event_owner(request)
        return

    event_id_str = request.path_params.get("event_id")
    event_id = int(event_id_str) if event_id_str and event_id_str.isdigit() else None
    room_id_str = request.path_params.get("room_id")
    room_id = int(room_id_str) if room_id_str and room_id_str.isdigit() else None

    from portal.database import get_session, get_room_by_id

    async with get_session() as db_session:
        principal = await resolve_principal(request, db_session)

        if principal.is_global_admin:
            return

        policy = principal.policy

        if room_id is not None:
            if policy.room_roles.get(room_id) == "room_coordinator":
                return

            if event_id is not None:
                if policy.has_role("event_owner", event_id=event_id):
                    return
            else:
                room = await get_room_by_id(db_session, room_id)
                if room is not None and policy.has_role(
                    "event_owner", event_id=room.event_id
                ):
                    return

        elif event_id is not None:
            if policy.has_role("event_owner", event_id=event_id):
                return
            if policy.has_room_coordinator_in_event(event_id):
                return

        else:
            if policy.has_any_role("event_owner") or policy.has_any_role(
                "room_coordinator"
            ):
                return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required.",
    )



async def require_super_admin(request: Request) -> None:
    """Allow only a current global administrator."""
    from portal.database import get_session

    async with get_session() as db_session:
        principal = await resolve_principal(
            request,
            db_session,
            error_detail="Super-admin access required.",
        )
        if principal.is_global_admin:
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Super-admin access required.",
    )


async def require_event_owner(request: Request) -> None:
    """Require global-admin or event-owner access to the requested scope."""
    event_id_str = request.path_params.get("event_id")
    event_id = int(event_id_str) if event_id_str and event_id_str.isdigit() else None
    room_id_str = request.path_params.get("room_id")
    room_id = int(room_id_str) if room_id_str and room_id_str.isdigit() else None

    from portal.database import get_session, get_room_by_id

    async with get_session() as db_session:
        principal = await resolve_principal(request, db_session)

        if principal.is_global_admin:
            return

        policy = principal.policy

        if event_id is not None:
            if policy.has_role("event_owner", event_id=event_id):
                return
        elif room_id is not None:
            room = await get_room_by_id(db_session, room_id)
            if room is not None and policy.has_role(
                "event_owner", event_id=room.event_id
            ):
                return
        elif policy.has_any_role("event_owner"):
            return

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin access required.",
    )


async def get_admin_flags(
    request: Request,
    event_id: int | None = None,
    room_id: int | None = None,
) -> dict[str, bool]:
    """Return admin template flags from current database-backed permissions."""
    flags = {
        "is_super_admin": False,
        "is_event_owner": False,
        "is_room_coordinator": False,
    }

    from portal.database import get_room_by_id, get_session

    async with get_session() as db_session:
        try:
            principal = await resolve_principal(request, db_session)
        except HTTPException:
            return flags

        if principal.is_global_admin:
            return {
                "is_super_admin": True,
                "is_event_owner": True,
                "is_room_coordinator": True,
            }

        policy = principal.policy

        if event_id is None and room_id is None:
            flags["is_event_owner"] = policy.has_any_role("event_owner")
            flags["is_room_coordinator"] = (
                flags["is_event_owner"]
                or policy.has_any_role("room_coordinator")
            )

        if event_id is not None:
            flags["is_event_owner"] = policy.has_role(
                "event_owner", event_id=event_id
            )
            flags["is_room_coordinator"] = (
                flags["is_event_owner"]
                or policy.has_room_coordinator_in_event(event_id)
            )

        if room_id is not None:
            flags["is_room_coordinator"] = (
                flags["is_room_coordinator"]
                or policy.room_roles.get(room_id) == "room_coordinator"
            )

            if not flags["is_event_owner"] and event_id is None:
                room = await get_room_by_id(db_session, room_id)
                if room is not None:
                    flags["is_event_owner"] = policy.has_role(
                        "event_owner", event_id=room.event_id
                    )
                    if flags["is_event_owner"]:
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
    return payload


async def get_accessible_event_ids(request: Request, *, user_id: int | None) -> tuple[bool, set[int] | None]:
    """Return (is_super_admin, allowed_event_ids) for the current request.

    Checks the user's active database record to determine super-admin status.
    For non-super-admins with a user_id, returns the set of event IDs the user
    may access (as event_owner or room_coordinator). Super-admins get None,
    meaning "all events".

    Args:
        request: The FastAPI Request (used to read cookies).
        user_id: The authenticated user's ID, or None for anonymous callers.

    Returns:
        (is_super_admin, allowed_event_ids) where allowed_event_ids is None for
        super-admins (unrestricted) or a set[int] for regular users.
    """
    from portal.database import (
        get_session,
        get_user_by_id,
        list_memberships_for_user,
        list_room_memberships_for_user,
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
        memberships = await list_memberships_for_user(session, user_id)
        room_memberships = await list_room_memberships_for_user(session, user_id)
    allowed_event_ids: set[int] = {m.event_id for m in memberships if m.role == "event_owner"}
    allowed_event_ids.update({rm.room.event_id for rm in room_memberships if rm.role == "room_coordinator"})
    return (False, allowed_event_ids)


async def require_user(request: Request) -> dict:
    """FastAPI dependency that requires a logged-in user.

    Returns the JWT payload dict with user_id, email, and is_admin.
    Raises HTTP 403 if not logged in.
    """
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Login required.")
    return user


def get_booth_session(request: Request | WebSocket) -> dict | None:
    """Return decoded JWT payload from either user_token (registered user) or session_token (invite link).

    Registered-user tokens (user_token) are checked FIRST so that a logged-in
    admin or event_admin is never shadowed by an older invite-link session_token
    cookie that may carry a lower role (e.g. 'interpreter').

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
        if payload.get("role") == "listener":
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

    if not settings.booth_access_token:
        return get_booth_session(websocket) or {}

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
    payload = get_booth_session(websocket)
    if not payload:
        await websocket.close(code=4001)
        raise WSAuthError("Missing WebSocket token and session cookie.")

    # Check Cookie Scope
    if payload.get("is_admin") or payload.get("admin"):
        return payload

    token_event = payload.get("event_slug", "")
    if payload.get("role") == "listener":
        if not token_event or not booth_id.startswith(f"{token_event}-"):
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
    payload: dict | None,
    booth_id: str | None = None,
) -> str | None:
    """Resolve the highest-ranked role from token claims and database memberships."""
    if payload is None:
        return None

    roles = []
    if "role" in payload:
        roles.append(payload["role"])

    if payload.get("sub") and booth_id:
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
                    user_id = int(payload["sub"])
                    booth_memberships = await list_booth_memberships_for_user(
                        db_session, user_id
                    )
                    room_memberships = await list_room_memberships_for_user(
                        db_session, user_id
                    )
                    event_memberships = await list_memberships_for_user(
                        db_session, user_id
                    )

                    # Preserve roles inherited from the booth's room and event,
                    # even when the user has no direct booth membership.
                    policy = AccessPolicy()
                    policy.booth_scopes[booth.id] = (
                        booth.room_id,
                        booth.event_id,
                    )

                    for membership in booth_memberships:
                        policy.booth_roles[membership.booth_id] = membership.role

                    for membership in room_memberships:
                        policy.room_roles[membership.room_id] = membership.role

                    for membership in event_memberships:
                        policy.event_roles[membership.event_id] = membership.role

                    roles_for_booth = policy._roles_for_scope(booth_id=booth.id)
                    roles.extend(roles_for_booth)

        except ValueError:
            pass
        except Exception:
            logger.exception("Failed to resolve booth role from database")

    if payload.get("is_admin") or payload.get("admin"):
        roles.append("super_admin")

    if not roles:
        return None

    return max(roles, key=lambda role: _ROLE_RANK.get(role, 0))



def can_perform_role(granted_role: str | None, requested_role: str) -> bool:
    """Return True if *granted_role* is at least as privileged as *requested_role*.

    A coordinator can join as interpreter (lower rank), but a listener cannot
    join as interpreter (higher rank).
    """
    if granted_role is None:
        return False
    return _ROLE_RANK.get(granted_role, -1) >= _ROLE_RANK.get(requested_role, 0)


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
