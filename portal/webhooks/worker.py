from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import HTTPException
from sqlalchemy import select, update

from portal.database import get_session
from portal.models import OAuthAuditLog, WebhookDelivery, WebhookSubscription
from portal.routers.webhooks import validate_ssrf

logger = logging.getLogger(__name__)

CLAIM_BATCH_SIZE = 10
CLAIM_LEASE_SECONDS = 120
POLL_INTERVAL_SECONDS = 1
MAX_ATTEMPTS = 4
CIRCUIT_BREAKER_THRESHOLD = 5


@dataclass(frozen=True)
class DeliveryJob:
    """Snapshot of a claimed delivery, loaded before any network call."""

    delivery_id: int
    subscription_id: int
    event_type: str
    payload: dict
    payload_version: str
    attempt_count: int
    target_url: str
    secret_key: str
    subscription_active: bool


@dataclass(frozen=True)
class DeliveryAttempt:
    success: bool
    last_error: str | None = None


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
                        status="pending",
                    )
                    deliveries.append(delivery)

            if deliveries:
                db.add_all(deliveries)
                await db.flush()
    except Exception as e:
        logger.error(f"Failed to enqueue webhook: {e}", exc_info=True)


def sign_payload(secret: str, payload_str: str, timestamp: int) -> str:
    """Generate the X-VoxBento-Signature HMAC-SHA256."""
    mac = hmac.new(secret.encode(), msg=f"{timestamp}.{payload_str}".encode(), digestmod=hashlib.sha256)
    return mac.hexdigest()


async def recover_stale_claims(db, now: datetime) -> int:
    """Requeue deliveries whose claim lease expired (for example after a crash).

    A delivery that was claimed but never recorded stays in ``delivering``
    forever otherwise. ``next_attempt_at`` doubles as the lease deadline while
    a delivery is claimed, so an expired value means the worker is gone.
    """
    result = await db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.status == "delivering", WebhookDelivery.next_attempt_at <= now)
        .values(status="failed", next_attempt_at=now)
    )
    return result.rowcount or 0


async def claim_due_deliveries(db, now: datetime) -> list[int]:
    """Atomically claim a batch of due deliveries for this worker."""
    subquery = (
        select(WebhookDelivery.id)
        .where(WebhookDelivery.status.in_(["pending", "failed"]))
        .where(WebhookDelivery.next_attempt_at <= now)
        .order_by(WebhookDelivery.id)
        .limit(CLAIM_BATCH_SIZE)
    )

    stmt = (
        update(WebhookDelivery)
        .where(WebhookDelivery.id.in_(subquery))
        .values(
            status="delivering",
            next_attempt_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS),
        )
        .returning(WebhookDelivery.id)
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def load_delivery_job(db, delivery_id: int) -> DeliveryJob | None:
    """Read everything a dispatch needs, so the session can be closed before I/O."""
    result = await db.execute(
        select(WebhookDelivery, WebhookSubscription)
        .join(WebhookSubscription, WebhookSubscription.id == WebhookDelivery.subscription_id)
        .where(WebhookDelivery.id == delivery_id)
    )
    row = result.first()
    if row is None:
        return None

    delivery, subscription = row
    return DeliveryJob(
        delivery_id=delivery.id,
        subscription_id=subscription.id,
        event_type=delivery.event_type,
        payload=delivery.payload,
        payload_version=delivery.payload_version,
        attempt_count=delivery.attempt_count,
        target_url=subscription.target_url,
        secret_key=subscription.secret_key,
        subscription_active=subscription.is_active,
    )


async def mark_delivery_dead(db, delivery_id: int, reason: str) -> None:
    delivery = await db.get(WebhookDelivery, delivery_id)
    if delivery is not None:
        delivery.status = "dead"
        delivery.last_error = reason


async def send_delivery(client: httpx.AsyncClient, job: DeliveryJob) -> DeliveryAttempt:
    """Perform the outbound HTTP request. Must run outside any database transaction."""
    envelope = {
        "delivery_id": job.delivery_id,
        "event_type": job.event_type,
        "payload_version": job.payload_version,
        "data": job.payload,
    }
    payload_str = json.dumps(envelope)
    timestamp = int(time.time())
    signature = sign_payload(job.secret_key, payload_str, timestamp)

    headers = {"Content-Type": "application/json", "X-VoxBento-Signature": f"t={timestamp},v1={signature}"}

    try:
        response = await client.post(job.target_url, content=payload_str, headers=headers)
        response.raise_for_status()
        return DeliveryAttempt(success=True)
    except httpx.RequestError as e:
        return DeliveryAttempt(success=False, last_error=f"Request error: {e}")
    except httpx.HTTPStatusError as e:
        return DeliveryAttempt(success=False, last_error=f"HTTP error: {e.response.status_code}")


async def record_delivery_attempt(db, job: DeliveryJob, attempt: DeliveryAttempt) -> None:
    """Persist the outcome of one attempt in a short write transaction."""
    delivery = await db.get(WebhookDelivery, job.delivery_id)
    if delivery is None:
        return

    delivery.attempt_count = job.attempt_count + 1

    if attempt.success:
        delivery.status = "succeeded"
        delivery.last_error = None
        subscription = await db.get(WebhookSubscription, job.subscription_id)
        if subscription is not None and subscription.consecutive_failures > 0:
            subscription.consecutive_failures = 0
        return

    delivery.last_error = attempt.last_error
    if delivery.attempt_count < MAX_ATTEMPTS:
        delivery.status = "failed"
        delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=2**delivery.attempt_count)
        return

    delivery.status = "dead"
    subscription = await db.get(WebhookSubscription, job.subscription_id)
    if subscription is None:
        return

    subscription.consecutive_failures += 1
    if subscription.consecutive_failures < CIRCUIT_BREAKER_THRESHOLD:
        return

    subscription.is_active = False

    from portal.models import OAuthClient

    client_result = await db.execute(
        select(OAuthClient)
        .where(OAuthClient.developer_account_id == subscription.developer_account_id)
        .order_by(OAuthClient.id)
    )
    dev_client = client_result.scalars().first()

    db.add(
        OAuthAuditLog(
            client_id=dev_client.id if dev_client else None,
            action="webhook.circuit_breaker_tripped",
            request_path=f"/dispatch/{subscription.id}",
            status_code=500,
        )
    )


async def process_delivery(client: httpx.AsyncClient, delivery_id: int) -> None:
    """Dispatch one claimed delivery without holding a write transaction during I/O.

    The database is read and closed first, the HTTP request happens with no
    session open, and the result is persisted in a separate short transaction.
    Delivery is at-least-once: the ``delivery_id`` in the envelope lets
    receivers deduplicate a request that was accepted before its result was
    saved.
    """
    async with get_session() as db:
        job = await load_delivery_job(db, delivery_id)
        if job is None:
            return

        if not job.subscription_active:
            await mark_delivery_dead(db, delivery_id, "Subscription is inactive or deleted.")
            return

        try:
            validate_ssrf(job.target_url)
        except HTTPException as e:
            await mark_delivery_dead(db, delivery_id, f"SSRF Validation failed at dispatch: {e.detail}")
            return

    attempt = await send_delivery(client, job)

    async with get_session() as db:
        await record_delivery_attempt(db, job, attempt)


async def run_worker_iteration(client: httpx.AsyncClient) -> int:
    """Run one poll cycle: recover expired claims, then claim and dispatch due deliveries."""
    async with get_session() as db:
        now = datetime.now(timezone.utc)
        await recover_stale_claims(db, now)
        claimed_ids = await claim_due_deliveries(db, now)

    for delivery_id in claimed_ids:
        await process_delivery(client, delivery_id)

    return len(claimed_ids)


async def webhook_worker_loop():
    """Background polling worker for webhooks."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            try:
                claimed = await run_worker_iteration(client)

                # Sleep OUTSIDE the database session to release the SQLite lock
                if not claimed:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Webhook worker error: {e}", exc_info=True)
                await asyncio.sleep(5)
