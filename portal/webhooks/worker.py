from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update

from portal.database import get_session
from portal.models import OAuthAuditLog, WebhookDelivery, WebhookSubscription
from portal.routers.webhooks import validate_ssrf

logger = logging.getLogger(__name__)

async def enqueue_webhook(event_type: str, payload: dict) -> None:
    """Helper to insert a new webhook delivery into the queue."""
    try:
        async with get_session() as db:
            result = await db.execute(select(WebhookSubscription).where(WebhookSubscription.is_active.is_(True)))
            subs = result.scalars().all()

            deliveries = []
            for sub in subs:
                if event_type in sub.event_types:
                    delivery = WebhookDelivery(
                        subscription_id=sub.id,
                        event_type=event_type,
                        payload=payload,
                        payload_version="1",
                        status="pending"
                    )
                    deliveries.append(delivery)

            if deliveries:
                db.add_all(deliveries)
                await db.commit()
    except Exception as e:
        logger.error(f"Failed to enqueue webhook: {e}", exc_info=True)


def sign_payload(secret: str, payload_str: str, timestamp: int) -> str:
    """Generate the X-VoxBento-Signature HMAC-SHA256."""
    mac = hmac.new(secret.encode(), msg=f"{timestamp}.{payload_str}".encode(), digestmod=hashlib.sha256)
    return mac.hexdigest()


async def process_delivery(client: httpx.AsyncClient, db, delivery: WebhookDelivery) -> None:
    result = await db.execute(
        select(WebhookSubscription).where(WebhookSubscription.id == delivery.subscription_id)
    )
    sub = result.scalars().first()

    if not sub or not sub.is_active:
        delivery.status = "dead"
        delivery.last_error = "Subscription is inactive or deleted."
        return

    try:
        validate_ssrf(sub.target_url)
    except Exception as e:
        delivery.status = "dead"
        delivery.last_error = f"SSRF Validation failed at dispatch: {e}"
        return

    envelope = {
        "delivery_id": delivery.id,
        "event_type": delivery.event_type,
        "payload_version": delivery.payload_version,
        "data": delivery.payload
    }
    payload_str = json.dumps(envelope)
    timestamp = int(time.time())
    signature = sign_payload(sub.secret_key, payload_str, timestamp)

    headers = {
        "Content-Type": "application/json",
        "X-VoxBento-Signature": f"t={timestamp},v1={signature}"
    }

    delivery.attempt_count += 1
    success = False
    last_error = None

    try:
        response = await client.post(sub.target_url, content=payload_str, headers=headers)
        response.raise_for_status()
        success = True
    except httpx.RequestError as e:
        last_error = f"Request error: {str(e)}"
    except httpx.HTTPStatusError as e:
        last_error = f"HTTP error: {e.response.status_code}"

    if success:
        delivery.status = "succeeded"
        delivery.last_error = None
        if sub.consecutive_failures > 0:
            sub.consecutive_failures = 0
            db.add(sub)
    else:
        delivery.last_error = last_error
        if delivery.attempt_count >= 4:
            delivery.status = "dead"
            sub.consecutive_failures += 1

            if sub.consecutive_failures >= 5:
                sub.is_active = False
                audit = OAuthAuditLog(
                    client_id=sub.developer_account_id,
                    action="webhook.circuit_breaker_tripped",
                    request_path=f"/dispatch/{sub.id}",
                    status_code=500
                )
                db.add(audit)
            db.add(sub)
        else:
            delivery.status = "failed"
            backoff_seconds = 2 ** delivery.attempt_count
            delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds)


async def webhook_worker_loop():
    """Background polling worker for webhooks."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            try:
                claimed = False
                async with get_session() as db:
                    now = datetime.now(timezone.utc)

                    subquery = (
                        select(WebhookDelivery.id)
                        .where(WebhookDelivery.status.in_(["pending", "failed"]))
                        .where(WebhookDelivery.next_attempt_at <= now)
                        .limit(10)
                    )

                    stmt = (
                        update(WebhookDelivery)
                        .where(WebhookDelivery.id.in_(subquery))
                        .values(status="delivering")
                        .returning(WebhookDelivery)
                    )

                    result = await db.execute(stmt)
                    claimed_deliveries = result.scalars().all()

                    if claimed_deliveries:
                        claimed = True
                        for delivery in claimed_deliveries:
                            await process_delivery(client, db, delivery)
                        await db.commit()

                # Sleep OUTSIDE the database session to release the SQLite lock
                if not claimed:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Webhook worker error: {e}", exc_info=True)
                await asyncio.sleep(5)
