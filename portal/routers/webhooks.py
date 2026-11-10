from __future__ import annotations

import ipaddress
import logging
import secrets
import socket
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from portal.auth import require_oauth_scope
from portal.database import get_db_session
from portal.models import (
    DeveloperAccount,
    OAuthAuditLog,
    OAuthClient,
    OAuthToken,
    WebhookDelivery,
    WebhookSubscription,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])


class WebhookCreate(BaseModel):
    target_url: str
    event_types: list[str]


class WebhookResponse(BaseModel):
    id: int
    target_url: str
    event_types: list[str]
    is_active: bool
    secret_key: str | None = None


def validate_ssrf(url: str) -> None:
    import os

    if os.getenv("ENVIRONMENT") == "development":
        if "localhost" in url or "127.0.0.1" in url:
            return

    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise ValueError("Webhook URLs must use HTTPS.")

        hostname = parsed.hostname
        if not hostname:
            raise ValueError("Invalid hostname.")

        ip = socket.gethostbyname(hostname)
        ip_obj = ipaddress.ip_address(ip)

        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast:
            raise ValueError(f"Webhook URL resolves to a forbidden internal IP address ({ip}).")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid webhook URL: {str(e)}")


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    data: WebhookCreate,
    response: Response = None,
    token: OAuthToken = Depends(require_oauth_scope("webhooks:manage")),
    db: AsyncSession = Depends(get_db_session),
):
    validate_ssrf(data.target_url)

    if not data.event_types:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="event_types cannot be empty")

    client_result = await db.execute(select(OAuthClient).where(OAuthClient.id == token.client_id))
    client = client_result.scalars().first()
    if not client:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid client")

    # Serialize webhook operations per developer account
    is_sqlite = db.get_bind().dialect.name == "sqlite"
    dev_stmt = select(DeveloperAccount).where(DeveloperAccount.id == client.developer_account_id)
    if not is_sqlite:
        dev_stmt = dev_stmt.with_for_update()
    await db.execute(dev_stmt)

    from portal.config import settings

    max_subs = getattr(settings, "max_webhook_subscriptions", 50)

    # Idempotency Check: if a subscription with the same target_url already exists
    # for this developer account, update and reactivate it rather than creating duplicates.
    existing_subs_result = await db.execute(
        select(WebhookSubscription)
        .where(
            WebhookSubscription.developer_account_id == client.developer_account_id,
            WebhookSubscription.target_url == data.target_url,
        )
        .order_by(WebhookSubscription.id.asc())
    )
    existing_subs = existing_subs_result.scalars().all()

    if existing_subs:
        sub = existing_subs[0]
        # Check active quota before reactivating an inactive subscription
        if not sub.is_active:
            count_result = await db.execute(
                select(WebhookSubscription).where(
                    WebhookSubscription.developer_account_id == client.developer_account_id,
                    WebhookSubscription.is_active.is_(True),
                )
            )
            active_subs = count_result.scalars().all()
            # Account for active duplicates of this target_url that will be pruned
            active_duplicates_count = sum(1 for s in active_subs if s.target_url == data.target_url)
            projected_active_count = len(active_subs) - active_duplicates_count + 1
            if projected_active_count > max_subs:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Maximum webhook subscriptions reached"
                )

        sub.event_types = sorted(set(data.event_types))
        sub.is_active = True
        sub.consecutive_failures = 0
        if not sub.secret_key:
            sub.secret_key = f"whsec_{secrets.token_urlsafe(32)}"

        # Prune any duplicate subscriptions for this exact target_url,
        # reassigning outstanding deliveries to the surviving subscription
        for duplicate in existing_subs[1:]:
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.subscription_id == duplicate.id)
                .values(subscription_id=sub.id)
            )
            await db.delete(duplicate)

        audit = OAuthAuditLog(
            token_id=token.id,
            client_id=client.id,
            action="webhook.updated",
            request_path="/api/v1/webhooks",
            status_code=status.HTTP_200_OK,
        )
        db.add(audit)
        await db.flush()

        if response is not None:
            response.status_code = status.HTTP_200_OK

        return WebhookResponse(
            id=sub.id,
            target_url=sub.target_url,
            event_types=sub.event_types,
            is_active=sub.is_active,
            secret_key=sub.secret_key,
        )

    # Quota Check for newly created webhook subscriptions
    count_result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.developer_account_id == client.developer_account_id,
            WebhookSubscription.is_active.is_(True),
        )
    )
    active_subs = count_result.scalars().all()
    if len(active_subs) >= max_subs:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Maximum webhook subscriptions reached"
        )

    secret_key = f"whsec_{secrets.token_urlsafe(32)}"

    sub = WebhookSubscription(
        developer_account_id=client.developer_account_id,
        target_url=data.target_url,
        event_types=data.event_types,
        secret_key=secret_key,
        is_active=True,
    )
    db.add(sub)
    await db.flush()  # Using flush because the context manager handles commit

    # Audit log
    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=client.id,
        action="webhook.created",
        request_path="/api/v1/webhooks",
        status_code=status.HTTP_201_CREATED,
    )
    db.add(audit)
    await db.flush()

    return WebhookResponse(
        id=sub.id,
        target_url=sub.target_url,
        event_types=sub.event_types,
        is_active=sub.is_active,
        secret_key=sub.secret_key,
    )


@router.get("", response_model=list[WebhookResponse])
async def list_webhooks(
    token: OAuthToken = Depends(require_oauth_scope("webhooks:manage")),
    db: AsyncSession = Depends(get_db_session),
):
    client_result = await db.execute(select(OAuthClient).where(OAuthClient.id == token.client_id))
    client = client_result.scalars().first()
    if not client:
        return []

    result = await db.execute(
        select(WebhookSubscription).where(WebhookSubscription.developer_account_id == client.developer_account_id)
    )
    subs = result.scalars().all()

    return [
        WebhookResponse(
            id=s.id, target_url=s.target_url, event_types=s.event_types, is_active=s.is_active, secret_key=None
        )
        for s in subs
    ]


@router.delete("/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    sub_id: int,
    token: OAuthToken = Depends(require_oauth_scope("webhooks:manage")),
    db: AsyncSession = Depends(get_db_session),
):
    client_result = await db.execute(select(OAuthClient).where(OAuthClient.id == token.client_id))
    client = client_result.scalars().first()
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    result = await db.execute(
        select(WebhookSubscription).where(
            WebhookSubscription.id == sub_id, WebhookSubscription.developer_account_id == client.developer_account_id
        )
    )
    sub = result.scalars().first()
    if not sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    await db.delete(sub)

    # Audit log
    audit = OAuthAuditLog(
        token_id=token.id,
        client_id=client.id,
        action="webhook.deleted",
        request_path=f"/api/v1/webhooks/{sub_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    db.add(audit)
    await db.flush()


@router.get("/debug/dump", include_in_schema=False)
async def debug_dump(db: AsyncSession = Depends(get_db_session)):
    from portal.models import WebhookDelivery

    subs_result = await db.execute(select(WebhookSubscription))
    subs = subs_result.scalars().all()
    deliv_result = await db.execute(select(WebhookDelivery))
    delivs = deliv_result.scalars().all()
    return {
        "subscriptions": [{"id": s.id, "event_types": s.event_types} for s in subs],
        "deliveries": [{"id": d.id, "status": d.status, "last_error": d.last_error} for d in delivs],
    }
