from __future__ import annotations

import hashlib
import logging
import secrets
from pathlib import Path

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, status
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


@router.get("/developer", include_in_schema=False)
async def developer_dashboard(
    request: Request,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_db_session),
):
    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    account = result.scalars().first()

    clients = []
    webhooks = []
    if account and account.status == "approved":
        client_result = await db.execute(select(OAuthClient).where(OAuthClient.developer_account_id == account.id))
        clients = client_result.scalars().all()

        webhook_result = await db.execute(
            select(WebhookSubscription).where(WebhookSubscription.developer_account_id == account.id)
        )
        webhooks = webhook_result.scalars().all()

    csrf_token = request.cookies.get("dashboard_csrf")
    should_set_cookie = False
    if not csrf_token:
        csrf_token = secrets.token_hex(32)
        should_set_cookie = True

    response = templates.TemplateResponse(
        request=request,
        name="developer/dashboard.html",
        context={
            "user": user,
            "account": account,
            "clients": clients,
            "webhooks": webhooks,
            "csrf_token": csrf_token,
        },
    )
    if should_set_cookie:
        response.set_cookie("dashboard_csrf", csrf_token, httponly=True, samesite="lax")
    return response


@router.post("/api/developer/apply")
async def apply_for_developer(
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

    uris = [uri.strip() for uri in redirect_uris.split(",") if uri.strip()]

    raw_client_id = f"client_{secrets.token_urlsafe(24)}"
    raw_secret = f"secret_{secrets.token_urlsafe(32)}"
    secret_hash = hashlib.sha256(raw_secret.encode()).hexdigest()

    client = OAuthClient(
        developer_account_id=account.id,
        client_id=raw_client_id,
        client_secret_hash=secret_hash,
        name=app_name,
        redirect_uris=uris,
    )
    db.add(client)
    await db.flush()

    # Re-fetch all clients and webhooks to render the dashboard
    client_result = await db.execute(select(OAuthClient).where(OAuthClient.developer_account_id == account.id))
    clients = client_result.scalars().all()

    webhook_result = await db.execute(
        select(WebhookSubscription).where(WebhookSubscription.developer_account_id == account.id)
    )
    webhooks = webhook_result.scalars().all()

    response = templates.TemplateResponse(
        request=request,
        name="developer/dashboard.html",
        context={
            "user": user,
            "account": account,
            "clients": clients,
            "webhooks": webhooks,
            "new_client": client,
            "new_secret": raw_secret,
            "csrf_token": dashboard_csrf,
        },
    )
    return response


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
