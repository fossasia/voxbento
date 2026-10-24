from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import urllib.parse
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import jwt
import sqlalchemy as sa
from fastapi import APIRouter, Body, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from portal.auth import (
    create_participant_token,
    create_token,
    create_user_token,
    decode_token,
    get_current_user,
    hash_password,
    require_user,
    resolve_request_principal,
    verify_password,
)
from portal.config import settings
from portal.database import (
    close_open_hosting_request,
    create_auth_token,
    create_hosting_request,
    create_user,
    get_auth_token,
    get_developer_account,
    get_open_hosting_request,
    get_session,
    get_user_by_email,
    get_user_by_id,
    redeem_auth_token,
    redeem_invite_token,
    update_user,
)
from portal.globals import _JS_CACHE_BUST, asset_version_context
from portal.hosting_requests import format_event_dates, parse_hosting_request_form
from portal.models import HOSTING_REQUEST_GRANTED, TOKEN_TYPE_HOSTING_INVITE, AuthToken, User, utc_now
from portal.notifications import queue_notification
from portal.rate_limit import check_rate_limit
from portal.schemas.auth import TokenRequest, TokenResponse
from portal.user_menu import resolve_user_menu, user_menu_context
from portal.utils import safe_redirect, verify_same_origin

_BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(
    directory=str(_BASE_DIR / "templates"), context_processors=[user_menu_context, asset_version_context]
)

router = APIRouter()
logger = logging.getLogger(__name__)

# Hash of a fixed throwaway value; it is only used when no stored password
# exists, and the user-state checks below still reject the login.
_DUMMY_PASSWORD_HASH = "$2b$12$OEvt/xgDNq5iYebPcyO6L.ntTqE5qjv39rH/iPdAnvHg33d2vcG4e"


async def _queue_verification_email(
    session,
    email: str,
    user_id: int,
) -> None:
    """Create the token and its durable message in one transaction.

    Earlier verification links keep working: delivery happens later, so revoking
    them now could leave the person with no working link if this email fails.
    """
    token_val = secrets.token_hex(32)
    auth_token = await create_auth_token(session, jti=token_val, user_id=user_id, token_type="verification")  # nosec B105
    await queue_notification(
        session,
        "verification",
        email,
        {"verification_link": f"{settings.public_base_url.rstrip('/')}/auth/verify/{token_val}"},
        expires_at=auth_token.expires_at,
        idempotency_key=f"verification:{token_val}",
        auth_token_id=auth_token.id,
    )


async def _queue_password_notice(session, email: str, user_id: int, message: str) -> None:
    """Tell the account owner their password changed, so they notice if it wasn't them."""
    await queue_notification(
        session,
        "account_access",
        email,
        {"message": message, "login_url": f"{settings.public_base_url.rstrip('/')}/login"},
        idempotency_key=f"password:{user_id}:{utc_now().isoformat()}",
    )


def _registration_success(request: Request, email: str) -> object:
    """Render the account-neutral registration confirmation page."""
    return templates.TemplateResponse(
        request=request,
        name="register_success.html",
        context={"email": email},
    )


def _is_cookie_secure(request: Request) -> bool:
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        return True
    return bool(settings.public_base_url.startswith("https://") and not settings.debug)


def set_auth_cookie(response: Response, token: str) -> None:
    """Set the user authentication cookie with standard settings."""
    response.set_cookie(
        key="user_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=settings.jwt_expiry_seconds,
    )


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
    # Do not turn this unauthenticated endpoint into an account-enumeration
    # oracle. These constant legacy fields keep older login pages on the
    # authentication path without exposing real account state; they are
    # compatibility sentinels, not facts about this email address.
    return {"status": "ok", "exists": True, "has_password": True}


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
        # Run the expensive password work for every valid submission, even
        # when the address already exists. The result is only persisted for a
        # newly-created account.
        pw_hash = hash_password(password_raw) if password else None
        async with get_session() as session:
            existing = await get_user_by_email(session, email)
            if existing:
                if not existing.email_verified and check_rate_limit(
                    "verification_email", email, max_requests=1, window_seconds=900
                ):
                    if existing.is_active:
                        # A password on an unverified account may have been
                        # chosen by an earlier registrant. Do not let email
                        # verification activate those credentials; the owner
                        # can use the resulting magic-link session to set a
                        # new password.
                        existing.password_hash = None
                        await _queue_verification_email(session, email, existing.id)
                return _registration_success(request, email)

            try:
                async with session.begin_nested():
                    user = await create_user(
                        session,
                        email=email,
                        display_name=display_name,
                        password_hash=pw_hash,
                        email_verified=False,
                    )
            except IntegrityError:
                # Another request may have created this address after the
                # lookup. Preserve the same neutral response on the losing
                # request instead of surfacing a unique-constraint error.
                existing = await get_user_by_email(session, email)
                if existing is None:
                    raise
                if not existing.email_verified and check_rate_limit(
                    "verification_email", email, max_requests=1, window_seconds=900
                ):
                    if existing.is_active:
                        existing.password_hash = None
                        await _queue_verification_email(session, email, existing.id)
                return _registration_success(request, email)

            await _queue_verification_email(session, email, user.id)
            return _registration_success(request, email)

    return templates.TemplateResponse(
        request=request,
        name="register.html",
        context={"errors": errors, "email": email, "display_name": display_name, "js_version": _JS_CACHE_BUST},
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
    )


@router.get("/auth/verify/{token}")
async def verify_email_route(request: Request, token: str):
    async with get_session() as session:
        try:
            auth_token = await redeem_auth_token(session, token, "verification")
            user = await get_user_by_id(session, auth_token.user_id)
            if user is None or not user.is_active:
                raise ValueError("Invalid verification link.")
            user = await update_user(session, auth_token.user_id, email_verified=True)
            await queue_notification(
                session,
                "onboarding",
                user.email,
                {"display_name": user.display_name},
                scheduled_at=utc_now() + timedelta(minutes=15),
                idempotency_key=f"onboarding:{user.id}",
            )

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

    password_hash = user.password_hash if user and user.password_hash else _DUMMY_PASSWORD_HASH
    password_valid = verify_password(password, password_hash)
    if user is None or not user.password_hash or not password_valid or not user.is_active or not user.email_verified:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid email or password.", "email": email, "next_url": next_url},
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
    data = await request.json()
    email = data.get("email", "").strip().lower()
    next_url = data.get("next_url", "")

    if not check_rate_limit("magic_link", email, max_requests=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many requests")

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
            # The link signs in only the browser that opens it (#929); nothing here is
            # returned to the requesting browser.
            magic_link = f"{settings.public_base_url.rstrip('/')}/auth/magic/{token_val}"
            if next_url:
                magic_link += f"?next={urllib.parse.quote(next_url)}"
            await queue_notification(
                session,
                "magic_login",
                email,
                {"magic_link": magic_link},
                expires_at=auth_tok.expires_at,
                idempotency_key=f"magic-login:{token_val}",
                auth_token_id=auth_tok.id,
            )

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
                await queue_notification(
                    session,
                    "onboarding",
                    user.email,
                    {"display_name": user.display_name},
                    scheduled_at=utc_now() + timedelta(minutes=15),
                    idempotency_key=f"onboarding:{user.id}",
                )

            jwt_token = create_user_token(
                user_id=user.id, email=user.email, display_name=user.display_name, is_admin=user.is_admin
            )

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
                parsed = urlparse(redirect_to)
                q_pairs = parse_qsl(parsed.query, keep_blank_values=True)
                q_pairs.append(("auth_sync", "1"))
                redirect_to_sync = urlunparse(parsed._replace(query=urlencode(q_pairs)))
                response = safe_redirect(url=redirect_to_sync, status_code=status.HTTP_303_SEE_OTHER)

            response.set_cookie(
                key="user_token",
                value=jwt_token,
                httponly=True,
                secure=_is_cookie_secure(request),
                samesite="lax",
                max_age=settings.jwt_expiry_seconds,
            )
            response.set_cookie(
                key="auth_sync",
                value="1",
                max_age=15,
                httponly=False,
                secure=_is_cookie_secure(request),
                samesite="lax",
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
            reset_token = await create_auth_token(
                session,
                jti=token_val,
                user_id=user.id,
                token_type="password_reset",  # nosec B105
            )
            await queue_notification(
                session,
                "password_reset",
                email,
                {"reset_link": f"{settings.public_base_url.rstrip('/')}/auth/reset/{token_val}"},
                expires_at=reset_token.expires_at if reset_token else None,
                idempotency_key=f"password-reset:{token_val}",
                auth_token_id=reset_token.id if reset_token else None,
            )

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
            await _queue_password_notice(session, user.email, user.id, "Your VoxBento password was reset.")
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


ACCOUNT_NOTICES = {
    "hosting_request_sent": "hosting_request_sent",
    "profile_saved": "Your display name was saved.",
    "password_changed": "Your password was changed.",
    "password_set": "Your password was set. You can now sign in with it or with an email link.",
    "password_removed": "Your password was removed. Sign in with a link sent to your email from now on.",
}
DISPLAY_NAME_MAX = 200
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@router.get("/account")
async def account_page(request: Request):
    """The person's profile, access (hosting, developer API) and sign-in settings (#767)."""
    current_user = await get_current_user(request)
    if current_user is None:
        return safe_redirect(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    async with get_session() as session:
        user = await get_user_by_id(session, int(current_user["sub"]))
        if user is None:
            response = safe_redirect(url="/login", status_code=status.HTTP_303_SEE_OTHER)
            response.delete_cookie("user_token")
            return response
        open_hosting_request = await get_open_hosting_request(session, user.id)
        developer_account = await get_developer_account(session, user.id)
    await resolve_user_menu(request)

    success = request.query_params.get("success", "")
    return templates.TemplateResponse(
        request=request,
        name="account.html",
        context={
            "user": user,
            "is_hosting_revoked": user.hosting_revoked_at is not None,
            "open_hosting_request": open_hosting_request,
            "developer_account": developer_account,
            "hosting_request_sent": success == "hosting_request_sent",
            "notice": ACCOUNT_NOTICES.get(success) if success != "hosting_request_sent" else None,
            "error": request.query_params.get("error", ""),
            "display_name_max": DISPLAY_NAME_MAX,
        },
    )


@router.post("/account/profile")
async def update_profile(request: Request):
    """Change the signed-in person's display name."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")
    current_user = await get_current_user(request)
    if current_user is None:
        return safe_redirect(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    form = await request.form()
    raw = form.get("display_name")
    display_name = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not display_name or len(display_name) > DISPLAY_NAME_MAX or _CONTROL_CHARS.search(display_name):
        return safe_redirect(url="/account?error=display_name", status_code=status.HTTP_303_SEE_OTHER)

    async with get_session() as session:
        user = await get_user_by_id(session, int(current_user["sub"]))
        if user is None:
            return safe_redirect(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        user.display_name = display_name
    return safe_redirect(url="/account?success=profile_saved", status_code=status.HTTP_303_SEE_OTHER)


HOSTING_REQUEST_PATH = "/account/hosting-request"
HOSTING_REQUEST_FIELDS = ("organization", "event_name", "event_starts_on", "event_ends_on", "languages", "message")


def render_hosting_request_form(
    request: Request,
    user: User,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = status.HTTP_200_OK,
):
    return templates.TemplateResponse(
        request=request,
        name="account_hosting_request.html",
        context={"user": user, "values": values or {}, "errors": errors or {}},
        status_code=status_code,
    )


async def load_hosting_requester(request: Request, session: AsyncSession) -> tuple[User | None, Response | None]:
    """Return the signed-in user who may request hosting access, or the redirect to send instead."""
    principal = await resolve_request_principal(request, session)
    user = principal["user"]
    if user is None:
        return None, safe_redirect(url=f"/login?next={HOSTING_REQUEST_PATH}", status_code=status.HTTP_303_SEE_OTHER)
    if user.is_admin or user.can_host or await get_open_hosting_request(session, user.id) is not None:
        return None, safe_redirect(url="/account", status_code=status.HTTP_303_SEE_OTHER)
    return user, None


@router.get(HOSTING_REQUEST_PATH)
async def hosting_request_page(request: Request):
    """Show the hosting access request form to a signed-in user without hosting access."""
    async with get_session() as session:
        user, redirect = await load_hosting_requester(request, session)
    if redirect is not None:
        return redirect
    return render_hosting_request_form(request, user)


@router.post(HOSTING_REQUEST_PATH)
async def submit_hosting_request(request: Request):
    """Save a hosting access request for the signed-in user and notify the team."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")

    form = await request.form()
    cleaned, errors = parse_hosting_request_form(form)

    try:
        async with get_session() as session:
            user, redirect = await load_hosting_requester(request, session)
            if redirect is not None:
                return redirect
            if errors:
                values = {key: str(form.get(key, "")) for key in HOSTING_REQUEST_FIELDS}
                return render_hosting_request_form(
                    request, user, values=values, errors=errors, status_code=status.HTTP_400_BAD_REQUEST
                )
            hosting_request = await create_hosting_request(
                session,
                user_id=user.id,
                organization=cleaned.organization,
                event_name=cleaned.event_name,
                event_starts_on=cleaned.event_starts_on,
                event_ends_on=cleaned.event_ends_on,
                languages=cleaned.languages,
                message=cleaned.message,
            )
            user.hosting_requested_at = hosting_request.created_at
            await queue_notification(
                session,
                "hosting_request",
                settings.hosting_requests_email,
                {
                    "requester_name": user.display_name,
                    "requester_email": user.email,
                    "organization": cleaned.organization,
                    "event_name": cleaned.event_name,
                    "event_dates": format_event_dates(cleaned.event_starts_on, cleaned.event_ends_on),
                    "languages": cleaned.languages,
                    "message": cleaned.message,
                    "review_url": f"{settings.public_base_url.rstrip('/')}/admin/hosting-access/requests/",
                },
                idempotency_key=f"hosting-request:{hosting_request.id}",
            )
    except IntegrityError:
        # A concurrent submission already opened this user's one request.
        return safe_redirect(url="/account", status_code=status.HTTP_303_SEE_OTHER)

    logger.info("Hosting access request %d opened by user %d", hosting_request.id, hosting_request.user_id)
    return safe_redirect(url="/account?success=hosting_request_sent", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/auth/set-password")
async def set_password_api(request: Request):
    user_token = await require_user(request)
    data = await request.json()
    password = data.get("password", "") if isinstance(data, dict) else ""
    current_password = data.get("current_password", "") if isinstance(data, dict) else ""

    if not isinstance(password, str) or len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")

    async with get_session() as session:
        user = await get_user_by_id(session, int(user_token["sub"]))
        if user is None:
            raise HTTPException(status_code=400, detail="User not found")
        # Changing an existing password needs the current one; setting a first password does not.
        if user.password_hash and not (
            isinstance(current_password, str) and verify_password(current_password, user.password_hash)
        ):
            raise HTTPException(status_code=403, detail="Your current password is not correct")
        had_password = bool(user.password_hash)
        await update_user(session, user.id, password_hash=hash_password(password))
        await _queue_password_notice(
            session,
            user.email,
            user.id,
            "Your VoxBento password was changed." if had_password else "You set a password for your VoxBento account.",
        )

    return {"status": "ok"}


@router.post("/api/auth/remove-password")
async def remove_password_api(request: Request):
    user_token = await require_user(request)
    data = await request.json()
    password = data.get("password", "") if isinstance(data, dict) else ""

    async with get_session() as session:
        user = await get_user_by_id(session, int(user_token["sub"]))
        if not user or not user.password_hash:
            raise HTTPException(status_code=400, detail="You don't have a password to remove")

        if not isinstance(password, str) or not verify_password(password, user.password_hash):
            raise HTTPException(status_code=403, detail="Your current password is not correct")

        # Set directly: update_user() treats None as "leave unchanged".
        user.password_hash = None
        await _queue_password_notice(
            session, user.email, user.id, "Your VoxBento password was removed. You now sign in with email links."
        )

    return {"status": "ok"}


def _extract_cookie_user_id(request: Request) -> str | None:
    cookie = request.cookies.get("user_token")
    if not cookie:
        return None
    try:
        payload = decode_token(cookie)
        sub = payload.get("sub")
        return str(sub) if sub is not None else None
    except jwt.PyJWTError:
        return None
    except Exception as exc:
        logger.debug("Unexpected error decoding user_token cookie: %s", exc)
        return None


@router.get("/invite/accept/{token}")
async def invite_accept_page(request: Request, token: str):
    """Render the invitation acceptance page for a hosting access token."""
    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with get_session() as session:
        tok = await get_auth_token(session, token_digest)
        if tok is None or tok.token_type != TOKEN_TYPE_HOSTING_INVITE or tok.is_expired or tok.is_used:
            return templates.TemplateResponse(
                request=request,
                name="accept_invite.html",
                context={"valid": False, "error_message": "This invitation link is invalid or has expired."},
            )
        user = tok.user
        if user is None or not user.is_active:
            return templates.TemplateResponse(
                request=request,
                name="accept_invite.html",
                context={"valid": False, "error_message": "This account is inactive or invalid."},
            )

        if user.password_hash and not user.email_verified:
            return templates.TemplateResponse(
                request=request,
                name="accept_invite.html",
                context={
                    "valid": False,
                    "error_message": "Please verify your email address first before accepting this invitation.",
                },
            )

        is_signed_in_as_user = _extract_cookie_user_id(request) == str(user.id)
        has_password = bool(user.password_hash)

        return templates.TemplateResponse(
            request=request,
            name="accept_invite.html",
            context={
                "valid": True,
                "token": token,
                "email": user.email,
                "display_name": user.display_name,
                "is_signed_in_as_user": is_signed_in_as_user,
                "has_password": has_password,
            },
        )


@router.post("/invite/accept/{token}")
async def invite_accept_submit(request: Request, token: str):
    """Redeem a hosting invitation token, setting password if new, and granting can_host."""
    if not verify_same_origin(request):
        raise HTTPException(status_code=400, detail="Invalid request origin.")

    token_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    form = await request.form()
    submitted_password = form.get("password", "")

    try:
        async with get_session() as session:
            tok_stmt = (
                sa.select(AuthToken)
                .options(sa.orm.joinedload(AuthToken.user))
                .where(
                    AuthToken.jti == token_digest,
                    AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                )
            )
            tok_res = await session.execute(tok_stmt)
            tok = tok_res.scalar_one_or_none()

            if tok is None or tok.is_used or tok.is_expired:
                raise HTTPException(status_code=400, detail="Invalid or expired invite token.")

            user = tok.user
            if user is None or not user.is_active:
                raise HTTPException(status_code=403, detail="Account is deactivated.")

            is_signed_in_as_user = _extract_cookie_user_id(request) == str(user.id)
            has_existing_password = bool(user.password_hash)

            if has_existing_password and not user.email_verified:
                raise HTTPException(
                    status_code=400,
                    detail="Please verify your email address before accepting this invitation.",
                )

            if is_signed_in_as_user:
                # User is already signed in as this account; no password entry or creation needed
                pass
            elif has_existing_password:
                if not submitted_password or not verify_password(submitted_password, user.password_hash):
                    raise HTTPException(status_code=400, detail="Invalid password.")
            else:
                raw_confirm = form.get("password_confirm", "")
                display_name = form.get("display_name", "").strip()
                if len(submitted_password) < 8:
                    raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
                if submitted_password != raw_confirm:
                    raise HTTPException(status_code=400, detail="Passwords do not match.")
                user.password_hash = hash_password(submitted_password)
                user.email_verified = True
                if display_name:
                    user.display_name = display_name

            # Claim the token with an atomic conditional update
            now = utc_now()
            claim_stmt = (
                sa.update(AuthToken)
                .where(
                    AuthToken.id == tok.id,
                    AuthToken.used_at.is_(None),
                )
                .values(used_at=now)
                .execution_options(synchronize_session=False)
            )
            claim_res = await session.execute(claim_stmt)
            if claim_res.rowcount != 1:
                raise HTTPException(status_code=400, detail="Invalid or expired invite token.")

            # Perform account activation and hosting grant in the same transaction
            user.can_host = True
            user.hosting_revoked_at = None
            user.hosting_requested_at = None
            await close_open_hosting_request(session, user.id, status=HOSTING_REQUEST_GRANTED, decided_by_user_id=None)

            # Delete any other pending hosting_invite tokens for this user
            delete_stmt = (
                sa.delete(AuthToken)
                .where(
                    AuthToken.user_id == user.id,
                    AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                    AuthToken.id != tok.id,
                )
                .execution_options(synchronize_session=False)
            )
            await session.execute(delete_stmt)
            await session.flush()

            user_id = user.id
            email = user.email
            display_name = user.display_name
            is_admin = user.is_admin

    except (OperationalError, DBAPIError) as exc:
        err_msg = str(exc).lower()
        if "busy" in err_msg or "locked" in err_msg or "snapshot" in err_msg:
            # Re-read token in a fresh transaction to verify whether it was actually claimed
            # by a concurrent winner or if this is an unrelated transient SQLite lock.
            try:
                for _ in range(5):
                    async with get_session() as check_session:
                        tok_check = (
                            await check_session.execute(
                                sa.select(AuthToken).where(
                                    AuthToken.jti == token_digest,
                                    AuthToken.token_type == TOKEN_TYPE_HOSTING_INVITE,
                                )
                            )
                        ).scalar_one_or_none()
                        if tok_check is not None and (tok_check.used_at is not None or tok_check.is_used):
                            logger.warning("Concurrent redemption race lost on token: %s", exc)
                            raise HTTPException(status_code=400, detail="Invalid or expired invite token.")
                    await asyncio.sleep(0.05)
            except HTTPException:
                raise
            except (OperationalError, DBAPIError) as check_err:
                logger.debug("Failed to re-check token redemption status after lock contention: %s", check_err)
            logger.warning("Transient database lock during token redemption: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service temporarily unavailable, please retry.",
            )
        raise

    if is_signed_in_as_user:
        return safe_redirect("/workspace/")

    jwt_token = create_user_token(
        user_id=user_id,
        email=email,
        display_name=display_name,
        is_admin=is_admin,
    )
    response = safe_redirect("/workspace/")
    set_auth_cookie(response, jwt_token)
    return response
