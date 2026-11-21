from __future__ import annotations

import ast
import importlib
import ipaddress
import json
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import portal.webhooks.events as wh_events
import portal.webhooks.worker as wh_worker
from fastapi_app import app
from portal.auth import require_oauth_scope
from portal.config import settings
from portal.models import (
    Base,
    DeveloperAccount,
    Event,
    OAuthAuditLog,
    OAuthClient,
    OAuthToken,
    User,
    WebhookDelivery,
    WebhookSubscription,
)
from portal.routers.webhooks import (
    WebhookCreate,
    create_webhook,
    list_available_events,
    list_webhooks,
    reconcile_subscriptions,
    require_webhooks_manage,
    validate_ssrf,
)
from portal.webhooks.events import (
    ALL_WEBHOOK_EVENTS,
    BOOTH_INTERPRETER_JOINED,
    BOOTH_TRANSCRIPTION_STARTED,
    BOOTH_TRANSCRIPTION_STOPPED,
    SESSION_STATUS_CHANGED,
    SUPPORTED_EVENT_TYPES,
    validate_event_types,
)
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


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """Replace DNS with a hostname -> IP list mapping; IP literals resolve to themselves."""
    records = {"example.com": ["93.184.216.34"], "webhook.site": ["178.63.67.106"]}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        try:
            ips = [str(ipaddress.ip_address(host))]
        except ValueError:
            if host not in records:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            ips = records[host]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return records


@pytest.mark.anyio
async def test_validate_ssrf():
    # Valid HTTPS external URL
    assert await validate_ssrf("https://webhook.site/123") == ["178.63.67.106"]

    # NAT64 is checked by the IPv4 address it embeds
    assert await validate_ssrf("https://[64:ff9b::5db8:d822]/hook") == ["64:ff9b::5db8:d822"]

    # HTTP is rejected (must use HTTPS)
    with pytest.raises(HTTPException) as exc:
        await validate_ssrf("http://example.com/api/webhooks")
    assert exc.value.status_code == 400

    # Internal IPs (should fail)

    with pytest.raises(HTTPException) as exc:
        await validate_ssrf("https://192.168.1.5")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        await validate_ssrf("https://10.0.0.1")
    assert exc.value.status_code == 400

    # Invalid URL structures
    with pytest.raises(HTTPException) as exc:
        await validate_ssrf("not_a_url")
    assert exc.value.status_code == 400

    with pytest.raises(HTTPException) as exc:
        await validate_ssrf("ftp://example.com")
    assert exc.value.status_code == 400

    # Inputs urllib and httpx parse differently must be rejected, not delivered
    for url in (
        " https://example.com/hook",
        "https://0135.0270.0330.042/hook",
        "https://example.com:99999/hook",
        "https://example.com:x/hook",
    ):
        with pytest.raises(HTTPException) as exc:
            await validate_ssrf(url)
        assert exc.value.status_code == 400


@pytest.mark.anyio
@pytest.mark.parametrize(
    "ips",
    [
        ["::1"],
        ["fd00::1"],
        ["fe80::1"],
        ["93.184.216.34", "10.0.0.5"],
        ["93.184.216.34", "::1"],
        ["0.0.0.0"],
        ["::"],
        ["::ffff:127.0.0.1"],
        ["::127.0.0.1"],
        ["::10.0.0.1"],
        ["::169.254.169.254"],
        ["::93.184.216.34"],
        ["64:ff9b::a00:1"],
        ["2002:a00:1::1"],
        ["240.0.0.1"],
        ["100.64.0.1"],
        ["224.0.0.1"],
    ],
)
async def test_validate_ssrf_rejects_any_internal_record(dns, ips):
    dns["hooks.attacker.test"] = ips
    with pytest.raises(HTTPException) as exc:
        await validate_ssrf("https://hooks.attacker.test/hook")
    assert exc.value.status_code == 400
    assert "forbidden internal IP" in exc.value.detail


@pytest.mark.anyio
async def test_validate_ssrf_rejects_unresolvable_host():
    with pytest.raises(HTTPException) as exc:
        await validate_ssrf("https://missing.attacker.test/hook")
    assert exc.value.status_code == 400


@pytest.mark.anyio
async def test_validate_ssrf_development_bypass_matches_exact_hostname(dns, monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    dns["localhost.attacker.com"] = ["127.0.0.1"]

    assert await validate_ssrf("http://localhost:8000/hook") is None
    assert await validate_ssrf("https://127.0.0.1/hook") is None
    assert await validate_ssrf("http://[::1]:8000/hook") is None

    for url in ("https://localhost.attacker.com/hook", "https://10.0.0.1/?next=localhost"):
        with pytest.raises(HTTPException):
            await validate_ssrf(url)


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
    assert str(args[0]) == "https://93.184.216.34/webhook"
    assert kwargs["headers"]["Host"] == "example.com"
    assert kwargs["extensions"] == {"sni_hostname": "example.com"}

    # Signature header embeds the timestamp: "t=<epoch>,v1=<hmac>"
    sig_header = kwargs["headers"]["X-VoxBento-Signature"]
    assert sig_header.startswith("t="), f"Expected signature to start with 't=', got: {sig_header}"
    assert ",v1=" in sig_header, f"Expected ',v1=' in signature, got: {sig_header}"

    posted_payload = json.loads(kwargs["content"])
    assert posted_payload["delivery_id"] == delivery.id
    assert posted_payload["event_type"] == "session.status_changed"
    assert posted_payload["payload_version"] == "1"
    assert posted_payload["data"] == {"is_active": True}


async def _make_delivery(db, target_url: str) -> WebhookDelivery:
    sub = WebhookSubscription(
        developer_account_id=1,
        target_url=target_url,
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
    return delivery


@pytest.mark.anyio
@pytest.mark.parametrize("dns_errno", [socket.EAI_AGAIN, socket.EAI_NONAME])
@pytest.mark.parametrize("recovered_ip, expected_status", [("93.184.216.34", "succeeded"), ("127.0.0.1", "dead")])
async def test_process_delivery_revalidates_after_dns_failure(
    db, dns, monkeypatch, dns_errno, recovered_ip, expected_status
):
    delivery = await _make_delivery(db, "https://recovery.example.test/hook")
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.return_value = MagicMock()
    before = datetime.now(timezone.utc)

    with monkeypatch.context() as resolver:
        resolver.setattr(
            socket, "getaddrinfo", MagicMock(side_effect=socket.gaierror(dns_errno, "resolver unavailable"))
        )
        await process_delivery(mock_client, db, delivery)

    assert delivery.status == "failed"
    assert delivery.attempt_count == 1
    assert (
        before + timedelta(seconds=2) <= delivery.next_attempt_at <= datetime.now(timezone.utc) + timedelta(seconds=2)
    )
    assert "resolver unavailable" in delivery.last_error
    mock_client.post.assert_not_called()

    dns["recovery.example.test"] = [recovered_ip]
    await process_delivery(mock_client, db, delivery)

    assert delivery.status == expected_status
    if expected_status == "succeeded":
        assert delivery.attempt_count == 2
        assert delivery.last_error is None
        assert str(mock_client.post.call_args.args[0]) == "https://93.184.216.34/hook"
    else:
        assert "forbidden internal IP" in delivery.last_error
        mock_client.post.assert_not_called()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "attempt_count, expected_status, backoff", [(1, "failed", 4), (2, "failed", 8), (3, "dead", None)]
)
async def test_process_delivery_dns_failures_use_retry_limit(db, monkeypatch, attempt_count, expected_status, backoff):
    delivery = await _make_delivery(db, "https://outage.example.test/hook")
    delivery.attempt_count = attempt_count
    sub = await db.get(WebhookSubscription, delivery.subscription_id)
    sub.consecutive_failures = 4
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    monkeypatch.setattr(
        socket, "getaddrinfo", MagicMock(side_effect=socket.gaierror(socket.EAI_AGAIN, "resolver unavailable"))
    )
    before = datetime.now(timezone.utc)

    await process_delivery(mock_client, db, delivery)

    assert delivery.status == expected_status
    assert delivery.attempt_count == attempt_count + 1
    mock_client.post.assert_not_called()
    if backoff is not None:
        assert (
            before + timedelta(seconds=backoff)
            <= delivery.next_attempt_at
            <= datetime.now(timezone.utc) + timedelta(seconds=backoff)
        )
        assert sub.consecutive_failures == 4
        assert sub.is_active
    else:
        assert sub.consecutive_failures == 5
        assert not sub.is_active


@pytest.mark.anyio
@pytest.mark.parametrize("rebound_ip", ["127.0.0.1", "::7f00:1", "::a00:1"])
async def test_process_delivery_rejects_rebound_dns(db, dns, rebound_ip):
    dns["rebind.attacker.test"] = ["93.184.216.34"]
    await validate_ssrf("https://rebind.attacker.test:9443/hook")
    delivery = await _make_delivery(db, "https://rebind.attacker.test:9443/hook")

    dns["rebind.attacker.test"] = [rebound_ip]
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    await process_delivery(mock_client, db, delivery)

    mock_client.post.assert_not_called()
    assert delivery.status == "dead"
    assert rebound_ip in delivery.last_error


@pytest.mark.anyio
@pytest.mark.parametrize("connect_failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_process_delivery_keeps_port_and_tries_next_vetted_ip(db, dns, connect_failure):
    dns["dual.example.org"] = ["2606:2800:220:1::1", "2606:2800:220:1::2", "93.184.216.34"]
    delivery = await _make_delivery(db, "https://dual.example.org:8443/hook?a=1")

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = [connect_failure("IPv6 unreachable"), MagicMock()]
    await process_delivery(mock_client, db, delivery)

    assert delivery.status == "succeeded"
    urls = [str(call.args[0]) for call in mock_client.post.call_args_list]
    assert urls == ["https://[2606:2800:220:1::1]:8443/hook?a=1", "https://93.184.216.34:8443/hook?a=1"]
    assert mock_client.post.call_args.kwargs["headers"]["Host"] == "dual.example.org:8443"


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
    monkeypatch.setattr("portal.routers.webhooks.validate_ssrf", AsyncMock(return_value=None))

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
    monkeypatch.setattr("portal.routers.webhooks.validate_ssrf", AsyncMock(return_value=None))
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
            WebhookCreate(target_url=f"https://example.com/wh/{i}", event_types=["session.status_changed"]),
            token=token,
            db=db,
        )

    # Calling with a 4th distinct target URL should raise 429
    with pytest.raises(HTTPException) as exc_info:
        await create_webhook(
            WebhookCreate(target_url="https://example.com/wh/4", event_types=["session.status_changed"]),
            token=token,
            db=db,
        )
    assert exc_info.value.status_code == 429

    # But re-subscribing to an EXISTING URL should still succeed (idempotent)!
    res_existing = await create_webhook(
        WebhookCreate(
            target_url="https://example.com/wh/1", event_types=["session.status_changed", "booth.transcription.started"]
        ),
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
        WebhookCreate(target_url="https://example.com/wh/filler", event_types=["session.status_changed"]),
        token=token,
        db=db,
    )

    # Trying to reactivate the inactive sub while active quota is full raises 429
    with pytest.raises(HTTPException) as exc_info:
        await create_webhook(
            WebhookCreate(target_url="https://example.com/wh/1", event_types=["session.status_changed"]),
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
        event_types=["session.status_changed"],
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
        WebhookCreate(target_url="https://example.com/wh/1", event_types=["session.status_changed"]),
        token=token,
        db=db,
    )
    assert res_reactivated.target_url == "https://example.com/wh/1"
    assert res_reactivated.is_active is True


@pytest.mark.anyio
async def test_process_delivery_handles_failed_refresh_gracefully(db, monkeypatch):
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


# ---------------------------------------------------------------------------
# Tests for Webhook Validation, Event Types Registry, and Enqueueing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_webhook_rejects_empty_event_types(db, monkeypatch):
    """Verify that creating a webhook with an empty list of event types is rejected with 400."""
    monkeypatch.setattr("portal.routers.webhooks.validate_ssrf", AsyncMock(return_value=None))

    user = User(email="empty@example.com", display_name="User", is_active=True)
    db.add(user)
    await db.flush()

    event = Event(slug="ev-empty", display_name="Event")
    db.add(event)
    await db.flush()

    dev_account = DeveloperAccount(user_id=user.id, status="approved")
    db.add(dev_account)
    await db.flush()

    client = OAuthClient(developer_account_id=dev_account.id, client_id="client_empty", name="App")
    db.add(client)
    await db.flush()

    token = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=event.id,
        scopes=["webhooks:manage"],
        access_token_hash="hash_empty",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(token)
    await db.flush()

    with pytest.raises(HTTPException) as exc_info:
        await create_webhook(
            WebhookCreate(target_url="https://example.com/wh", event_types=[]),
            token=token,
            db=db,
        )
    assert exc_info.value.status_code == 400
    assert "event_types cannot be empty" in exc_info.value.detail


@pytest.mark.anyio
async def test_create_webhook_rejects_unknown_event_types(db, monkeypatch):
    """Verify that creating a webhook with unknown event types is rejected with 400 and clear detail."""
    monkeypatch.setattr("portal.routers.webhooks.validate_ssrf", AsyncMock(return_value=None))

    user = User(email="unknown@example.com", display_name="User", is_active=True)
    db.add(user)
    await db.flush()

    event = Event(slug="ev-unknown", display_name="Event")
    db.add(event)
    await db.flush()

    dev_account = DeveloperAccount(user_id=user.id, status="approved")
    db.add(dev_account)
    await db.flush()

    client = OAuthClient(developer_account_id=dev_account.id, client_id="client_unknown", name="App")
    db.add(client)
    await db.flush()

    token = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=event.id,
        scopes=["webhooks:manage"],
        access_token_hash="hash_unknown",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(token)
    await db.flush()

    # Completely unknown event
    with pytest.raises(HTTPException) as exc_info:
        await create_webhook(
            WebhookCreate(target_url="https://example.com/wh", event_types=["bogus.event"]),
            token=token,
            db=db,
        )
    assert exc_info.value.status_code == 400
    assert "Unsupported event types: bogus.event" in exc_info.value.detail
    assert "Supported event types are:" in exc_info.value.detail

    # Mix of valid and invalid
    with pytest.raises(HTTPException) as exc_info:
        await create_webhook(
            WebhookCreate(
                target_url="https://example.com/wh",
                event_types=["session.status_changed", "typo.interpreter.join"],
            ),
            token=token,
            db=db,
        )
    assert exc_info.value.status_code == 400
    assert "Unsupported event types: typo.interpreter.join" in exc_info.value.detail


@pytest.mark.anyio
async def test_create_webhook_accepts_all_supported_event_types(db, monkeypatch):
    """Verify that all supported event types are accepted individually and together."""
    monkeypatch.setattr("portal.routers.webhooks.validate_ssrf", AsyncMock(return_value=None))

    user = User(email="all@example.com", display_name="User", is_active=True)
    db.add(user)
    await db.flush()

    event = Event(slug="ev-all", display_name="Event")
    db.add(event)
    await db.flush()

    dev_account = DeveloperAccount(user_id=user.id, status="approved")
    db.add(dev_account)
    await db.flush()

    client = OAuthClient(developer_account_id=dev_account.id, client_id="client_all", name="App")
    db.add(client)
    await db.flush()

    token = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=event.id,
        scopes=["webhooks:manage"],
        access_token_hash="hash_all",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(token)
    await db.flush()

    # Each individual event type must be accepted
    for evt in sorted(SUPPORTED_EVENT_TYPES):
        res = await create_webhook(
            WebhookCreate(target_url=f"https://example.com/wh/{evt}", event_types=[evt]),
            token=token,
            db=db,
        )
        assert res.event_types == [evt]

    # All together must be accepted
    res_all = await create_webhook(
        WebhookCreate(target_url="https://example.com/wh/all", event_types=list(SUPPORTED_EVENT_TYPES)),
        token=token,
        db=db,
    )
    assert set(res_all.event_types) == SUPPORTED_EVENT_TYPES


def test_list_available_events():
    """Verify that GET /api/v1/webhooks/events requires auth and returns all supported event types."""
    client = TestClient(app)
    unauth_resp = client.get("/api/v1/webhooks/events")
    assert unauth_resp.status_code == 401

    mock_token = OAuthToken(scopes=["webhooks:manage"])
    app.dependency_overrides[require_webhooks_manage] = lambda: mock_token
    try:
        resp = client.get("/api/v1/webhooks/events")
        assert resp.status_code == 200
        assert resp.json() == sorted(SUPPORTED_EVENT_TYPES)
    finally:
        app.dependency_overrides.pop(require_webhooks_manage, None)


@pytest.mark.anyio
async def test_enqueue_webhook_queues_delivery_only_for_matching_subscription(db, monkeypatch):
    """Verify that deliveries are only queued for active subscriptions matching the emitted event."""
    # Sub 1: Active, subscribed to SESSION_STATUS_CHANGED
    sub1 = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook1",
        event_types=[SESSION_STATUS_CHANGED],
        secret_key="whsec_1",
        is_active=True,
    )
    # Sub 2: Active, subscribed to BOOTH_INTERPRETER_JOINED
    sub2 = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook2",
        event_types=[BOOTH_INTERPRETER_JOINED],
        secret_key="whsec_2",
        is_active=True,
    )
    # Sub 3: Inactive, subscribed to SESSION_STATUS_CHANGED
    sub3 = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/webhook3",
        event_types=[SESSION_STATUS_CHANGED],
        secret_key="whsec_3",
        is_active=False,
    )
    db.add_all([sub1, sub2, sub3])
    await db.flush()

    # Mock get_session in worker to return the test's db session
    @asynccontextmanager
    async def _mock_get_session():
        yield db

    monkeypatch.setattr("portal.webhooks.worker.get_session", _mock_get_session)
    monkeypatch.setattr(wh_worker, "enqueue_webhook", wh_worker._real_enqueue_webhook)

    payload = {"booth_id": "test-booth", "is_active": True}
    await wh_worker.enqueue_webhook(SESSION_STATUS_CHANGED, payload)

    delivs = (await db.execute(select(WebhookDelivery))).scalars().all()
    assert len(delivs) == 1
    d = delivs[0]
    assert d.subscription_id == sub1.id
    assert d.event_type == SESSION_STATUS_CHANGED
    assert d.payload == payload
    assert d.status == "pending"


@pytest.mark.anyio
async def test_enqueue_webhook_rejects_unsupported_event(monkeypatch):
    """Verify that enqueueing an unsupported event raises ValueError."""
    monkeypatch.setattr(wh_worker, "enqueue_webhook", wh_worker._real_enqueue_webhook)

    with pytest.raises(ValueError) as exc_info:
        await wh_worker.enqueue_webhook("unsupported.event", {"test": True})
    assert "Unknown webhook event type: 'unsupported.event'" in str(exc_info.value)


@pytest.mark.anyio
async def test_reconcile_subscriptions_enforces_tenant_isolation(db):
    """Verify reconcile_subscriptions strictly scopes to developer_account_id and rejects None."""
    # Sub for account 1
    sub1 = WebhookSubscription(
        developer_account_id=1,
        target_url="https://example.com/acct1",
        event_types=["unsupported.evt", SESSION_STATUS_CHANGED],
        secret_key="sec1",
        is_active=True,
    )
    # Sub for account 2
    sub2 = WebhookSubscription(
        developer_account_id=2,
        target_url="https://example.com/acct2",
        event_types=["unsupported.evt2"],
        secret_key="sec2",
        is_active=True,
    )
    db.add_all([sub1, sub2])
    await db.flush()

    # Reconciling with developer_account_id=None must return empty list and touch nothing
    res_none = await reconcile_subscriptions(db, None)
    assert res_none == []

    # Verify neither sub was modified
    db_sub2 = (await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == sub2.id))).scalars().first()
    assert db_sub2.is_active is True
    assert db_sub2.event_types == ["unsupported.evt2"]

    # Reconciling account 1 only touches account 1
    res_acct1 = await reconcile_subscriptions(db, 1)
    assert len(res_acct1) == 1
    assert res_acct1[0].id == sub1.id
    assert res_acct1[0].event_types == [SESSION_STATUS_CHANGED]

    # Sub 2 is still untouched
    db_sub2_after = (
        (await db.execute(select(WebhookSubscription).where(WebhookSubscription.id == sub2.id))).scalars().first()
    )
    assert db_sub2_after.is_active is True
    assert db_sub2_after.event_types == ["unsupported.evt2"]


@pytest.mark.anyio
async def test_reconcile_preexisting_subscriptions_frees_quota(db, monkeypatch):
    """Verify that pre-existing subscriptions with unsupported events are reconciled and free quota."""
    monkeypatch.setattr("portal.routers.webhooks.validate_ssrf", AsyncMock(return_value=None))

    user = User(email="reconcile@example.com", display_name="User", is_active=True)
    db.add(user)
    await db.flush()

    event = Event(slug="ev-reconcile", display_name="Event")
    db.add(event)
    await db.flush()

    dev_account = DeveloperAccount(user_id=user.id, status="approved")
    db.add(dev_account)
    await db.flush()

    client = OAuthClient(developer_account_id=dev_account.id, client_id="client_reconcile", name="App")
    db.add(client)
    await db.flush()

    token = OAuthToken(
        client_id=client.id,
        user_id=user.id,
        event_id=event.id,
        scopes=["webhooks:manage"],
        access_token_hash="hash_reconcile",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.add(token)
    await db.flush()

    # Create pre-existing active subscriptions directly in db:
    # sub1 has ONLY unsupported event (should become inactive and free quota)
    sub1 = WebhookSubscription(
        developer_account_id=dev_account.id,
        target_url="https://example.com/legacy1",
        event_types=["unsupported.legacy.event"],
        secret_key="sec1",
        is_active=True,
    )
    # sub2 has mixed events (supported + unsupported)
    sub2 = WebhookSubscription(
        developer_account_id=dev_account.id,
        target_url="https://example.com/legacy2",
        event_types=[SESSION_STATUS_CHANGED, "old.deprecated.event"],
        secret_key="sec2",
        is_active=True,
    )
    db.add_all([sub1, sub2])
    await db.flush()

    monkeypatch.setattr(settings, "max_webhook_subscriptions", 2)

    # Calling list_webhooks reconciles subscriptions
    listed = await list_webhooks(token=token, db=db)
    sub1_listed = next(s for s in listed if s.target_url == "https://example.com/legacy1")
    sub2_listed = next(s for s in listed if s.target_url == "https://example.com/legacy2")

    # sub1 should be deactivated and have empty event_types
    assert sub1_listed.is_active is False
    assert sub1_listed.event_types == []

    # sub2 should remain active with unsupported event pruned
    assert sub2_listed.is_active is True
    assert sub2_listed.event_types == [SESSION_STATUS_CHANGED]

    # Creating a new subscription now succeeds because sub1 is inactive and doesn't consume quota
    new_sub = await create_webhook(
        WebhookCreate(target_url="https://example.com/new", event_types=[SESSION_STATUS_CHANGED]),
        token=token,
        db=db,
    )
    assert new_sub.is_active is True
    assert new_sub.target_url == "https://example.com/new"


def test_event_registry_single_source_of_truth():
    """Verify that event constants match values and AST checks prove callers use the registry."""
    # Constants must match their expected string representations
    assert SESSION_STATUS_CHANGED == "session.status_changed"
    assert BOOTH_TRANSCRIPTION_STARTED == "booth.transcription.started"
    assert BOOTH_TRANSCRIPTION_STOPPED == "booth.transcription.stopped"
    assert BOOTH_INTERPRETER_JOINED == "booth.interpreter.joined"

    # Set and tuple consistency
    assert set(ALL_WEBHOOK_EVENTS) == SUPPORTED_EVENT_TYPES
    assert len(ALL_WEBHOOK_EVENTS) == 4

    # validate_event_types helper sorts and deduplicates
    res = validate_event_types([SESSION_STATUS_CHANGED, SESSION_STATUS_CHANGED])
    assert res == [SESSION_STATUS_CHANGED]

    with pytest.raises(ValueError, match="event_types cannot be empty"):
        validate_event_types([])

    with pytest.raises(ValueError, match="Unsupported event types"):
        validate_event_types(["invalid"])

    # Verify runtime producer import bindings evaluate to valid supported constants
    producer_bindings = [
        ("portal.booth_state", {"SESSION_STATUS_CHANGED": SESSION_STATUS_CHANGED}),
        (
            "portal.transcription.worker",
            {
                "BOOTH_TRANSCRIPTION_STARTED": BOOTH_TRANSCRIPTION_STARTED,
                "BOOTH_TRANSCRIPTION_STOPPED": BOOTH_TRANSCRIPTION_STOPPED,
            },
        ),
        ("portal.websockets.manager", {"BOOTH_INTERPRETER_JOINED": BOOTH_INTERPRETER_JOINED}),
    ]
    for module_name, bindings in producer_bindings:
        mod = importlib.import_module(module_name)
        for attr_name, expected_val in bindings.items():
            actual_val = getattr(mod, attr_name, None)
            assert actual_val == expected_val, (
                f"{module_name}.{attr_name} evaluated to {actual_val!r}, expected {expected_val!r}"
            )
            assert actual_val in SUPPORTED_EVENT_TYPES, (
                f"{module_name}.{attr_name} value {actual_val!r} is not in SUPPORTED_EVENT_TYPES"
            )

    # AST verification of all calls to enqueue_webhook across portal/
    portal_dir = Path(__file__).resolve().parents[1] / "portal"
    for py_file in portal_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))

        # Track event constant bindings imported from portal.webhooks.events in this file
        imported_event_bindings: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "portal.webhooks.events":
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    imported_event_bindings[local_name] = getattr(wh_events, alias.name, None)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                func_name = getattr(func, "id", None) or getattr(func, "attr", None)
                if func_name == "enqueue_webhook":
                    event_arg = None
                    if node.args:
                        event_arg = node.args[0]
                    else:
                        for kw in node.keywords:
                            if kw.arg == "event_type":
                                event_arg = kw.value
                                break

                    if event_arg is not None:
                        if isinstance(event_arg, ast.Constant):
                            assert event_arg.value in SUPPORTED_EVENT_TYPES, (
                                f"{py_file}: String literal {event_arg.value!r} passed to enqueue_webhook "
                                f"is not in SUPPORTED_EVENT_TYPES"
                            )
                        elif isinstance(event_arg, ast.Name):
                            assert event_arg.id in {
                                "SESSION_STATUS_CHANGED",
                                "BOOTH_TRANSCRIPTION_STARTED",
                                "BOOTH_TRANSCRIPTION_STOPPED",
                                "BOOTH_INTERPRETER_JOINED",
                                "event_type",  # Inside worker.py itself
                            }, f"{py_file}: Unknown identifier {event_arg.id} passed to enqueue_webhook"

                            # Confirm identifier was imported from portal.webhooks.events and evaluates to supported event
                            if event_arg.id != "event_type":
                                assert event_arg.id in imported_event_bindings, (
                                    f"{py_file}: Identifier {event_arg.id} passed to enqueue_webhook "
                                    f"was not imported from portal.webhooks.events"
                                )
                                bound_val = imported_event_bindings[event_arg.id]
                                assert bound_val in SUPPORTED_EVENT_TYPES, (
                                    f"{py_file}: Identifier {event_arg.id} binds to {bound_val!r}, "
                                    f"which is not in SUPPORTED_EVENT_TYPES"
                                )


@pytest.mark.anyio
async def test_webhook_debug_dump_endpoint_removed():
    """Ensure the unauthenticated debug dump endpoint is not present or accessible."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Unauthenticated request
        response = await client.get("/api/v1/webhooks/debug/dump")
        assert response.status_code == 404

        # Request with arbitrary Bearer token
        response_with_auth = await client.get(
            "/api/v1/webhooks/debug/dump",
            headers={"Authorization": "Bearer test_token"},
        )
        assert response_with_auth.status_code == 404
