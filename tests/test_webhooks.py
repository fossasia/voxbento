from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portal.models import Base, OAuthAuditLog, WebhookDelivery, WebhookSubscription
from portal.routers.webhooks import validate_ssrf
from portal.webhooks.worker import process_delivery, sign_payload

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
async def test_process_delivery_success(db):
    # Setup Data
    sub = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook",
        event_types=["session.status_changed"],
        secret_key="secret",
        is_active=True,
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id, event_type="session.status_changed", payload={"is_active": True}, status="delivering"
    )
    db.add(delivery)
    await db.flush()

    # Mock HTTP Client
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_response

    # Process
    await process_delivery(mock_client, db, delivery)

    # Assertions
    assert delivery.status == "succeeded"
    assert delivery.attempt_count == 1
    assert delivery.last_error is None

    # Assert client was called correctly
    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert args[0] == "https://example.com/webhook"

    # Signature header embeds the timestamp: "t=<epoch>,v1=<hmac>"
    sig_header = kwargs["headers"]["X-VoxBento-Signature"]
    assert sig_header.startswith("t="), f"Expected signature to start with 't=', got: {sig_header}"
    assert ",v1=" in sig_header, f"Expected ',v1=' in signature, got: {sig_header}"

    posted_payload = json.loads(kwargs["content"])
    assert posted_payload["delivery_id"] == delivery.id
    assert posted_payload["event_type"] == "session.status_changed"
    assert posted_payload["payload_version"] == "1"
    assert posted_payload["data"] == {"is_active": True}


@pytest.mark.anyio
async def test_process_delivery_http_error_retry(db):
    sub = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook",
        event_types=["session.status_changed"],
        secret_key="secret",
        is_active=True,
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id, event_type="session.status_changed", payload={"is_active": True}, status="delivering"
    )
    db.add(delivery)
    await db.flush()

    # Mock HTTP Client throwing error
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client.post.side_effect = httpx.HTTPStatusError(
        "500 Server Error", request=MagicMock(), response=mock_response
    )

    # Process
    await process_delivery(mock_client, db, delivery)

    # Assertions (Failed, should retry)
    assert delivery.status == "failed"
    assert delivery.attempt_count == 1
    assert delivery.last_error == "HTTP error: 500"
    assert delivery.next_attempt_at is not None
    # Backoff is 2^1 = 2 seconds
    assert delivery.next_attempt_at > datetime.now(timezone.utc)


@pytest.mark.anyio
async def test_process_delivery_circuit_breaker(db):
    sub = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook",
        event_types=["session.status_changed"],
        secret_key="secret",
        is_active=True,
        consecutive_failures=4,  # 1 more to disable
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="session.status_changed",
        payload={"is_active": True},
        status="delivering",
        attempt_count=3,  # Next attempt will be 4th (max)
    )
    db.add(delivery)
    await db.flush()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = httpx.RequestError("Connection timeout")

    # Process
    await process_delivery(mock_client, db, delivery)

    # Assertions (Dead, Circuit Breaker)
    assert delivery.status == "dead"
    assert delivery.attempt_count == 4

    assert sub.consecutive_failures == 5
    assert sub.is_active is False  # Circuit broken

    # Audit log should be created
    audit_result = await db.execute(select(OAuthAuditLog))
    audits = audit_result.scalars().all()
    assert len(audits) == 1
    assert audits[0].action == "webhook.circuit_breaker_tripped"
    assert audits[0].client_id is None  # Zero-client case coverage


@pytest.mark.anyio
async def test_process_delivery_circuit_breaker_with_multiple_clients(db):
    from portal.models import OAuthClient

    client1 = OAuthClient(developer_account_id=1, client_id="client_1", name="C1")
    client2 = OAuthClient(developer_account_id=1, client_id="client_2", name="C2")
    db.add_all([client1, client2])
    await db.flush()

    sub = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook",
        event_types=["session.status_changed"],
        secret_key="secret",
        is_active=True,
        consecutive_failures=4,
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="session.status_changed",
        payload={"is_active": True},
        status="delivering",
        attempt_count=3,
    )
    db.add(delivery)
    await db.flush()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = httpx.RequestError("Connection timeout")

    await process_delivery(mock_client, db, delivery)

    audit_result = await db.execute(select(OAuthAuditLog))
    audits = audit_result.scalars().all()
    assert len(audits) == 1
    assert audits[0].action == "webhook.circuit_breaker_tripped"
    assert audits[0].client_id == client1.id  # Verifies order_by(OAuthClient.id) selects the oldest client


@pytest.mark.anyio
async def test_atomic_queue_claim(db):
    """Test that multiple workers do not claim the same webhook delivery."""
    sub = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook",
        event_types=["session.status_changed"],
        secret_key="secret",
        is_active=True,
    )
    db.add(sub)
    await db.flush()

    # Create 5 pending deliveries
    for i in range(5):
        delivery = WebhookDelivery(
            subscription_id=sub.id, event_type="session.status_changed", payload={"id": i}, status="pending"
        )
        db.add(delivery)
    await db.flush()

    now = datetime.now(timezone.utc)
    from sqlalchemy import select, update

    # Simulate worker 1 claiming 3 items
    subquery1 = (
        select(WebhookDelivery.id)
        .where(WebhookDelivery.status.in_(["pending", "failed"]))
        .where(WebhookDelivery.next_attempt_at <= now)
        .limit(3)
    )
    stmt1 = (
        update(WebhookDelivery)
        .where(WebhookDelivery.id.in_(subquery1))
        .values(status="delivering")
        .returning(WebhookDelivery)
    )
    result1 = await db.execute(stmt1)
    claimed1 = result1.scalars().all()

    # Simulate worker 2 claiming 3 items at the exact same time
    subquery2 = (
        select(WebhookDelivery.id)
        .where(WebhookDelivery.status.in_(["pending", "failed"]))
        .where(WebhookDelivery.next_attempt_at <= now)
        .limit(3)
    )
    stmt2 = (
        update(WebhookDelivery)
        .where(WebhookDelivery.id.in_(subquery2))
        .values(status="delivering")
        .returning(WebhookDelivery)
    )
    result2 = await db.execute(stmt2)
    claimed2 = result2.scalars().all()

    # Worker 1 should get 3, Worker 2 should get the remaining 2
    assert len(claimed1) == 3
    assert len(claimed2) == 2

    # No overlap in claimed IDs
    ids1 = {d.id for d in claimed1}
    ids2 = {d.id for d in claimed2}
    assert ids1.isdisjoint(ids2)


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


@pytest.mark.anyio
async def test_process_delivery_handles_failed_refresh_gracefully(db, monkeypatch):
    from unittest.mock import AsyncMock

    from sqlalchemy.exc import SQLAlchemyError

    from portal.webhooks.worker import process_delivery

    sub = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook",
        event_types=["session.status_changed"],
        secret_key="secret",
        is_active=True,
        consecutive_failures=2,
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="session.status_changed",
        payload={"is_active": True},
        status="pending",
        attempt_count=0,
    )
    db.add(delivery)
    await db.flush()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock(return_value=None)
    mock_client.post.return_value = mock_resp

    async def mock_refresh(instance):
        raise SQLAlchemyError("Simulated database refresh failure")

    monkeypatch.setattr(db, "refresh", mock_refresh)

    # Must complete successfully without raising exception on sub access
    await process_delivery(mock_client, db, delivery)
    assert delivery.status == "succeeded"
    assert delivery.last_error is None
