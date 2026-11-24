from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from portal.auth import is_cookie_secure, require_user
from portal.config import settings
from portal.database import get_db_session
from portal.globals import asset_version_context
from portal.models import DeveloperAccount, OAuthAuditLog, OAuthClient, WebhookSubscription
from portal.notifications import queue_notification
from portal.user_menu import resolve_user_menu, user_menu_context

logger = logging.getLogger(__name__)

# Loads the signed-in person once per request for the shared header and user menu.
router = APIRouter(dependencies=[Depends(resolve_user_menu)])

_BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(
    directory=str(_BASE_DIR / "templates"), context_processors=[user_menu_context, asset_version_context]
)

# Column sizes of DeveloperAccount.organization_name / OAuthClient.name and
# OAuthAuthorizationCode.redirect_uri. Longer values fail on insert in PostgreSQL.
MAX_NAME_LENGTH = 200
MAX_REDIRECT_URI_LENGTH = 500
MAX_REDIRECT_URIS = 10
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def clean_name(value: str, label: str) -> str:
    """Strip a dashboard form name and reject blank or over-long values."""
    name = value.strip()
    if not name:
        raise ValueError(f"{label} cannot be empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValueError(f"{label} must be {MAX_NAME_LENGTH} characters or fewer.")
    # PostgreSQL rejects NUL in text columns; other control characters have no place in a display name.
    if not name.isprintable():
        raise ValueError(f"{label} contains characters that are not allowed.")
    return name


def redirect_uri_problem(uri: str) -> str | None:
    """Return why ``uri`` cannot be registered as an OAuth redirect URI, or None if it can."""
    if len(uri) > MAX_REDIRECT_URI_LENGTH:
        return f"it is longer than {MAX_REDIRECT_URI_LENGTH} characters"
    if any(ch.isspace() or not ch.isprintable() for ch in uri):
        return "it contains spaces or control characters"
    try:
        parts = urlsplit(uri)
        # Reading .port is what raises ValueError for a malformed port.
        hostname, _port = parts.hostname, parts.port
    except ValueError:
        return "it is not a valid URL"
    if parts.scheme not in ("https", "http"):
        return "it must start with https://"
    if not hostname:
        return "it has no host"
    if parts.username is not None or parts.password is not None:
        return "it must not contain a username or password"
    if "#" in uri:
        return "it must not contain a fragment (#)"
    if parts.scheme == "http" and hostname not in LOOPBACK_HOSTS:
        return "it must use https (http is only allowed for localhost)"
    return None


def parse_redirect_uris(raw: str) -> tuple[str, ...]:
    """Split the comma-separated redirect URIs from the dashboard form and validate each one.

    The authorization endpoint redirects the browser to a registered URI with the
    authorization code attached, so only absolute http(s) URLs are accepted here.
    """
    uris = tuple(dict.fromkeys(uri.strip() for uri in raw.split(",") if uri.strip()))
    if not uris:
        raise ValueError("Enter at least one redirect URI.")
    if len(uris) > MAX_REDIRECT_URIS:
        raise ValueError(f"An app can have at most {MAX_REDIRECT_URIS} redirect URIs.")
    for uri in uris:
        problem = redirect_uri_problem(uri)
        if problem:
            shown = uri if len(uri) <= 80 else f"{uri[:77]}..."
            raise ValueError(f"Redirect URI '{shown}' was not accepted: {problem}.")
    return uris


def generate_developer_csrf_token(user_id: int | str) -> str:
    """Generate an HMAC-signed CSRF token bound to the authenticated user ID."""
    nonce = secrets.token_hex(16)
    message = f"developer:{user_id}:{nonce}".encode("utf-8")
    sig = hmac.new(settings.effective_jwt_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"{nonce}.{sig}"


def is_valid_developer_csrf_token(token: str | None, user_id: int | str) -> bool:
    """Verify that a CSRF token has a valid HMAC signature bound to user_id."""
    if not token or "." not in token:
        return False
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    nonce, sig = parts
    message = f"developer:{user_id}:{nonce}".encode("utf-8")
    expected_sig = hmac.new(settings.effective_jwt_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return secrets.compare_digest(sig, expected_sig)


def get_dashboard_csrf_cookie(request: Request) -> str | None:
    """Read the dashboard CSRF cookie, checking __Host- prefix first then plain name."""
    return request.cookies.get("__Host-dashboard_csrf") or request.cookies.get("dashboard_csrf")


def set_dashboard_csrf_cookie(response: Response, request: Request, token: str) -> None:
    """Set the dashboard CSRF cookie with appropriate flags."""
    secure = is_cookie_secure(request)
    response.set_cookie(
        "dashboard_csrf",
        token,
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def verify_developer_csrf(request: Request, csrf_token: str, user_id: int | str) -> None:
    """Verify that submitted CSRF token matches the cookie and is signed for user_id."""
    cookie_token = get_dashboard_csrf_cookie(request)
    if (
        not cookie_token
        or not secrets.compare_digest(csrf_token, cookie_token)
        or not is_valid_developer_csrf_token(cookie_token, user_id)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


def _get_flash_fernet() -> Fernet:
    """Derive a Fernet key from settings.effective_jwt_secret for short-lived flash cookies."""
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.effective_jwt_secret.encode("utf-8")).digest())
    return Fernet(key)


async def render_dashboard(
    request: Request,
    user: dict,
    db: AsyncSession,
    account: DeveloperAccount | None,
    csrf_token: str,
    status_code: int = status.HTTP_200_OK,
    form_error: str | None = None,
    new_client: OAuthClient | None = None,
    new_secret: str | None = None,
    is_rotation: bool = False,
) -> Response:
    clients = []
    webhooks = []
    if account and account.status == "approved":
        client_result = await db.execute(select(OAuthClient).where(OAuthClient.developer_account_id == account.id))
        clients = client_result.scalars().all()

        webhook_result = await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.developer_account_id == account.id)
        )
        webhooks = webhook_result.scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="developer/dashboard.html",
        context={
            "user": user,
            "account": account,
            "clients": clients,
            "webhooks": webhooks,
            "new_client": new_client,
            "new_secret": new_secret,
            "is_rotation": is_rotation,
            "form_error": form_error,
            "csrf_token": csrf_token,
        },
        status_code=status_code,
    )


@router.get("/developer", include_in_schema=False)
async def developer_dashboard(
    request: Request,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    account = result.scalars().first()

    cookie_csrf = get_dashboard_csrf_cookie(request)
    should_set_cookie = False
    if not cookie_csrf or not is_valid_developer_csrf_token(cookie_csrf, user["sub"]):
        csrf_token = generate_developer_csrf_token(user["sub"])
        should_set_cookie = True
    else:
        csrf_token = cookie_csrf

    new_client = None
    new_secret = None
    is_rotation = False
    flash_cookie = request.cookies.get("dev_flash_secret")
    if flash_cookie:
        try:
            payload_bytes = _get_flash_fernet().decrypt(flash_cookie.encode("utf-8"), ttl=60)
            flash_data = json.loads(payload_bytes.decode("utf-8"))
            if flash_data.get("user_id") == int(user["sub"]):
                flash_client_id = flash_data.get("client_id")
                c_res = await db.execute(select(OAuthClient).where(OAuthClient.client_id == flash_client_id))
                new_client = c_res.scalars().first()
                if new_client:
                    new_secret = flash_data.get("secret")
                    is_rotation = bool(flash_data.get("is_rotation", False))
        except Exception:
            logger.warning("Failed to decrypt dev_flash_secret cookie", exc_info=True)

    response = await render_dashboard(
        request,
        user,
        db,
        account,
        csrf_token,
        new_client=new_client,
        new_secret=new_secret,
        is_rotation=is_rotation,
    )
    if should_set_cookie:
        set_dashboard_csrf_cookie(response, request, csrf_token)
    if flash_cookie:
        response.delete_cookie("dev_flash_secret", path="/developer")
        response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/api/developer/apply")
async def apply_for_developer(
    request: Request,
    organization_name: str = Form(""),
    csrf_token: str = Form(...),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    verify_developer_csrf(request, csrf_token, user["sub"])
    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    existing = result.scalars().first()

    if existing:
        return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)

    try:
        organization_name = clean_name(organization_name, "Organization / project name")
    except ValueError as exc:
        cookie_csrf = get_dashboard_csrf_cookie(request) or csrf_token
        return await render_dashboard(
            request, user, db, None, cookie_csrf, status_code=status.HTTP_400_BAD_REQUEST, form_error=str(exc)
        )

    account = DeveloperAccount(
        user_id=int(user["sub"]),
        status="pending",
        organization_name=organization_name,
    )
    db.add(account)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)

    await queue_notification(
        db,
        "developer_account",
        str(user["email"]),
        {"message": "We received your developer account request.", "login_url": f"{settings.public_base_url.rstrip('/')}/developer"},
        idempotency_key=f"developer-account:{account.id}:received",
    )

    return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/developer/clients")
async def create_oauth_client(
    request: Request,
    # Empty defaults let missing or empty fields reach clean_name()/parse_redirect_uris() and get
    # the dashboard's 400 message; FastAPI would otherwise answer 422 before the handler runs.
    app_name: str = Form(""),
    redirect_uris: str = Form(""),
    csrf_token: str = Form(...),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    verify_developer_csrf(request, csrf_token, user["sub"])
    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    account = result.scalars().first()

    if not account or account.status != "approved":
        return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)

    try:
        app_name = clean_name(app_name, "App name")
        uris = parse_redirect_uris(redirect_uris)
    except ValueError as exc:
        cookie_csrf = get_dashboard_csrf_cookie(request) or csrf_token
        return await render_dashboard(
            request, user, db, account, cookie_csrf, status_code=status.HTTP_400_BAD_REQUEST, form_error=str(exc)
        )

    raw_client_id = f"client_{secrets.token_urlsafe(24)}"
    raw_secret = f"secret_{secrets.token_urlsafe(32)}"
    secret_hash = hashlib.sha256(raw_secret.encode()).hexdigest()

    from portal.routers.oauth import VALID_SCOPES

    client = OAuthClient(
        developer_account_id=account.id,
        client_id=raw_client_id,
        client_secret_hash=secret_hash,
        name=app_name,
        redirect_uris=list(uris),
        scopes_requested=list(VALID_SCOPES.keys()),
    )
    db.add(client)
    await db.flush()

    cookie_csrf = get_dashboard_csrf_cookie(request) or csrf_token
    return await render_dashboard(request, user, db, account, cookie_csrf, new_client=client, new_secret=raw_secret)


_client_rotation_locks: dict[str, asyncio.Lock] = {}


@router.post("/api/developer/clients/{client_id}/rotate-secret")
async def rotate_oauth_client_secret(
    client_id: str,
    request: Request,
    csrf_token: str = Form(...),
    expected_secret_hash: str | None = Form(None),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    verify_developer_csrf(request, csrf_token, user["sub"])

    lock = _client_rotation_locks.setdefault(client_id, asyncio.Lock())
    async with lock:
        # Query client belonging to the approved developer account of this user
        client_result = await db.execute(
            select(OAuthClient)
            .join(DeveloperAccount, OAuthClient.developer_account_id == DeveloperAccount.id)
            .where(
                OAuthClient.client_id == client_id,
                DeveloperAccount.user_id == int(user["sub"]),
                DeveloperAccount.status == "approved",
            )
        )
        client = client_result.scalars().first()
        if not client:
            acc_result = await db.execute(
                select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"]))
            )
            account = acc_result.scalars().first()
            if not account or account.status != "approved":
                return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)
            raise HTTPException(status_code=404, detail="OAuth client not found")

        if not client.is_confidential:
            raise HTTPException(status_code=400, detail="Public clients do not have a client secret")

        if expected_secret_hash is not None and expected_secret_hash != (client.client_secret_hash or ""):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Client secret rotation conflict. The secret was modified concurrently. Please refresh the page and try again.",
            )

        expected_old_hash = client.client_secret_hash
        raw_secret = f"secret_{secrets.token_urlsafe(32)}"
        new_secret_hash = hashlib.sha256(raw_secret.encode()).hexdigest()

        # Optimistic concurrency / CAS update to prevent overlapping rotations from overwriting newer secret
        update_result = await db.execute(
            update(OAuthClient)
            .where(
                OAuthClient.id == client.id,
                OAuthClient.client_secret_hash == expected_old_hash,
            )
            .values(client_secret_hash=new_secret_hash)
        )
        if update_result.rowcount != 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Client secret rotation conflict. The secret was modified concurrently. Please refresh the page and try again.",
            )

        audit = OAuthAuditLog(
            client_id=client.id,
            action="client.secret_rotated",
            request_path=f"/api/developer/clients/{client_id}/rotate-secret",
            status_code=status.HTTP_200_OK,
        )
        db.add(audit)
        # Commit the hash update explicitly before returning or flashing the secret
        await db.commit()

        # Post-Redirect-Get: deliver one-time flash secret through an encrypted short-lived cookie
        flash_payload = json.dumps({
            "client_id": client.client_id,
            "secret": raw_secret,
            "is_rotation": True,
            "user_id": int(user["sub"]),
        })
        encrypted_flash = _get_flash_fernet().encrypt(flash_payload.encode("utf-8")).decode("utf-8")

        response = RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            "dev_flash_secret",
            encrypted_flash,
            max_age=60,
            httponly=True,
            secure=is_cookie_secure(request),
            samesite="lax",
            path="/developer",
        )
        return response


@router.post("/api/developer/clients/{client_id}/delete")
async def delete_oauth_client(
    client_id: str,
    request: Request,
    csrf_token: str = Form(...),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    verify_developer_csrf(request, csrf_token, user["sub"])

    client_result = await db.execute(
        select(OAuthClient)
        .join(DeveloperAccount, OAuthClient.developer_account_id == DeveloperAccount.id)
        .where(
            OAuthClient.client_id == client_id,
            DeveloperAccount.user_id == int(user["sub"]),
            DeveloperAccount.status == "approved",
        )
    )
    client = client_result.scalars().first()
    if client:
        # Audit log
        audit = OAuthAuditLog(
            client_id=client.id,
            action="client.deleted",
            request_path=f"/api/developer/clients/{client_id}/delete",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        db.add(audit)
        await db.delete(client)
        await db.flush()

    return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/developer/webhooks/{webhook_id}/delete")
async def delete_webhook_subscription(
    webhook_id: int,
    request: Request,
    csrf_token: str = Form(...),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    verify_developer_csrf(request, csrf_token, user["sub"])

    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    account = result.scalars().first()

    if not account or account.status != "approved":
        return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)

    webhook_result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == webhook_id, WebhookSubscription.developer_account_id == account.id
        )
    )
    webhook = webhook_result.scalars().first()
    if webhook:
        # Audit log (no client_id since it's a developer-level dashboard deletion)
        audit = OAuthAuditLog(
            client_id=None,
            action="webhook.deleted",
            request_path=f"/api/developer/webhooks/{webhook_id}/delete",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        db.add(audit)
        await db.delete(webhook)
        await db.flush()

    return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)
