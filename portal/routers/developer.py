from __future__ import annotations

import hashlib
import logging
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.auth import require_user
from portal.database import get_db_session
from portal.models import DeveloperAccount, OAuthAuditLog, OAuthClient, WebhookSubscription

logger = logging.getLogger(__name__)

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

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

    csrf_token = request.cookies.get("dashboard_csrf")
    should_set_cookie = False
    if not csrf_token:
        csrf_token = secrets.token_hex(32)
        should_set_cookie = True

    response = await render_dashboard(request, user, db, account, csrf_token)
    if should_set_cookie:
        response.set_cookie("dashboard_csrf", csrf_token, httponly=True, samesite="lax")
    return response


@router.post("/api/developer/apply")
async def apply_for_developer(
    request: Request,
    organization_name: str = Form(...),
    csrf_token: str = Form(...),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not dashboard_csrf or not secrets.compare_digest(csrf_token, dashboard_csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    existing = result.scalars().first()

    if existing:
        return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)

    try:
        organization_name = clean_name(organization_name, "Organization / project name")
    except ValueError as exc:
        return await render_dashboard(
            request, user, db, None, dashboard_csrf, status_code=status.HTTP_400_BAD_REQUEST, form_error=str(exc)
        )

    account = DeveloperAccount(
        user_id=int(user["sub"]),
        status="pending",
        organization_name=organization_name,
    )
    db.add(account)
    await db.flush()

    return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/api/developer/clients")
async def create_oauth_client(
    request: Request,
    app_name: str = Form(...),
    redirect_uris: str = Form(...),
    csrf_token: str = Form(...),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not dashboard_csrf or not secrets.compare_digest(csrf_token, dashboard_csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    account = result.scalars().first()

    if not account or account.status != "approved":
        return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)

    try:
        app_name = clean_name(app_name, "App name")
        uris = parse_redirect_uris(redirect_uris)
    except ValueError as exc:
        return await render_dashboard(
            request, user, db, account, dashboard_csrf, status_code=status.HTTP_400_BAD_REQUEST, form_error=str(exc)
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

    return await render_dashboard(request, user, db, account, dashboard_csrf, new_client=client, new_secret=raw_secret)


@router.post("/api/developer/clients/{client_id}/delete")
async def delete_oauth_client(
    client_id: str,
    csrf_token: str = Form(...),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not dashboard_csrf or not secrets.compare_digest(csrf_token, dashboard_csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    account = result.scalars().first()

    if not account or account.status != "approved":
        return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)

    client_result = await db.execute(
        select(OAuthClient).where(OAuthClient.client_id == client_id, OAuthClient.developer_account_id == account.id)
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
    csrf_token: str = Form(...),
    dashboard_csrf: str | None = Cookie(None),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    if not dashboard_csrf or not secrets.compare_digest(csrf_token, dashboard_csrf):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")

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
