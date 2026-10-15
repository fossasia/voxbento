from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from portal.auth import (
    create_participant_token,
    create_token,
    create_user_token,
    get_current_user,
    hash_password,
    require_user,
    verify_password,
)
from portal.config import settings
from portal.database import (
    create_auth_token,
    create_user,
    get_auth_token,
    get_session,
    get_user_by_email,
    get_user_by_id,
    list_booth_memberships_for_user,
    list_memberships_for_user,
    list_room_memberships_for_user,
    redeem_auth_token,
    redeem_invite_token,
    update_user,
)
from portal.email import send_magic_login_email, send_password_reset_email, send_verification_email
from portal.email_sender import send_delayed_onboarding_email
from portal.globals import _JS_CACHE_BUST
from portal.rate_limit import check_rate_limit
from portal.schemas.auth import TokenRequest, TokenResponse
from portal.utils import safe_redirect

_BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

router = APIRouter()
logger = logging.getLogger(__name__)

# In-memory registry for ephemeral magic link polling sessions:
#   poll_id -> {token_jti, nonce_hash, user_id, expires_at, consumed, created_at}
#
# Architectural Note on Multi-Worker / Clustered Environments:
# The core authentication security state (magic token cryptographic validity, expiration,
# user active status, and single-use `used_at` timestamp) is strictly persisted and validated
# in the database (`AuthToken` table).
# This `_magic_polls` registry is strictly an ephemeral 15-minute handshake cache for cross-context
# polling fallback (/api/auth/magic-poll). In modern browsers, authentication synchronizes
# immediately client-side via BroadcastChannel('voxbento_auth') without touching the server poll.
# Because VoxBento currently runs as a single Uvicorn container instance without a multi-worker
# cluster or Redis layer, this in-memory dictionary avoids unnecessary database migrations and I/O overhead.
# If VoxBento is scaled horizontally to multi-worker processes or multi-replica deployments
# without sticky sessions in the future, this ephemeral poll state should be backed by Redis
# or persisted to the database.
_magic_polls: dict[str, dict] = {}


def _is_cookie_secure(request: Request) -> bool:
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        return True
    return bool(settings.public_base_url.startswith("https://") and not settings.debug)


def _cleanup_expired_polls():
    import time

    now = time.time()
    expired = [k for k, v in _magic_polls.items() if now - v.get("created_at", 0) > 900]
    for k in expired:
        _magic_polls.pop(k, None)


@router.post("/api/auth/token", response_model=TokenResponse)
async def get_token(body: Annotated[TokenRequest | None, Body()] = None) -> TokenResponse:
    provided = body.token if body is not None else ""
    if settings.booth_access_token and provided != settings.booth_access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token.")
    return TokenResponse(access_token=create_token())


@router.get("/join/{token}")
async def join_via_invite(request: Request, token: str) -> RedirectResponse:
    """Validate an invite token, issue a JWT cookie, and redirect to the booth."""
    async with get_session() as session:
        try:
            tok = await redeem_invite_token(session, token)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))

    if tok is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invite token.")

    jwt_token = create_participant_token(
        booth_id=tok.booth_id,
        role=tok.role,
        event_slug=tok.booth.event.slug,
        room_id=tok.booth.room_id,
        language_code=tok.booth.language_code,
    )

    if tok.role == "listener":
        redirect_url = f"/listener/{tok.booth.event.slug}"
    else:
        redirect_url = "/interpreter"

    response = safe_redirect(url=redirect_url, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="session_token",
        value=jwt_token,
        httponly=True,
        secure=_is_cookie_secure(request),
        samesite="lax",
        max_age=settings.jwt_expiry_seconds,
    )
    return response


@router.post("/api/auth/check-email")
async def check_email_api(request: Request):
    data = await request.json()
    email = data.get("email", "").strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="Email required")
    async with get_session() as session:
        user = await get_user_by_email(session, email)
    if user:
        return {"exists": True, "has_password": user.password_hash is not None}
    return {"exists": False, "has_password": False}


@router.get("/register")
async def register_page(request: Request):
    current_user = await get_current_user(request)
    if current_user:
        return safe_redirect(url="/account", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="register.html", context={"js_version": _JS_CACHE_BUST})


@router.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    email = form.get("email", "").strip().lower()
    display_name = form.get("display_name", "").strip()
    password_raw = form.get("password", "")
    password = password_raw.strip()
    password_confirm = form.get("password_confirm", "")

    errors = []
    if not email or "@" not in email:
        errors.append("Valid email is required.")
    if not display_name:
        errors.append("Display name is required.")
    if password_raw and not password:
        errors.append("Password cannot be blank or only whitespace.")
    elif password_raw and len(password_raw) < 8:
        errors.append("Password must be at least 8 characters.")
    elif password_raw and password_raw != password_confirm:
        errors.append("Passwords do not match.")

    if not errors:
        async with get_session() as session:
            existing = await get_user_by_email(session, email)
            if existing:
                errors.append("An account with this email already exists.")
            else:
                pw_hash = hash_password(password_raw) if password else None
                user = await create_user(
                    session,
                    email=email,
                    display_name=display_name,
                    password_hash=pw_hash,
                    email_verified=False,
                )

                # Send verification email
                import secrets

                token_val = secrets.token_hex(32)
                await create_auth_token(
                    session,
                    jti=token_val,
                    user_id=user.id,
                    token_type="verification",  # nosec B105
                )
                await send_verification_email(email, token_val)

                return templates.TemplateResponse(
                    request=request,
                    name="register_success.html",
                    context={"email": email},
                )

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"errors": errors, "email": email, "display_name": display_name, "js_version": _JS_CACHE_BUST},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@router.get("/auth/verify/{token}")
async def verify_email_route(request: Request, token: str, background_tasks: BackgroundTasks):
    async with get_session() as session:
        try:
            auth_token = await redeem_auth_token(session, token, "verification")
            user = await update_user(session, auth_token.user_id, email_verified=True)
            background_tasks.add_task(send_delayed_onboarding_email, user.email, user.display_name)

            jwt_token = create_user_token(
                user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
            )
            response = safe_redirect(url="/account", status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie(
                key="user_token",
                value=jwt_token,
                httponly=True,
                secure=_is_cookie_secure(request),
                samesite="lax",
                max_age=settings.jwt_expiry_seconds,
            )
            return response
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.get("/login")
async def user_login_page(request: Request, next: str = "", email: str = "", switch: bool = False):
    if not switch:
        current_user = await get_current_user(request)
        if current_user:
            redirect_to = next if next and next.startswith("/") and not next.startswith("//") else "/account"
            return safe_redirect(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request=request, name="login.html", context={"next_url": next, "email": email, "prefill_email": email}
    )


@router.post("/login")
async def user_login_submit(request: Request):
    form = await request.form()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    next_url = form.get("next_url", "")

    if not check_rate_limit("login", email, max_requests=10, window_seconds=3600):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Too many attempts. Try again later.", "email": email, "next_url": next_url},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    async with get_session() as session:
        user = await get_user_by_email(session, email)

    if user is None or not user.password_hash or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid email or password.", "email": email, "next_url": next_url},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={
                "error": "Your account has been deactivated. Contact an admin.",
                "email": email,
                "next_url": next_url,
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not user.email_verified:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Please verify your email address first.", "email": email, "next_url": next_url},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    jwt_token = create_user_token(
        user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
    )
    redirect_to = next_url if next_url else "/account"
    response = safe_redirect(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="user_token",
        value=jwt_token,
        httponly=True,
        secure=_is_cookie_secure(request),
        samesite="lax",
        max_age=settings.jwt_expiry_seconds,
    )
    return response


@router.get("/api/auth/status")
async def get_auth_status(request: Request):
    user = await get_current_user(request)
    if user:
        return {"authenticated": True, "email": user.get("email"), "user_id": user.get("sub")}
    return {"authenticated": False}


@router.post("/api/auth/magic-link")
async def request_magic_link(request: Request):
    import hashlib
    import secrets
    import time
    from datetime import datetime, timedelta, timezone

    from fastapi.responses import JSONResponse

    data = await request.json()
    email = data.get("email", "").strip().lower()
    next_url = data.get("next_url", "")

    if not check_rate_limit("magic_link", email, max_requests=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many requests")

    _cleanup_expired_polls()
    poll_id = secrets.token_hex(32)
    poll_nonce = secrets.token_hex(32)
    nonce_hash = hashlib.sha256(poll_nonce.encode()).hexdigest()

    async with get_session() as session:
        user = await get_user_by_email(session, email)
        if user:
            token_val = secrets.token_hex(32)
            auth_tok = await create_auth_token(
                session,
                jti=token_val,
                user_id=user.id,
                token_type="magic_link",  # nosec B105
            )

            # Bind poll to requesting browser and token expiry
            expires_at = (
                auth_tok.expires_at
                if hasattr(auth_tok, "expires_at") and auth_tok.expires_at
                else datetime.now(timezone.utc) + timedelta(minutes=15)
            )
            _magic_polls[poll_id] = {
                "token_jti": token_val,
                "nonce_hash": nonce_hash,
                "user_id": user.id,
                "expires_at": expires_at,
                "consumed": False,
                "created_at": time.time(),
            }
            sent = await send_magic_login_email(email, token_val, next_url)
            if not sent:
                _magic_polls.pop(poll_id, None)
                logger.error("Failed to deliver magic link email to %s", email)

    resp = JSONResponse({"status": "ok", "poll_id": poll_id})
    resp.set_cookie(
        key="poll_verifier",
        value=f"{poll_id}:{poll_nonce}",
        httponly=True,
        secure=_is_cookie_secure(request),
        samesite="lax",
        max_age=900,
    )
    return resp


@router.get("/api/auth/magic-poll")
async def poll_magic_link(request: Request, poll_id: str = ""):
    """Poll magic link authentication state for cross-context fallback.

    Note: Same-browser authentication synchronizes client-side via BroadcastChannel.
    This endpoint serves as an ephemeral polling fallback backed by `_magic_polls`.
    In multi-worker or clustered deployments without sticky sessions, an external
    shared store (e.g. Redis or database-backed polling state) would be required.
    """
    from fastapi.responses import JSONResponse

    if not poll_id or len(poll_id) != 64:
        return {"authenticated": False}

    _cleanup_expired_polls()
    poll_entry = _magic_polls.get(poll_id)
    if not poll_entry or poll_entry.get("consumed"):
        return {"authenticated": False}

    # 1. Bind poll capability to the browser that requested the link via cookie
    cookie_verifier = request.cookies.get("poll_verifier", "")
    if not cookie_verifier or ":" not in cookie_verifier:
        return {"authenticated": False}

    c_poll_id, c_nonce = cookie_verifier.split(":", 1)
    if c_poll_id != poll_id:
        return {"authenticated": False}

    import hashlib

    expected_nonce_hash = poll_entry.get("nonce_hash") or poll_entry.get("secret_hash")
    if hashlib.sha256(c_nonce.encode()).hexdigest() != expected_nonce_hash:
        return {"authenticated": False}

    # 2. Reject expired magic-link tokens before issuing a JWT
    from datetime import datetime, timezone

    now_utc = datetime.now(timezone.utc)
    exp = poll_entry.get("expires_at")
    if exp:
        exp_utc = exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp
        if exp_utc < now_utc:
            _magic_polls.pop(poll_id, None)
            return {"authenticated": False}

    from sqlalchemy import select

    from portal.models import AuthToken

    async with get_session() as session:
        result = await session.execute(select(AuthToken).where(AuthToken.jti == poll_entry["token_jti"]))
        matched = result.scalars().first()

        if not matched or not matched.used_at:
            return {"authenticated": False}

        # Check token expiration on DB record as well
        if matched.expires_at:
            matched_exp = (
                matched.expires_at.replace(tzinfo=timezone.utc)
                if matched.expires_at.tzinfo is None
                else matched.expires_at
            )
            if matched_exp < now_utc:
                _magic_polls.pop(poll_id, None)
                return {"authenticated": False}

        user = await get_user_by_id(session, matched.user_id)
        if not user or not user.is_active:
            return {"authenticated": False}

        # 3. Single-use: Mark consumed and remove from active poll registry
        poll_entry["consumed"] = True
        _magic_polls.pop(poll_id, None)

        jwt_token = create_user_token(
            user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
        )
        response = JSONResponse(
            {
                "authenticated": True,
                "email": user.email,
                "user_id": user.id,
            }
        )
        response.set_cookie(
            key="user_token",
            value=jwt_token,
            httponly=True,
            secure=_is_cookie_secure(request),
            samesite="lax",
            max_age=settings.jwt_expiry_seconds,
        )
        # Clear poll_verifier cookie as authentication is complete
        response.delete_cookie("poll_verifier")
        return response


@router.get("/auth/magic/{token}")
async def redeem_magic_link(request: Request, token: str, background_tasks: BackgroundTasks, next: str | None = None):
    async with get_session() as session:
        try:
            auth_token = await redeem_auth_token(session, token, "magic_link")
            user = await get_user_by_id(session, auth_token.user_id)
            if not user.is_active:
                raise ValueError("Account deactivated")

            # If they log in via magic link, we can implicitly consider their email verified
            if not user.email_verified:
                await update_user(session, user.id, email_verified=True)
                background_tasks.add_task(send_delayed_onboarding_email, user.email, user.display_name)

            jwt_token = create_user_token(
                user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
            )

            from urllib.parse import urlparse

            def _is_local_oauth_path(u: str | None) -> bool:
                if not u or not isinstance(u, str):
                    return False
                try:
                    parsed = urlparse(u)
                    if parsed.scheme or parsed.netloc:
                        return False
                    return parsed.path.startswith("/oauth/authorize") and not u.startswith("//")
                except Exception:
                    return False

            if _is_local_oauth_path(next):
                response = templates.TemplateResponse(
                    request=request, name="magic_success.html", context={"next_url": next}
                )
            else:
                redirect_to = (
                    next
                    if next and next.startswith("/") and not next.startswith("//") and not urlparse(next).netloc
                    else "/account"
                )
                response = safe_redirect(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)

            response.set_cookie(
                key="user_token",
                value=jwt_token,
                httponly=True,
                secure=_is_cookie_secure(request),
                samesite="lax",
                max_age=settings.jwt_expiry_seconds,
            )
            return response
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.post("/api/auth/forgot-password")
async def forgot_password_request(request: Request):
    data = await request.json()
    email = data.get("email", "").strip().lower()

    if not check_rate_limit("forgot_password", email, max_requests=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many requests")

    async with get_session() as session:
        user = await get_user_by_email(session, email)
        if user and user.password_hash:
            import secrets

            token_val = secrets.token_hex(32)
            await create_auth_token(
                session,
                jti=token_val,
                user_id=user.id,
                token_type="password_reset",  # nosec B105
            )
            await send_password_reset_email(email, token_val)

    return {"status": "ok"}


@router.get("/auth/reset/{token}")
async def reset_password_page(request: Request, token: str):
    async with get_session() as session:
        tok = await get_auth_token(session, token)
        error = None
        if tok is None or tok.token_type != "password_reset":
            error = "Invalid reset link."
        elif tok.is_expired:
            error = "This reset link has expired."
        elif tok.is_used:
            error = "This reset link has already been used."

        if error:
            return templates.TemplateResponse(
                request=request,
                name="reset_password.html",
                context={"token": token, "error": error, "token_invalid": True},
                status_code=status.HTTP_400_BAD_REQUEST,
            )

    return templates.TemplateResponse(request=request, name="reset_password.html", context={"token": token})


@router.post("/auth/reset/{token}")
async def reset_password_submit(request: Request, token: str):
    form = await request.form()
    password = form.get("password", "")

    if len(password) < 8:
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={"token": token, "error": "Password must be at least 8 characters"},
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        async with get_session() as session:
            try:
                auth_token = await redeem_auth_token(session, token, "password_reset")
            except ValueError as e:
                return templates.TemplateResponse(
                    request=request,
                    name="reset_password.html",
                    context={"token": token, "error": str(e), "token_invalid": True},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

            pw_hash = hash_password(password)
            user = await update_user(session, auth_token.user_id, password_hash=pw_hash)
            if user is None:
                raise ValueError("User not found.")
            jwt_token = create_user_token(
                user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
            )
    except Exception:
        logger.exception("Failed to complete password reset for token")
        return templates.TemplateResponse(
            request=request,
            name="reset_password.html",
            context={
                "token": token,
                "error": "An error occurred while resetting your password. Please try again.",
            },
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    response = safe_redirect(url="/account", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="user_token",
        value=jwt_token,
        httponly=True,
        secure=_is_cookie_secure(request),
        samesite="lax",
        max_age=settings.jwt_expiry_seconds,
    )
    return response


@router.get("/logout")
async def user_logout(request: Request, next: str = "/"):
    target = next if next and next.startswith("/") and not next.startswith("//") else "/"
    response = safe_redirect(url=target, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("user_token")
    response.delete_cookie("session_token")
    response.delete_cookie("admin_token")
    return response


@router.get("/account")
async def account_page(request: Request):
    current_user = await get_current_user(request)
    if current_user is None:
        return safe_redirect(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    async with get_session() as session:
        user = await get_user_by_id(session, int(current_user["sub"]))
        if user is None:
            response = safe_redirect(url="/login", status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie("user_token")
            return response
        event_memberships = await list_memberships_for_user(session, user.id)
        room_memberships = await list_room_memberships_for_user(session, user.id)
        booth_memberships = await list_booth_memberships_for_user(session, user.id)

    unified_memberships = []
    coordinated_event_slugs = {
        m.room.event_id: m.room.event.slug
        for m in room_memberships
        if m.role == "room_coordinator" and m.room and m.room.event
    }
    for m in event_memberships:
        if m.event is None:
            link = "#"
        elif m.role == "event_owner":
            link = f"/workspace/events/{m.event.id}/"
        elif m.event_id in coordinated_event_slugs:
            link = f"/mission-control/{coordinated_event_slugs[m.event_id]}/"
        else:
            link = "#"
        unified_memberships.append(
            {
                "context": m.event.display_name if m.event else "—",
                "link": link,
                "type": "Event",
                "role": m.role,
                "created_at": m.created_at,
            }
        )
    for m in room_memberships:
        context_str = (
            f"{m.room.event.display_name} - {m.room.display_name}"
            if m.room and m.room.event
            else (m.room.display_name if m.room else "—")
        )
        link_str = f"/mission-control/{m.room.event.slug}/" if m.room and m.room.event else "#"
        unified_memberships.append(
            {"context": context_str, "link": link_str, "type": "Room", "role": m.role, "created_at": m.created_at}
        )
    for m in booth_memberships:
        context_str = (
            f"{m.booth.event.display_name} - {m.booth.room.display_name} - {m.booth.language_name}"
            if m.booth and m.booth.event and m.booth.room
            else "—"
        )
        link_str = (
            f"/interpreter/{m.booth.event.slug}/{m.booth.room_id}/{m.booth.language_code}"
            if m.booth and m.booth.event
            else "#"
        )
        unified_memberships.append(
            {"context": context_str, "link": link_str, "type": "Booth", "role": m.role, "created_at": m.created_at}
        )

    unified_memberships.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=timezone.utc))

    if user.is_admin:
        management_home = "/admin/"
        management_label = "Admin Panel"
    elif any(m.role == "event_owner" for m in event_memberships):
        management_home = "/workspace/"
        management_label = "Organizer Workspace"
    elif any(m.role == "room_coordinator" for m in room_memberships):
        management_home = "/mission-control/"
        management_label = "Mission Control"
    else:
        management_home = "/account"
        management_label = "My Account"

    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "user": user,
            "memberships": unified_memberships,
            "management_home": management_home,
            "management_label": management_label,
        },
    )


@router.post("/api/auth/set-password")
async def set_password_api(request: Request):
    user_token = await require_user(request)
    data = await request.json()
    password = data.get("password", "")

    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    async with get_session() as session:
        pw_hash = hash_password(password)
        await update_user(session, int(user_token["sub"]), password_hash=pw_hash)

    return {"status": "ok"}


@router.post("/api/auth/remove-password")
async def remove_password_api(request: Request):
    user_token = await require_user(request)
    data = await request.json()
    password = data.get("password", "")

    async with get_session() as session:
        user = await get_user_by_id(session, int(user_token["sub"]))
        if not user or not user.password_hash:
            raise HTTPException(status_code=400, detail="User has no password")

        if not verify_password(password, user.password_hash):
            raise HTTPException(status_code=403, detail="Invalid password")

        await update_user(session, user.id, password_hash=None)

    return {"status": "ok"}
