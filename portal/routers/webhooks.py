from __future__ import annotations

import ipaddress
import logging
import secrets
import socket
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
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
from portal.webhooks.events import SUPPORTED_EVENT_TYPES, validate_event_types

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])

require_webhooks_manage = require_oauth_scope("webhooks:manage")


class WebhookCreate(BaseModel):
    target_url: str
    event_types: list[str] = Field(
        ...,
        description=(
            f"List of event names to subscribe to. Supported event types: {', '.join(sorted(SUPPORTED_EVENT_TYPES))}"
        ),
    )


class WebhookResponse(BaseModel):
    id: int
    target_url: str
    event_types: list[str]
    is_active: bool
    secret_key: str | None = None


def validate_ssrf(url: str) -> None:
    """Validate that the webhook URL uses HTTPS and does not resolve to a private IP.

    Raises HTTPException 400 if the scheme is not HTTPS, the hostname cannot be
    resolved, or the resolved address is in a private, loopback, link-local, or
    multicast range. Development mode permits localhost / loopback URLs.
    """
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


async def reconcile_subscriptions(
    db: AsyncSession, developer_account_id: int | None = None
) -> list[WebhookSubscription]:
    """Reconcile subscriptions with unsupported event names.

    Prunes unsupported event types from pre-existing subscriptions. If a
    subscription has no valid event types remaining, it is deactivated
    (is_active=False) so it stops consuming quota without receiving deliveries.

    Returns the list of matching subscriptions after reconciliation.
    """
    stmt = select(WebhookSubscription)
    if developer_account_id is not None:
        stmt = stmt.where(WebhookSubscription.developer_account_id == developer_account_id)

    result = await db.execute(stmt)
    subs = result.scalars().all()
    modified = False

    for sub in subs:
        valid_events = sorted({e for e in sub.event_types if e in SUPPORTED_EVENT_TYPES})
        changed = False

        if sub.event_types != valid_events:
            sub.event_types = valid_events
            changed = True

        if not valid_events and sub.is_active:
            sub.is_active = False
            changed = True

        if changed:
            db.add(sub)
            modified = True

    if modified:
        await db.flush()

    return list(subs)


@router.post("", response_model=WebhookResponse, status_code=status.HTTP_201_CREATED)
async def create_webhook(
    data: WebhookCreate,
    response: Response = None,
    token: OAuthToken = Depends(require_webhooks_manage),
    db: AsyncSession = Depends(get_db_session),
):
    """Create or update a webhook subscription for the authenticated client.

    Validates the target URL against SSRF and event_types against supported events.
    Reconciles pre-existing subscriptions for the developer account so stale
    unsupported subscriptions do not exhaust quota. Reactivates existing
    subscriptions for the same URL idempotently and prunes duplicate records.
    """
    validate_ssrf(data.target_url)

    try:
        validated_event_types = validate_event_types(data.event_types)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

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

    # Reconcile pre-existing subscriptions so stale unsupported events do not consume quota
    await reconcile_subscriptions(db, client.developer_account_id)

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

        sub.event_types = validated_event_types
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
        event_types=validated_event_types,
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
    token: OAuthToken = Depends(require_webhooks_manage),
    db: AsyncSession = Depends(get_db_session),
):
    """List all webhook subscriptions for the authenticated developer account.

    Reconciles subscriptions so that inactive or pruned event types are
    accurately reflected for the caller.
    """
    client_result = await db.execute(select(OAuthClient).where(OAuthClient.id == token.client_id))
    client = client_result.scalars().first()
    if not client:
        return []

    # Serialize webhook operations per developer account
    is_sqlite = db.get_bind().dialect.name == "sqlite"
    dev_stmt = select(DeveloperAccount).where(DeveloperAccount.id == client.developer_account_id)
    if not is_sqlite:
        dev_stmt = dev_stmt.with_for_update()
    await db.execute(dev_stmt)

    subs = await reconcile_subscriptions(db, client.developer_account_id)

    return [
        WebhookResponse(
            id=s.id, target_url=s.target_url, event_types=s.event_types, is_active=s.is_active, secret_key=None
        )
        for s in subs
    ]


@router.get("/events", response_model=list[str])
async def list_available_events(
    token: OAuthToken = Depends(require_webhooks_manage),
) -> list[str]:
    """Return all supported webhook event types in sorted order.

    Requires the webhooks:manage OAuth scope.
    """
    return sorted(SUPPORTED_EVENT_TYPES)


@router.delete("/{sub_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(
    sub_id: int,
    token: OAuthToken = Depends(require_webhooks_manage),
    db: AsyncSession = Depends(get_db_session),
):
    """Delete an existing webhook subscription by its ID.

    Requires the webhooks:manage OAuth scope and developer account ownership.
    """
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
    """Dump all subscriptions and deliveries for debugging purposes."""
    from portal.models import WebhookDelivery

    subs_result = await db.execute(select(WebhookSubscription))
    subs = subs_result.scalars().all()
    deliv_result = await db.execute(select(WebhookDelivery))
    delivs = deliv_result.scalars().all()
    return {
        "subscriptions": [{"id": s.id, "event_types": s.event_types} for s in subs],
        "deliveries": [{"id": d.id, "status": d.status, "last_error": d.last_error} for d in delivs],
    }
