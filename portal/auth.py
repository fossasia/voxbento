from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, WebSocket, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from portal.config import settings

security = HTTPBearer(auto_error=False)


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
