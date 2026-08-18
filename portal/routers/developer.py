from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from portal.auth import require_user
from portal.database import get_session
from portal.models import DeveloperAccount, OAuthClient

logger = logging.getLogger(__name__)

router = APIRouter()

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))

@router.get("/developer", include_in_schema=False)
async def developer_dashboard(
    request: Request,
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    result = await db.execute(select(DeveloperAccount).where(DeveloperAccount.user_id == int(user["sub"])))
    account = result.scalars().first()

    clients = []
    if account and account.status == "approved":
        client_result = await db.execute(select(OAuthClient).where(OAuthClient.developer_account_id == account.id))
        clients = client_result.scalars().all()

    return templates.TemplateResponse(
        "developer/dashboard.html",
        {
            "request": request,
            "user": user,
            "account": account,
            "clients": clients,
        },
    )

@router.post("/api/developer/apply")
async def apply_for_developer(
    organization_name: str = Form(...),
    user: dict = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
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
    await db.commit()

    return RedirectResponse(url="/developer", status_code=status.HTTP_303_SEE_OTHER)
