from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portal import database
from portal.models import (
    Base,
    DeveloperAccount,
    OAuthAuditLog,
    User,
    WebhookDelivery,
    WebhookSubscription,
)
from portal.routers.webhooks import validate_ssrf
from portal.webhooks import worker
from portal.webhooks.worker import claim_due_deliveries, process_delivery, run_worker_iteration, sign_payload

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Yield an async session backed by an in-memory SQLite database."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        async with session.begin():
            yield session


@pytest.fixture
async def worker_db(tmp_path):
    """Configure the real database factory against a file-backed SQLite DB.

    File-backed SQLite is required to reproduce the write-lock regression:
    concurrent connections in the same process do not share an in-memory DB.
    """
    await database.dispose()
    database.configure(f"sqlite+aiosqlite:///{tmp_path / 'webhooks.db'}")
    await database.init_db()
    try:
        yield
    finally:
        await database.dispose()


async def seed_delivery(
    *,
    attempt_count: int = 0,
    consecutive_failures: int = 0,
    status: str = "delivering",
    next_attempt_at: datetime | None = None,
    delivery_count: int = 1,
) -> tuple[int, int, int]:
    """Create a user, developer account, subscription and deliveries.

    Returns ``(delivery_id, subscription_id, developer_account_id)`` for the
    first delivery.
    """
    async with database.get_session() as session:
        user = User(email="webhook@example.invalid", display_name="Webhook Owner")
        session.add(user)
        await session.flush()

        account = DeveloperAccount(user_id=user.id, status="approved")
        session.add(account)
        await session.flush()

        subscription = WebhookSubscription(
            developer_account_id=account.id,
            target_url="https://example.com/webhook",
            event_types=["session.status_changed"],
            secret_key="secret",
            is_active=True,
            consecutive_failures=consecutive_failures,
        )
        session.add(subscription)
        await session.flush()

        deliveries = [
            WebhookDelivery(
                subscription_id=subscription.id,
                event_type="session.status_changed",
                payload={"is_active": True, "index": index},
                status=status,
                attempt_count=attempt_count,
                next_attempt_at=next_attempt_at or datetime.now(timezone.utc),
            )
            for index in range(delivery_count)
        ]
        session.add_all(deliveries)
        await session.flush()

        return deliveries[0].id, subscription.id, account.id


def successful_client() -> AsyncMock:
    client = AsyncMock(spec=httpx.AsyncClient)
    response = MagicMock()
    response.raise_for_status = MagicMock()
    client.post.return_value = response
    return client


# ---------------------------------------------------------------------------
# Tests for Webhook Utilities
# ---------------------------------------------------------------------------


def test_sign_payload():
    secret = "whsec_test123"
    payload = '{"test": "data"}'
    timestamp = 1620000000

    signature = sign_payload(secret, payload, timestamp)
    # The signature should be a valid SHA-256 hex string (64 characters)
    assert len(signature) == 64
    assert isinstance(signature, str)


def test_validate_ssrf():
    # Valid HTTPS external URL
    validate_ssrf("https://webhook.site/123")

    # HTTP is rejected (must use HTTPS)
    with pytest.raises(HTTPException) as exc:
        validate_ssrf("http://example.com/api/webhooks")
    assert exc.value.status_code == 400

    # Internal IPs (should fail)

    with pytest.raises(HTTPException) as exc:
        validate_ssrf("https://192.168.1.5")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        validate_ssrf("https://10.0.0.1")
    assert exc.value.status_code == 400

    # Invalid URL structures
    with pytest.raises(HTTPException) as exc:
        validate_ssrf("not_a_url")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        validate_ssrf("ftp://example.com")
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Tests for Background Worker
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_process_delivery_success(worker_db):
    delivery_id, _, _ = await seed_delivery()

    mock_client = successful_client()

    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        assert delivery.status == "succeeded"
        assert delivery.attempt_count == 1
        assert delivery.last_error is None

    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://example.com/webhook"

    # Signature header embeds the timestamp: "t=<epoch>,v1=<hmac>"
    sig_header = kwargs["headers"]["X-VoxBento-Signature"]
    assert sig_header.startswith("t="), f"Expected signature to start with 't=', got: {sig_header}"
    assert ",v1=" in sig_header, f"Expected ',v1=' in signature, got: {sig_header}"

    posted_payload = json.loads(kwargs["content"])
    assert posted_payload["delivery_id"] == delivery_id
    assert posted_payload["event_type"] == "session.status_changed"
    assert posted_payload["payload_version"] == "1"
    assert posted_payload["data"] == {"is_active": True, "index": 0}


@pytest.mark.anyio
async def test_process_delivery_http_error_retry(worker_db):
    delivery_id, _, _ = await seed_delivery()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client.post.side_effect = httpx.HTTPStatusError(
        "500 Server Error", request=MagicMock(), response=mock_response
    )

    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.attempt_count == 1
        assert delivery.last_error == "HTTP error: 500"
        assert delivery.next_attempt_at is not None
        # Backoff is 2^1 = 2 seconds
        assert delivery.next_attempt_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)


@pytest.mark.anyio
async def test_process_delivery_timeout_retries(worker_db):
    delivery_id, _, _ = await seed_delivery()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = httpx.ReadTimeout("timed out")

    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.attempt_count == 1
        assert delivery.last_error.startswith("Request error:")


@pytest.mark.anyio
async def test_process_delivery_circuit_breaker(worker_db):
    delivery_id, subscription_id, _ = await seed_delivery(
        attempt_count=3,  # Next attempt will be 4th (max)
        consecutive_failures=4,  # 1 more to disable
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = httpx.RequestError("Connection timeout")

    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        subscription = await session.get(WebhookSubscription, subscription_id)
        audits = (await session.execute(select(OAuthAuditLog))).scalars().all()

    assert delivery.status == "dead"
    assert delivery.attempt_count == 4
    assert subscription.consecutive_failures == 5
    assert subscription.is_active is False  # Circuit broken
    assert len(audits) == 1
    assert audits[0].action == "webhook.circuit_breaker_tripped"
    assert audits[0].client_id is None  # Zero-client case coverage


@pytest.mark.anyio
async def test_process_delivery_circuit_breaker_with_multiple_clients(worker_db):
    from portal.models import OAuthClient

    delivery_id, subscription_id, account_id = await seed_delivery(attempt_count=3, consecutive_failures=4)

    async with database.get_session() as session:
        session.add_all(
            [
                OAuthClient(developer_account_id=account_id, client_id="client_1", name="C1"),
                OAuthClient(developer_account_id=account_id, client_id="client_2", name="C2"),
            ]
        )
        await session.flush()
        client1_id = await session.scalar(select(OAuthClient.id).where(OAuthClient.client_id == "client_1"))

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = httpx.RequestError("Connection timeout")

    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        audits = (await session.execute(select(OAuthAuditLog))).scalars().all()

    assert len(audits) == 1
    assert audits[0].action == "webhook.circuit_breaker_tripped"
    assert audits[0].client_id == client1_id  # Verifies order_by(OAuthClient.id) selects the oldest client


@pytest.mark.anyio
async def test_atomic_queue_claim(worker_db):
    """Two workers cannot claim the same delivery and the batch limit is honoured."""
    await seed_delivery(delivery_count=15, status="pending")
    now = datetime.now(timezone.utc)

    async with database.get_session() as session:
        claimed1 = await claim_due_deliveries(session, now)
    async with database.get_session() as session:
        claimed2 = await claim_due_deliveries(session, now)

    assert len(claimed1) == 10
    assert len(claimed2) == 5
    assert set(claimed1).isdisjoint(claimed2)


@pytest.mark.anyio
async def test_worker_recovers_expired_claim(worker_db):
    """A delivery left in ``delivering`` by a crashed worker is retried."""
    delivery_id, _, _ = await seed_delivery(
        status="delivering",
        next_attempt_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )
    mock_client = successful_client()

    claimed = await run_worker_iteration(mock_client)

    assert claimed == 1
    mock_client.post.assert_called_once()
    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        assert delivery.status == "succeeded"


@pytest.mark.anyio
async def test_slow_receiver_does_not_block_unrelated_writes(worker_db):
    """A held webhook response must not keep a SQLite write lock open."""
    await seed_delivery(delivery_count=2, status="pending")
    async with database.get_session() as session:
        user_id = await session.scalar(select(User.id))

    first_request = asyncio.Event()
    request_count = 0

    async def receiver(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        first_request.set()
        await asyncio.sleep(1.5)
        return httpx.Response(200, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(receiver), timeout=5.0)

    async def poll_worker() -> None:
        while True:
            await worker.run_worker_iteration(client)
            await asyncio.sleep(0.05)

    task = asyncio.create_task(poll_worker())
    try:
        with patch.object(worker, "validate_ssrf", return_value=None):
            await asyncio.wait_for(first_request.wait(), timeout=5)

            started = time.monotonic()
            async with database.get_session() as session:
                await session.execute(update(User).where(User.id == user_id).values(display_name="After"))
            write_seconds = time.monotonic() - started

            assert write_seconds < 1.0, f"unrelated write blocked for {write_seconds:.2f}s"

            statuses = []
            for _ in range(200):
                async with database.get_session() as session:
                    statuses = list(
                        (await session.scalars(select(WebhookDelivery.status).order_by(WebhookDelivery.id))).all()
                    )
                if statuses == ["succeeded", "succeeded"]:
                    break
                await asyncio.sleep(0.05)

            async with database.get_session() as session:
                display_name = await session.scalar(select(User.display_name).where(User.id == user_id))

        assert request_count == 2
        assert display_name == "After"
        assert statuses == ["succeeded", "succeeded"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await client.aclose()


@pytest.mark.anyio
async def test_create_webhook_idempotency_and_deduplication(db, monkeypatch):
    from portal.models import DeveloperAccount, Event, OAuthClient, OAuthToken, User
    from portal.routers.webhooks import WebhookCreate, create_webhook

    monkeypatch.setattr("portal.routers.webhooks.validate_ssrf", lambda url: None)

    user = User(email="dev@example.com", display_name="Dev User", is_active=True)
    db.add(user)
    await db.flush()

    event = Event(slug="dev-event", display_name="Dev Event")
    db.add(event)
    await db.flush()

    dev_account = DeveloperAccount(user_id=user.id, status="approved")
    db.add(dev_account)
    await db.flush()

    client = OAuthClient(
        developer_account_id=dev_account.id,
        client_id="client_test_123",
        name="Test App",
    )
    db.add(client)
    await db.flush()

    token = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=event.id,
        scopes=["webhooks:manage"],
        access_token_hash="hash123",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(token)
    await db.flush()

    # 1. Create first webhook subscription
    req1 = WebhookCreate(
        target_url="https://client.example.com/webhook/",
        event_types=["booth.transcription.started"],
    )
    res1 = await create_webhook(req1, token=token, db=db)
    assert res1.id is not None
    assert res1.target_url == req1.target_url
    assert res1.event_types == ["booth.transcription.started"]
    assert res1.secret_key.startswith("whsec_")
    orig_id = res1.id
    orig_secret = res1.secret_key

    # 2. Call again with same target_url but updated event_types
    from fastapi import Response

    resp2 = Response()
    req2 = WebhookCreate(
        target_url="https://client.example.com/webhook/",
        event_types=["booth.transcription.started", "booth.transcription.stopped"],
    )
    res2 = await create_webhook(req2, response=resp2, token=token, db=db)
    assert resp2.status_code == 200
    assert res2.id == orig_id
    assert res2.secret_key == orig_secret
    assert set(res2.event_types) == {"booth.transcription.started", "booth.transcription.stopped"}

    # Verify only 1 row exists in DB
    all_subs = (await db.execute(select(WebhookSubscription))).scalars().all()
    assert len(all_subs) == 1

    # 3. Simulate legacy duplicate rows in DB (e.g. from prior bug)
    dup = WebhookSubscription(
        developer_account_id=dev_account.id,
        target_url="https://client.example.com/webhook/",
        event_types=["booth.transcription.started"],
        secret_key="whsec_duplicate",
        is_active=True,
    )
    db.add(dup)
    await db.flush()

    from portal.models import WebhookDelivery

    delivery = WebhookDelivery(
        subscription_id=dup.id,
        event_type="booth.transcription.started",
        payload={"test": "reassigned"},
    )
    db.add(delivery)
    await db.flush()
    delivery_id = delivery.id

    assert len((await db.execute(select(WebhookSubscription))).scalars().all()) == 2

    # Call create_webhook again — it should prune the duplicate and reassign pending deliveries!
    res3 = await create_webhook(req2, token=token, db=db)
    assert res3.id == orig_id
    subs_after_prune = (await db.execute(select(WebhookSubscription))).scalars().all()
    assert len(subs_after_prune) == 1
    assert subs_after_prune[0].id == orig_id

    # Verify delivery was preserved and reassigned to orig_id
    reassigned_del = (
        (await db.execute(select(WebhookDelivery).where(WebhookDelivery.id == delivery_id))).scalars().first()
    )
    assert reassigned_del is not None
    assert reassigned_del.subscription_id == orig_id


@pytest.mark.anyio
async def test_create_webhook_quota_limit(db, monkeypatch):
    from portal.config import settings
    from portal.models import DeveloperAccount, Event, OAuthClient, OAuthToken, User
    from portal.routers.webhooks import WebhookCreate, create_webhook

    monkeypatch.setattr("portal.routers.webhooks.validate_ssrf", lambda url: None)
    monkeypatch.setattr(settings, "max_webhook_subscriptions", 3)

    user = User(email="quota@example.com", display_name="Quota User", is_active=True)
    db.add(user)
    await db.flush()

    event = Event(slug="quota-event", display_name="Quota Event")
    db.add(event)
    await db.flush()

    dev_account = DeveloperAccount(user_id=user.id, status="approved")
    db.add(dev_account)
    await db.flush()

    client = OAuthClient(
        developer_account_id=dev_account.id,
        client_id="client_quota_123",
        name="Quota App",
    )
    db.add(client)
    await db.flush()

    token = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=event.id,
        scopes=["webhooks:manage"],
        access_token_hash="hash123",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(token)
    await db.flush()

    for i in range(3):
        await create_webhook(
            WebhookCreate(target_url=f"https://example.com/wh/{i}", event_types=["test"]),
            token=token,
            db=db,
        )

    # Calling with a 4th distinct target URL should raise 429
    with pytest.raises(HTTPException) as exc_info:
        await create_webhook(
            WebhookCreate(target_url="https://example.com/wh/4", event_types=["test"]),
            token=token,
            db=db,
        )
    assert exc_info.value.status_code == 429

    # But re-subscribing to an EXISTING URL should still succeed (idempotent)!
    res_existing = await create_webhook(
        WebhookCreate(target_url="https://example.com/wh/1", event_types=["test", "extra"]),
        token=token,
        db=db,
    )
    assert res_existing.target_url == "https://example.com/wh/1"

    # Deactivating an existing subscription and creating a new one works within quota
    sub_row = (
        (
            await db.execute(
                select(WebhookSubscription).where(WebhookSubscription.target_url == "https://example.com/wh/1")
            )
        )
        .scalars()
        .first()
    )
    sub_row.is_active = False
    await db.flush()

    # Now create another sub to reach the quota of 3 active subs again
    await create_webhook(
        WebhookCreate(target_url="https://example.com/wh/filler", event_types=["test"]),
        token=token,
        db=db,
    )

    # Trying to reactivate the inactive sub while active quota is full raises 429
    with pytest.raises(HTTPException) as exc_info:
        await create_webhook(
            WebhookCreate(target_url="https://example.com/wh/1", event_types=["test"]),
            token=token,
            db=db,
        )
    assert exc_info.value.status_code == 429

    # But if one of the active subscriptions is a duplicate of the same URL,
    # reactivating the canonical sub will prune the duplicate, keeping total active count within quota.
    # Add an active duplicate of https://example.com/wh/1:
    dup_sub = WebhookSubscription(
        developer_account_id=client.developer_account_id,
        target_url="https://example.com/wh/1",
        event_types=["test"],
        secret_key="whsec_duplicate",
        is_active=True,
    )
    db.add(dup_sub)
    await db.flush()

    # Now active subs = 4 (wh/2, wh/3, wh/filler, wh/1-dup).
    # Deactivate wh/filler so active subs = 3 (quota limit of 3 is reached).
    filler_sub = (
        (
            await db.execute(
                select(WebhookSubscription).where(WebhookSubscription.target_url == "https://example.com/wh/filler")
            )
        )
        .scalars()
        .first()
    )
    filler_sub.is_active = False
    await db.flush()

    # Total active subs is 3 (wh/2, wh/3, and wh/1-dup).
    # Calling create_webhook for wh/1 should SUCCEED because pruning wh/1-dup keeps projected count at 3.
    res_reactivated = await create_webhook(
        WebhookCreate(target_url="https://example.com/wh/1", event_types=["test"]),
        token=token,
        db=db,
    )
    assert res_reactivated.target_url == "https://example.com/wh/1"
    assert res_reactivated.is_active is True
