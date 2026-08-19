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
        validate_ssrf("https://127.0.0.1:8000")
    assert exc.value.status_code == 400

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
        is_active=True
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="session.status_changed",
        payload={"is_active": True},
        status="delivering"
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
        is_active=True
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="session.status_changed",
        payload={"is_active": True},
        status="delivering"
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
        consecutive_failures=4 # 1 more to disable
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="session.status_changed",
        payload={"is_active": True},
        status="delivering",
        attempt_count=3 # Next attempt will be 4th (max)
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
    assert sub.is_active is False # Circuit broken

    # Audit log should be created
    audit_result = await db.execute(select(OAuthAuditLog))
    audits = audit_result.scalars().all()
    assert len(audits) == 1
    assert audits[0].action == "webhook.circuit_breaker_tripped"

