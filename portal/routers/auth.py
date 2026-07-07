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
    verify_password,
)
from portal.config import settings
from portal.database import (
    create_user,
    get_session,
    get_user_by_email,
    get_user_by_id,
    list_booth_memberships_for_user,
    list_memberships_for_user,
    list_room_memberships_for_user,
    redeem_invite_token,
)
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
    password_confirm = form.get("password_confirm", "")

    errors = []
    if not email or "@" not in email:
        errors.append("Valid email is required.")
    if not display_name:
        errors.append("Display name is required.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")
    if password != password_confirm:
        errors.append("Passwords do not match.")

    if not errors:
        async with get_session() as session:
            existing = await get_user_by_email(session, email)
            if existing:
                errors.append("An account with this email already exists.")
            else:
                pw_hash = hash_password(password)
                user = await create_user(
                    session,
                    email=email,
                    display_name=display_name,
                    password_hash=pw_hash,
                )
                token = create_user_token(
                    user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
                )
                response = safe_redirect(url="/account", status_code=status.HTTP_303_SEE_OTHER)
                response.set_cookie(
                    key="user_token",
                    value=token,
                    httponly=True,
                    samesite="lax",
                    max_age=settings.jwt_expiry_seconds,
                )
                return response

    return templates.TemplateResponse(
        request,
        "register.html",
        {"errors": errors, "email": email, "display_name": display_name},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


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

    async with get_session() as session:
        user = await get_user_by_email(session, email)

    if user is None or not verify_password(password, user.password_hash):
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

    token = create_user_token(user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin)
    redirect_to = next_url if next_url else "/account"
    response = safe_redirect(url=redirect_to, status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="user_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_expiry_seconds,
    )
    return response


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
        unified_memberships.append({
            "context": m.event.display_name if m.event else '—',
            "link": f"/admin/events/{m.event.id}/" if m.event else "#",
            "type": "Event",
            "role": m.role,
            "created_at": m.created_at
        })
    for m in room_memberships:
        context_str = f"{m.room.event.display_name} - {m.room.display_name}" if m.room and m.room.event else (m.room.display_name if m.room else '—')
        link_str = f"/mission-control/{m.room.event.slug}/" if m.room and m.room.event else "#"
        unified_memberships.append({
            "context": context_str,
            "link": link_str,
            "type": "Room",
            "role": m.role,
            "created_at": m.created_at
        })
    for m in booth_memberships:
        context_str = f"{m.booth.event.display_name} - {m.booth.room.display_name} - {m.booth.language_name}" if m.booth and m.booth.event and m.booth.room else '—'
        link_str = f"/interpreter/{m.booth.event.slug}/{m.booth.language_code}" if m.booth and m.booth.event else "#"
        unified_memberships.append({
            "context": context_str,
            "link": link_str,
            "type": "Booth",
            "role": m.role,
            "created_at": m.created_at
        })

    unified_memberships.sort(key=lambda x: x["created_at"] or datetime.min.replace(tzinfo=timezone.utc))

    return templates.TemplateResponse(request, "account.html", {"user": user, "memberships": unified_memberships})
