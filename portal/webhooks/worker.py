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
# The lease must outlive a full batch. httpx applies the timeout per phase
# (connect, write, read, pool), so budget 4 phases per delivery. Individual
# requests still stay far below the lease, and the lease deadline is checked
# again before a result is written.
CLAIM_LEASE_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 5.0
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
    lease_expires_at: datetime


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
    a delivery is claimed, so an expired value means the worker is gone. The
    lost attempt is counted so a delivery whose result is never recorded still
    reaches the retry limit instead of looping forever.
    """
    result = await db.execute(
        select(
            WebhookDelivery.id,
            WebhookDelivery.subscription_id,
            WebhookDelivery.attempt_count,
        )
        .where(WebhookDelivery.status == "delivering", WebhookDelivery.next_attempt_at <= now)
        .order_by(WebhookDelivery.id)
        .with_for_update(skip_locked=True)
    )
    stale = result.all()

    for delivery_id, subscription_id, attempt_count in stale:
        next_attempt = attempt_count + 1
        if next_attempt >= MAX_ATTEMPTS:
            await db.execute(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == delivery_id)
                .values(
                    status="dead",
                    attempt_count=next_attempt,
                    last_error="Claim lease expired before the result was recorded.",
                )
            )
            await record_subscription_failure(db, subscription_id)
            continue

        await db.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id == delivery_id)
            .values(status="failed", attempt_count=next_attempt, next_attempt_at=now)
        )

    return len(stale)


async def claim_due_deliveries(db, now: datetime) -> list[int]:
    """Atomically claim a batch of due deliveries for this worker."""
    subquery = (
        select(WebhookDelivery.id)
        .where(WebhookDelivery.status.in_(["pending", "failed"]))
        .where(WebhookDelivery.next_attempt_at <= now)
        .order_by(WebhookDelivery.id)
        .limit(CLAIM_BATCH_SIZE)
        # PostgreSQL lets concurrent workers skip locked rows instead of waiting.
        # SQLite ignores this clause.
        .with_for_update(skip_locked=True)
    )

    stmt = (
        update(WebhookDelivery)
        .where(
            WebhookDelivery.id.in_(subquery),
            # Re-check claimability on the updated row. PostgreSQL re-evaluates
            # this predicate after waiting on a concurrent claim, so a second
            # worker cannot return IDs the first one just claimed.
            WebhookDelivery.status.in_(["pending", "failed"]),
            WebhookDelivery.next_attempt_at <= now,
        )
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
        lease_expires_at=delivery.next_attempt_at,
    )


async def mark_delivery_dead(db, job: DeliveryJob, reason: str) -> None:
    delivery = await locked_claimed_delivery(db, job)
    if delivery is not None:
        delivery.status = "dead"
        delivery.last_error = reason


async def locked_claimed_delivery(db, job: DeliveryJob) -> WebhookDelivery | None:
    """Lock and re-check that this worker still owns the claim."""
    delivery = await db.get(WebhookDelivery, job.delivery_id, with_for_update=True)
    if delivery is None or delivery.status != "delivering" or delivery.next_attempt_at != job.lease_expires_at:
        return None
    return delivery


async def release_claim(job: DeliveryJob, reason: str) -> None:
    """Record an unexpected failure through the normal retry/dead-letter path."""
    async with get_session() as db:
        await record_delivery_attempt(db, job, DeliveryAttempt(success=False, last_error=reason))


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
    except httpx.InvalidURL as e:
        return DeliveryAttempt(success=False, last_error=f"Invalid URL: {e}")
    except httpx.RequestError as e:
        return DeliveryAttempt(success=False, last_error=f"Request error: {e}")
    except httpx.HTTPStatusError as e:
        return DeliveryAttempt(success=False, last_error=f"HTTP error: {e.response.status_code}")


async def record_delivery_attempt(db, job: DeliveryJob, attempt: DeliveryAttempt) -> None:
    """Persist the outcome of one attempt in a short write transaction."""
    delivery = await locked_claimed_delivery(db, job)
    if delivery is None:
        return

    delivery.attempt_count = job.attempt_count + 1

    if attempt.success:
        delivery.status = "succeeded"
        delivery.last_error = None
        await db.execute(
            update(WebhookSubscription)
            .where(
                WebhookSubscription.id == job.subscription_id,
                WebhookSubscription.consecutive_failures > 0,
            )
            .values(consecutive_failures=0)
        )
        return

    delivery.last_error = attempt.last_error
    if delivery.attempt_count < MAX_ATTEMPTS:
        delivery.status = "failed"
        delivery.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=2**delivery.attempt_count)
        return

    delivery.status = "dead"
    await record_subscription_failure(db, job.subscription_id)


async def record_subscription_failure(db, subscription_id: int) -> None:
    """Increment the breaker counter atomically and trip it at the threshold."""
    result = await db.execute(
        update(WebhookSubscription)
        .where(WebhookSubscription.id == subscription_id)
        .values(consecutive_failures=WebhookSubscription.consecutive_failures + 1)
        .returning(
            WebhookSubscription.consecutive_failures,
            WebhookSubscription.developer_account_id,
        )
    )
    row = result.first()
    if row is None or row.consecutive_failures < CIRCUIT_BREAKER_THRESHOLD:
        return

    await db.execute(
        update(WebhookSubscription).where(WebhookSubscription.id == subscription_id).values(is_active=False)
    )

    from portal.models import OAuthClient

    client_result = await db.execute(
        select(OAuthClient).where(OAuthClient.developer_account_id == row.developer_account_id).order_by(OAuthClient.id)
    )
    dev_client = client_result.scalars().first()

    db.add(
        OAuthAuditLog(
            client_id=dev_client.id if dev_client else None,
            action="webhook.circuit_breaker_tripped",
            request_path=f"/dispatch/{subscription_id}",
            status_code=500,
        )
    )


async def process_delivery(
    client: httpx.AsyncClient,
    delivery_id: int,
    job: DeliveryJob | None = None,
) -> None:
    """Dispatch one claimed delivery without holding a write transaction during I/O.

    The database is read and closed first, the HTTP request happens with no
    session open, and the result is persisted in a separate short transaction.
    Delivery is at-least-once: the ``delivery_id`` in the envelope lets
    receivers deduplicate a request that was accepted before its result was
    saved.
    """
    if job is None:
        async with get_session() as db:
            job = await load_delivery_job(db, delivery_id)

    if job is None:
        return

    if not job.subscription_active:
        async with get_session() as db:
            await mark_delivery_dead(db, job, "Subscription is inactive or deleted.")
        return

    try:
        # validate_ssrf does blocking DNS resolution; keep it out of the loop
        # and out of the database session.
        await asyncio.to_thread(validate_ssrf, job.target_url)
    except HTTPException as e:
        async with get_session() as db:
            await mark_delivery_dead(db, job, f"SSRF Validation failed at dispatch: {e.detail}")
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
        async with get_session() as db:
            job = await load_delivery_job(db, delivery_id)
        if job is None:
            continue

        try:
            await process_delivery(client, delivery_id, job)
        except Exception:
            # Isolate one bad delivery so the rest of the claimed batch still runs.
            logger.exception("Webhook delivery %s failed unexpectedly", delivery_id)
            try:
                await release_claim(job, "Unexpected dispatch error.")
            except Exception:
                logger.exception("Failed to release webhook delivery %s", delivery_id)

    return len(claimed_ids)


async def webhook_worker_loop():
    """Background polling worker for webhooks."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
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
