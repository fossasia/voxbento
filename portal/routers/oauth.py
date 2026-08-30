from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from portal.auth import get_current_user, require_user
from portal.database import get_db_session
from portal.models import (
    Event,
    EventMembership,
    OAuthAuditLog,
    OAuthAuthorizationCode,
    OAuthClient,
    OAuthConsentGrant,
    OAuthToken,
)
from portal.rate_limit import auth_rate_limiter, token_rate_limiter

logger = logging.getLogger(__name__)
router = APIRouter(tags=["oauth"])

_BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

VALID_SCOPES = {
    "events:read": "Read event information",
    "events:write": "Manage events",
    "rooms:read": "Read room information",
    "rooms:write": "Manage rooms",
    "booths:read": "Read booth information",
    "booths:write": "Manage booths",
    "sessions:manage": "Start and stop transcription sessions",
    "sessions:read": "Read transcription session status",
    "transcripts:read": "Read and export transcripts",
    "listeners:provision": "Create listener access tokens",
    "webhooks:manage": "Manage webhook subscriptions",
}


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    if method == "S256":
        hashed = hashlib.sha256(code_verifier.encode()).digest()
        encoded = base64.urlsafe_b64encode(hashed).decode().rstrip("=")
        return encoded == code_challenge
    return False


async def get_effective_scopes(db: AsyncSession, user: dict, event_id: int, requested_scopes: list[str]) -> list[str]:
    # Check if user has EventMembership
    result = await db.execute(
        select(EventMembership).where(EventMembership.user_id == int(user["sub"]), EventMembership.event_id == event_id)
    )
    event_membership = result.scalars().first()

    # Check if user has RoomMembership in this event (for room_coordinators)
    # simplified for now: if they have any role in the event, we grant scopes they requested
    # that map to their role.
    # Accept "owner" as a synonym for "event_owner" (e.g. roles synced from Eventyay)
    is_event_admin = event_membership and event_membership.role in ("event_owner", "super_admin", "owner")
    is_room_coordinator = event_membership and event_membership.role == "room_coordinator"

    # In a full implementation, we would narrow this down per-room.
    # For MVP, if they are event_owner, they get all they asked for.
    # If they are room_coordinator, they get room/booth level scopes.
    allowed = set()
    if is_event_admin:
        allowed = set(VALID_SCOPES.keys())
    elif is_room_coordinator:
        allowed = {
            "rooms:read",
            "rooms:write",
            "booths:read",
            "booths:write",
            "sessions:manage",
            "sessions:read",
            "transcripts:read",
            "listeners:provision",
        }

    # Intersection
    return list(set(requested_scopes) & allowed)


@router.get("/oauth/authorize", include_in_schema=False)
async def authorize_get(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str,
    scope: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    event: str = "",
    user: dict | None = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
):
    if user is None:
        # User is not logged in, redirect to login with 'next' parameter
        path_query = f"{request.url.path}?{request.url.query}" if request.url.query else request.url.path
        next_url = urllib.parse.quote(path_query)
        return RedirectResponse(url=f"/login?next={next_url}", status_code=303)

    if response_type != "code":
        raise HTTPException(status_code=400, detail="Unsupported response_type. Only 'code' is supported.")
    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="PKCE required. code_challenge_method must be 'S256'.")
    if not event:
        raise HTTPException(status_code=400, detail="Missing 'event' parameter.")

    client_ip = request.client.host if request.client else "unknown"
    if await auth_rate_limiter.is_rate_limited(f"auth_{client_ip}"):
        raise HTTPException(status_code=429, detail="Too many authorization requests.")

    # 1. Validate Client
    result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    client = result.scalars().first()
    if not client or client.status != "active":
        raise HTTPException(status_code=400, detail="Invalid or inactive client_id.")

    if redirect_uri not in client.redirect_uris:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri.")

    # 2. Validate Event
    evt_result = await db.execute(select(Event).where(Event.slug == event))
    evt = evt_result.scalars().first()
    if not evt:
        # Auto-provision a stub event for OAuth flow
        evt = Event(slug=event, display_name=event)
        db.add(evt)
        await db.flush()

        # Give the authorizing user ownership
        membership = EventMembership(user_id=int(user["sub"]), event_id=evt.id, role="event_owner")
        db.add(membership)
        await db.flush()

    # 3. Calculate Scopes
    requested_scopes = scope.split(" ") if scope else []
    effective_scopes = await get_effective_scopes(db, user, evt.id, requested_scopes)

    if not effective_scopes:
        raise HTTPException(
            status_code=403, detail="You do not have permission to grant the requested scopes for this event."
        )

    # 4. For now, require explicit consent (can auto-approve if consent exists and covers scopes)

    # For now, require explicit consent (can auto-approve if consent exists and covers scopes)

    return templates.TemplateResponse(
        request=request,
        name="oauth/consent.html",
        context={
            "client": client,
            "event": evt,
            "scopes": [(s, VALID_SCOPES.get(s, s)) for s in effective_scopes],
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "scope_string": " ".join(effective_scopes),
        },
    )


@router.post("/oauth/authorize", include_in_schema=False)
async def authorize_post(
    client_id: Annotated[str, Form()],
    redirect_uri: Annotated[str, Form()],
    state: Annotated[str, Form()],
    code_challenge: Annotated[str, Form()],
    code_challenge_method: Annotated[str, Form()],
    event_id: Annotated[int, Form()],
    scope: Annotated[str, Form()],
    action: Annotated[str, Form()],
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    # Re-validate client and redirect URI before any redirect response
    result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    client = result.scalars().first()
    if not client or client.status != "active" or redirect_uri not in client.redirect_uris:
        raise HTTPException(status_code=400, detail="Invalid client or redirect URI.")

    # Use the canonical registered redirect URI value for all redirects.
    validated_redirect_uri = next((uri for uri in client.redirect_uris if uri == redirect_uri), None)
    if not validated_redirect_uri:
        raise HTTPException(status_code=400, detail="Invalid client or redirect URI.")

    if action == "deny":
        parsed_redirect = urllib.parse.urlparse(validated_redirect_uri)
        existing_query = urllib.parse.parse_qsl(parsed_redirect.query, keep_blank_values=True)
        existing_query.extend([("error", "access_denied"), ("state", state)])
        error_query = urllib.parse.urlencode(existing_query)
        error_url = urllib.parse.urlunparse(parsed_redirect._replace(query=error_query))
        return RedirectResponse(url=error_url, status_code=303)

    # Re-validate scopes live
    effective_scopes = await get_effective_scopes(db, user, event_id, scope.split(" "))
    if not effective_scopes:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Save consent — handle race conditions with a savepoint
    from sqlalchemy.exc import IntegrityError

    try:
        async with db.begin_nested():
            consent = OAuthConsentGrant(
                client_id=client.id, user_id=int(user["sub"]), event_id=event_id, scopes=effective_scopes
            )
            db.add(consent)
            await db.flush()
    except IntegrityError:
        # Another request inserted it concurrently. Just update the scopes.
        await db.execute(
            update(OAuthConsentGrant)
            .where(
                OAuthConsentGrant.client_id == client.id,
                OAuthConsentGrant.user_id == int(user["sub"]),
                OAuthConsentGrant.event_id == event_id,
            )
            .values(scopes=effective_scopes)
        )

    # Generate authorization code
    code = generate_token()
    auth_code = OAuthAuthorizationCode(
        client_id=client.id,
        user_id=int(user["sub"]),
        event_id=event_id,
        scopes=effective_scopes,
        code_hash=hash_token(code),
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        redirect_uri=redirect_uri,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db.add(auth_code)
    db.add(
        OAuthAuditLog(
            client_id=client.id, event_id=event_id, action="authorize", request_path="/oauth/authorize", status_code=303
        )
    )
    await db.flush()

    parsed_redirect = urllib.parse.urlparse(validated_redirect_uri)
    existing_query = urllib.parse.parse_qsl(parsed_redirect.query, keep_blank_values=True)
    existing_query.extend([("code", code), ("state", state)])
    redirect_query = urllib.parse.urlencode(existing_query)
    redirect_url = urllib.parse.urlunparse(parsed_redirect._replace(query=redirect_query))
    return RedirectResponse(url=redirect_url, status_code=303)


@router.post("/oauth/token", response_class=JSONResponse)
async def token_exchange(
    request: Request,
    grant_type: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    client_secret: Annotated[str | None, Form()] = None,
    code: Annotated[str | None, Form()] = None,
    redirect_uri: Annotated[str | None, Form()] = None,
    code_verifier: Annotated[str | None, Form()] = None,
    refresh_token: Annotated[str | None, Form()] = None,
    db: AsyncSession = Depends(get_db_session),
):
    # Basic client validation
    result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    client = result.scalars().first()
    if not client or client.status != "active":
        return JSONResponse(status_code=400, content={"error": "invalid_client"})

    if await token_rate_limiter.is_rate_limited(f"token_{client_id}"):
        return JSONResponse(status_code=429, content={"error": "slow_down", "error_description": "Rate limit exceeded"})

    if client.is_confidential:
        if not client_secret or client.client_secret_hash != hash_token(client_secret):
            return JSONResponse(status_code=401, content={"error": "invalid_client"})

    if grant_type == "authorization_code":
        if not code or not redirect_uri or not code_verifier:
            return JSONResponse(status_code=400, content={"error": "invalid_request"})

        code_hash = hash_token(code)
        code_result = await db.execute(
            select(OAuthAuthorizationCode).where(OAuthAuthorizationCode.code_hash == code_hash)
        )
        auth_code = code_result.scalars().first()

        if (
            not auth_code
            or auth_code.used
            or auth_code.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)
        ):
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})

        if auth_code.client_id != client.id or auth_code.redirect_uri != redirect_uri:
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})

        if not verify_pkce(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method):
            return JSONResponse(
                status_code=400, content={"error": "invalid_grant", "error_description": "PKCE verification failed"}
            )

        # Mark code as used
        auth_code.used = True

        # Issue tokens
        access_token_raw = generate_token()
        refresh_token_raw = generate_token()

        token_record = OAuthToken(
            client_id=client.id,
            user_id=auth_code.user_id,
            event_id=auth_code.event_id,
            scopes=auth_code.scopes,
            access_token_hash=hash_token(access_token_raw),
            refresh_token_hash=hash_token(refresh_token_raw),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(token_record)
        db.add(
            OAuthAuditLog(
                token_id=token_record.id,
                client_id=client.id,
                event_id=auth_code.event_id,
                action="token_exchange_code",
                request_path="/oauth/token",
                status_code=200,
            )
        )
        await db.flush()

        return {
            "access_token": access_token_raw,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": refresh_token_raw,
            "scope": " ".join(auth_code.scopes),
        }

    elif grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse(status_code=400, content={"error": "invalid_request"})

        refresh_hash = hash_token(refresh_token)
        token_result = await db.execute(
            select(OAuthToken)
            .options(selectinload(OAuthToken.client), selectinload(OAuthToken.event))
            .where(OAuthToken.refresh_token_hash == refresh_hash)
        )
        token_record = token_result.scalars().first()

        if not token_record or token_record.client_id != client.id:
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})

        if token_record.revoked:
            # Refresh token reuse detected! Revoke the entire family
            await db.execute(
                update(OAuthToken)
                .where(OAuthToken.client_id == client.id)
                .where(OAuthToken.user_id == token_record.user_id)
                .where(OAuthToken.event_id == token_record.event_id)
                .values(revoked=True)
            )
            db.add(
                OAuthAuditLog(
                    client_id=client.id,
                    event_id=token_record.event_id,
                    action="token_reuse_detected",
                    request_path="/oauth/token",
                    status_code=400,
                )
            )
            await db.flush()
            return JSONResponse(
                status_code=400, content={"error": "invalid_grant", "error_description": "Token reuse detected"}
            )

        # Revoke the old token
        token_record.revoked = True

        # Issue new token pair
        new_access_token_raw = generate_token()
        new_refresh_token_raw = generate_token()

        new_token_record = OAuthToken(
            client_id=client.id,
            user_id=token_record.user_id,
            event_id=token_record.event_id,
            scopes=token_record.scopes,  # Can be narrowed down, but keep same for now
            access_token_hash=hash_token(new_access_token_raw),
            refresh_token_hash=hash_token(new_refresh_token_raw),
            parent_token_id=token_record.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(new_token_record)
        db.add(
            OAuthAuditLog(
                token_id=new_token_record.id,
                client_id=client.id,
                event_id=token_record.event_id,
                action="token_exchange_refresh",
                request_path="/oauth/token",
                status_code=200,
            )
        )
        await db.flush()

        return {
            "access_token": new_access_token_raw,
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": new_refresh_token_raw,
            "scope": " ".join(token_record.scopes),
        }

    return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})


@router.post("/oauth/revoke")
async def revoke_token(
    token: Annotated[str, Form()],
    client_id: Annotated[str, Form()],
    client_secret: Annotated[str | None, Form()] = None,
    db: AsyncSession = Depends(get_db_session),
):
    result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    client = result.scalars().first()
    if not client:
        return JSONResponse(status_code=400, content={"error": "invalid_client"})

    if client.is_confidential:
        if not client_secret or client.client_secret_hash != hash_token(client_secret):
            return JSONResponse(status_code=401, content={"error": "invalid_client"})

    t_hash = hash_token(token)

    # Try access token first
    token_result = await db.execute(select(OAuthToken).where(OAuthToken.access_token_hash == t_hash))
    token_record = token_result.scalars().first()

    if not token_record:
        # Try refresh token
        token_result = await db.execute(select(OAuthToken).where(OAuthToken.refresh_token_hash == t_hash))
        token_record = token_result.scalars().first()

    if token_record and token_record.client_id == client.id:
        token_record.revoked = True
        db.add(
            OAuthAuditLog(
                token_id=token_record.id,
                client_id=client.id,
                event_id=token_record.event_id,
                action="token_revoke",
                request_path="/oauth/revoke",
                status_code=200,
            )
        )
        await db.flush()

    return JSONResponse(status_code=200, content={})
