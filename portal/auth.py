from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import HTTPException, Request, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from portal.config import settings

security = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def create_token() -> str:
    now = datetime.now(timezone.utc)
    payload = {
        'iat': now,
        'exp': now + timedelta(seconds=settings.jwt_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm='HS256')


def create_participant_token(
    *,
    booth_id: int,
    role: str,
    event_slug: str,
    language_code: str,
) -> str:
    """Create a JWT with role claims for a participant who joined via invite link."""
    now = datetime.now(timezone.utc)
    payload = {
        'sub': str(uuid.uuid4()),
        'booth_id': booth_id,
        'role': role,
        'event_slug': event_slug,
        'language_code': language_code,
        'iat': now,
        'exp': now + timedelta(seconds=settings.jwt_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm='HS256')


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.effective_jwt_secret, algorithms=['HS256'])


def verify_bearer(credentials: HTTPAuthorizationCredentials | None) -> None:
    """Raise HTTP 401 if auth is required and credentials are missing or invalid."""
    if not settings.booth_access_token:
        return
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Missing auth token.')
    try:
        decode_token(credentials.credentials)
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=f'Invalid token: {exc}')


async def verify_ws_token(websocket: WebSocket) -> None:
    """Validate JWT from ?token= query param before accepting the connection.

    Closes the connection with code 4001 if the token is invalid.
    Raises ValueError so the caller can return early without calling accept().
    """
    if not settings.booth_access_token:
        return
    token = websocket.query_params.get('token', '')
    if not token:
        await websocket.close(code=4001)
        raise ValueError('Missing WebSocket token.')
    try:
        decode_token(token)
    except jwt.InvalidTokenError as exc:
        await websocket.close(code=4001)
        raise ValueError(f'Invalid WebSocket token: {exc}')


async def require_admin(request: Request) -> None:
    """FastAPI dependency that guards admin routes.

    Checks for a valid ``admin_token`` cookie containing a JWT with
    ``admin=True`` claim. Also accepts a valid ``user_token`` with
    ``is_admin=True``. Returns None on success; raises HTTP 403 on failure.
    """
    user_cookie = request.cookies.get('user_token', '')
    if user_cookie:
        try:
            payload = decode_token(user_cookie)
            if payload.get('user') and payload.get('is_admin'):
                return
        except jwt.InvalidTokenError:
            pass

    cookie = request.cookies.get('admin_token', '')
    if not cookie:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Admin access required.')
    try:
        payload = decode_token(cookie)
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Invalid admin token.')
    if not payload.get('admin'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Admin access required.')


def create_admin_token() -> str:
    """Create a JWT with admin=True claim for admin panel access."""
    now = datetime.now(timezone.utc)
    payload = {
        'admin': True,
        'iat': now,
        'exp': now + timedelta(seconds=settings.jwt_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm='HS256')


# ---------------------------------------------------------------------------
# User auth
# ---------------------------------------------------------------------------


def create_user_token(*, user_id: int, email: str, is_admin: bool = False) -> str:
    """Create a JWT for a registered user session."""
    now = datetime.now(timezone.utc)
    payload = {
        'sub': str(user_id),
        'email': email,
        'is_admin': is_admin,
        'user': True,
        'iat': now,
        'exp': now + timedelta(seconds=settings.jwt_expiry_seconds),
    }
    return jwt.encode(payload, settings.effective_jwt_secret, algorithm='HS256')


async def get_current_user(request: Request) -> dict | None:
    """Extract current user from user_token cookie. Returns None if not logged in."""
    cookie = request.cookies.get('user_token', '')
    if not cookie:
        return None
    try:
        payload = decode_token(cookie)
    except jwt.InvalidTokenError:
        return None
    if not payload.get('user'):
        return None
    return payload


async def require_user(request: Request) -> dict:
    """FastAPI dependency that requires a logged-in user.

    Returns the JWT payload dict with user_id, email, and is_admin.
    Raises HTTP 403 if not logged in.
    """
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Login required.')
    return user


# Role hierarchy — higher index = more privilege.
_ROLE_RANK: dict[str, int] = {
    'listener': 0,
    'interpreter': 1,
    'coordinator': 2,
    'event_admin': 3,
    'super_admin': 4,
}


def get_booth_session(request: Request) -> dict | None:
    """Return decoded JWT payload from either user_token (registered user) or session_token (invite link).

    Registered-user tokens (user_token) are checked FIRST so that a logged-in
    admin or event_admin is never shadowed by an older invite-link session_token
    cookie that may carry a lower role (e.g. 'interpreter').

    Returns None if neither cookie exists or both are invalid.
    """
    # Prefer user_token (registered user with is_admin / event roles) over
    # session_token (one-time invite link with a fixed role claim).
    for cookie_name in ('user_token', 'session_token'):
        cookie = request.cookies.get(cookie_name, '')
        if not cookie:
            continue
        try:
            payload = decode_token(cookie)
            return payload
        except jwt.InvalidTokenError:
            continue
    return None


def resolve_booth_role(payload: dict | None) -> str | None:
    """Extract the role claim from a booth session payload.

    - Invite tokens carry a ``role`` claim directly.
    - User tokens carry ``is_admin``; without a booth-specific role the
      caller must check EventMembership separately.  When is_admin=True we
      treat them as event_admin.
    Returns None if no role can be determined.
    """
    if payload is None:
        return None
    # Invite / participant token
    if 'role' in payload:
        return payload['role']
    # Registered user — is_admin maps to event_admin for booth purposes
    if payload.get('is_admin'):
        return 'event_admin'
    return None


def can_perform_role(granted_role: str | None, requested_role: str) -> bool:
    """Return True if *granted_role* is at least as privileged as *requested_role*.

    A coordinator can join as interpreter (lower rank), but a listener cannot
    join as interpreter (higher rank).
    """
    if granted_role is None:
        return False
    return _ROLE_RANK.get(granted_role, -1) >= _ROLE_RANK.get(requested_role, 0)
