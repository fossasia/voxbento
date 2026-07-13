from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request, status
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
from portal.rate_limit import check_rate_limit
from portal.schemas.auth import TokenRequest, TokenResponse
from portal.utils import safe_redirect

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

router = APIRouter()


@router.post("/api/auth/token", response_model=TokenResponse)
async def get_token(body: Annotated[TokenRequest | None, Body()] = None) -> TokenResponse:
    provided = body.token if body is not None else ""
    if settings.booth_access_token and provided != settings.booth_access_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid access token.")
    return TokenResponse(access_token=create_token())


@router.get("/join/{token}")
async def join_via_invite(token: str) -> RedirectResponse:
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
    return templates.TemplateResponse(request, "register.html", {})


@router.post("/register")
async def register_submit(request: Request):
    form = await request.form()
    email = form.get("email", "").strip().lower()
    display_name = form.get("display_name", "").strip()
    password = form.get("password", "")

    errors = []
    if not email or "@" not in email:
        errors.append("Valid email is required.")
    if not display_name:
        errors.append("Display name is required.")

    if not errors:
        async with get_session() as session:
            existing = await get_user_by_email(session, email)
            if existing:
                errors.append("An account with this email already exists.")
            else:
                pw_hash = hash_password(password) if password else None
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
                    request,
                    "register_success.html",
                    {"email": email},
                )

    return templates.TemplateResponse(
        request,
        "register.html",
        {"errors": errors, "email": email, "display_name": display_name},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@router.get("/auth/verify/{token}")
async def verify_email_route(request: Request, token: str):
    async with get_session() as session:
        try:
            auth_token = await redeem_auth_token(session, token, "verification")
            user = await update_user(session, auth_token.user_id, email_verified=True)

            jwt_token = create_user_token(
                user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
            )
            response = safe_redirect(url="/account", status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie(
                key="user_token",
                value=jwt_token,
                httponly=True,
                samesite="lax",
                max_age=settings.jwt_expiry_seconds,
            )
            return response
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.get("/login")
async def user_login_page(request: Request, next: str = ""):
    current_user = await get_current_user(request)
    if current_user:
        redirect_to = next if next and next.startswith("/") and not next.startswith("//") else "/account"
        return safe_redirect(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"next_url": next})


@router.post("/login")
async def user_login_submit(request: Request):
    form = await request.form()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    next_url = form.get("next_url", "")

    if not check_rate_limit("login", email, max_requests=10, window_seconds=3600):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many attempts. Try again later.", "email": email, "next_url": next_url},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    async with get_session() as session:
        user = await get_user_by_email(session, email)

    if user is None or not user.password_hash or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid email or password.", "email": email, "next_url": next_url},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not user.is_active:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Your account has been deactivated. Contact an admin.", "email": email, "next_url": next_url},
            status_code=status.HTTP_403_FORBIDDEN,
        )
    if not user.email_verified:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Please verify your email address first.", "email": email, "next_url": next_url},
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
        samesite="lax",
        max_age=settings.jwt_expiry_seconds,
    )
    return response


@router.post("/api/auth/magic-link")
async def request_magic_link(request: Request):
    data = await request.json()
    email = data.get("email", "").strip().lower()

    if not check_rate_limit("magic_link", email, max_requests=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many requests")

    async with get_session() as session:
        user = await get_user_by_email(session, email)
        if user:
            import secrets

            token_val = secrets.token_hex(32)
            await create_auth_token(
                session,
                jti=token_val,
                user_id=user.id,
                token_type="magic_link",  # nosec B105
            )
            await send_magic_login_email(email, token_val)

    return {"status": "ok"}


@router.get("/auth/magic/{token}")
async def redeem_magic_link(request: Request, token: str, next: str | None = None):
    async with get_session() as session:
        try:
            auth_token = await redeem_auth_token(session, token, "magic_link")
            user = await get_user_by_id(session, auth_token.user_id)
            if not user.is_active:
                raise ValueError("Account deactivated")

            # If they log in via magic link, we can implicitly consider their email verified
            if not user.email_verified:
                await update_user(session, user.id, email_verified=True)

            jwt_token = create_user_token(
                user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
            )
            redirect_to = next if next and next.startswith("/") and not next.startswith("//") else "/account"
            response = safe_redirect(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie(
                key="user_token",
                value=jwt_token,
                httponly=True,
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
    return templates.TemplateResponse(request, "reset_password.html", {"token": token})


@router.post("/auth/reset/{token}")
async def reset_password_submit(request: Request, token: str):
    form = await request.form()
    password = form.get("password", "")

    if len(password) < 8:
        return templates.TemplateResponse(
            request, "reset_password.html", {"token": token, "error": "Password must be at least 8 characters"}
        )

    async with get_session() as session:
        try:
            auth_token = await redeem_auth_token(session, token, "password_reset")
            pw_hash = hash_password(password)
            user = await update_user(session, auth_token.user_id, password_hash=pw_hash)

            jwt_token = create_user_token(
                user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
            )
            response = safe_redirect(url="/account", status_code=status.HTTP_303_SEE_OTHER)
            response.set_cookie(
                key="user_token",
                value=jwt_token,
                httponly=True,
                samesite="lax",
                max_age=settings.jwt_expiry_seconds,
            )
            return response
        except ValueError as e:
            return templates.TemplateResponse(request, "reset_password.html", {"token": token, "error": str(e)})


@router.get("/logout")
async def user_logout(request: Request):
    response = safe_redirect(url="/", status_code=status.HTTP_303_SEE_OTHER)
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
    for m in event_memberships:
        unified_memberships.append(
            {
                "context": m.event.display_name if m.event else "—",
                "link": f"/admin/events/{m.event.id}/" if m.event else "#",
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
        link_str = f"/interpreter/{m.booth.event.slug}/{m.booth.language_code}" if m.booth and m.booth.event else "#"
        unified_memberships.append(
            {"context": context_str, "link": link_str, "type": "Booth", "role": m.role, "created_at": m.created_at}
        )

    unified_memberships.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=timezone.utc))

    return templates.TemplateResponse(request, "account.html", {"user": user, "memberships": unified_memberships})


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
