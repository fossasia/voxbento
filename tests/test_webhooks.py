from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from portal.models import Base, OAuthAuditLog, WebhookDelivery, WebhookSubscription
import portal.webhooks.worker
from portal.routers.webhooks import _is_forbidden_ip, validate_ssrf
from portal.webhooks.worker import post_to_vetted_ip, process_delivery, sign_payload

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


EXTERNAL_IPV4 = "93.184.216.34"
EXTERNAL_IPV6 = "2606:2800:21f:cb07:6820:80da:af6b:8b2c"


def _make_getaddrinfo(*ips: str):
    """Return a coroutine that fakes asyncio loop.getaddrinfo results."""

    async def _fake(_host, _port, **_kw):
        results = []
        for ip in ips:
            family = socket.AF_INET6 if ":" in ip else socket.AF_INET
            results.append((family, socket.SOCK_STREAM, 6, "", (ip, 0)))
        return results

    return _fake


# ---------------------------------------------------------------------------
# Tests for _is_forbidden_ip
# ---------------------------------------------------------------------------


def test_forbidden_ip_private():
    assert _is_forbidden_ip(ipaddress.ip_address("10.0.0.1")) is True
    assert _is_forbidden_ip(ipaddress.ip_address("192.168.1.1")) is True
    assert _is_forbidden_ip(ipaddress.ip_address("172.16.0.1")) is True


def test_forbidden_ip_loopback():
    assert _is_forbidden_ip(ipaddress.ip_address("127.0.0.1")) is True
    assert _is_forbidden_ip(ipaddress.ip_address("::1")) is True


def test_forbidden_ip_link_local():
    assert _is_forbidden_ip(ipaddress.ip_address("169.254.1.1")) is True


def test_forbidden_ip_cloud_metadata():
    assert _is_forbidden_ip(ipaddress.ip_address("169.254.169.254")) is True
    assert _is_forbidden_ip(ipaddress.ip_address("fd00:ec2::254")) is True
    assert _is_forbidden_ip(ipaddress.ip_address("100.100.100.200")) is True


def test_forbidden_ip_ipv4_mapped_ipv6():
    mapped = ipaddress.ip_address("::ffff:10.0.0.1")
    assert _is_forbidden_ip(mapped) is True


def test_forbidden_ip_nat64():
    nat64 = ipaddress.ip_address("64:ff9b::10.0.0.1")
    assert _is_forbidden_ip(nat64) is True


def test_forbidden_ip_global_allowed():
    assert _is_forbidden_ip(ipaddress.ip_address(EXTERNAL_IPV4)) is False
    assert _is_forbidden_ip(ipaddress.ip_address(EXTERNAL_IPV6)) is False


# ---------------------------------------------------------------------------
# Tests for validate_ssrf (async)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_validate_ssrf_https_ok():
    loop_mock = MagicMock()
    loop_mock.getaddrinfo = _make_getaddrinfo(EXTERNAL_IPV4)
    with patch("asyncio.get_running_loop", return_value=loop_mock):
        result = await validate_ssrf("https://webhook.site/123")
    assert result == [EXTERNAL_IPV4]


@pytest.mark.anyio
async def test_validate_ssrf_http_rejected():
    with pytest.raises(HTTPException) as exc:
        await validate_ssrf("http://example.com/webhook")
    assert exc.value.status_code == 400


@pytest.mark.anyio
async def test_validate_ssrf_private_ip_rejected():
    loop_mock = MagicMock()
    loop_mock.getaddrinfo = _make_getaddrinfo("10.0.0.1")
    with patch("asyncio.get_running_loop", return_value=loop_mock):
        with pytest.raises(HTTPException) as exc:
            await validate_ssrf("https://evil.example.com")
    assert exc.value.status_code == 400
    assert "forbidden" in str(exc.value.detail).lower()


@pytest.mark.anyio
async def test_validate_ssrf_invalid_url():
    with pytest.raises(HTTPException):
        await validate_ssrf("not_a_url")


@pytest.mark.anyio
async def test_validate_ssrf_ftp_rejected():
    with pytest.raises(HTTPException):
        await validate_ssrf("ftp://example.com")


@pytest.mark.anyio
async def test_validate_ssrf_dns_rebinding_all_checked():
    """If any resolved IP is forbidden the whole URL is rejected."""
    loop_mock = MagicMock()
    loop_mock.getaddrinfo = _make_getaddrinfo(EXTERNAL_IPV4, "127.0.0.1")
    with patch("asyncio.get_running_loop", return_value=loop_mock):
        with pytest.raises(HTTPException) as exc:
            await validate_ssrf("https://rebind.example.com")
    assert exc.value.status_code == 400


@pytest.mark.anyio
async def test_validate_ssrf_dual_stack():
    """Both A and AAAA records are returned when all are safe."""
    loop_mock = MagicMock()
    loop_mock.getaddrinfo = _make_getaddrinfo(EXTERNAL_IPV4, EXTERNAL_IPV6)
    with patch("asyncio.get_running_loop", return_value=loop_mock):
        result = await validate_ssrf("https://dualstack.example.com")
    assert set(result) == {EXTERNAL_IPV4, EXTERNAL_IPV6}


@pytest.mark.anyio
async def test_validate_ssrf_dev_localhost_bypass():
    with patch.dict("os.environ", {"ENVIRONMENT": "development"}):
        result = await validate_ssrf("https://localhost/hook")
    assert result is None


# ---------------------------------------------------------------------------
# Tests for post_to_vetted_ip
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_post_to_vetted_ip_single_ipv4():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_client.post.return_value = mock_response

    resp = await post_to_vetted_ip(
        mock_client, "https://example.com/webhook", [EXTERNAL_IPV4], '{"a":1}', {"Content-Type": "application/json"}
    )
    assert resp is mock_response
    args, kwargs = mock_client.post.call_args
    posted_url = args[0]
    assert EXTERNAL_IPV4 in str(posted_url)
    assert kwargs["headers"]["Host"] == "example.com"
    assert kwargs["extensions"]["sni_hostname"] == "example.com"


@pytest.mark.anyio
async def test_post_to_vetted_ip_dual_stack_fallback():
    """IPv6 fails, falls back to IPv4."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_client.post.side_effect = [httpx.ConnectError("v6 unreachable"), mock_response]

    resp = await post_to_vetted_ip(
        mock_client, "https://example.com/hook", [EXTERNAL_IPV6, EXTERNAL_IPV4], "{}", {}
    )
    assert resp is mock_response
    assert mock_client.post.call_count == 2


@pytest.mark.anyio
async def test_post_to_vetted_ip_none_bypasses_pinning():
    """When vetted_ips is None (dev localhost), post directly to the URL."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_client.post.return_value = mock_response

    resp = await post_to_vetted_ip(mock_client, "https://localhost/hook", None, "{}", {})
    assert resp is mock_response
    args, _kwargs = mock_client.post.call_args
    assert args[0] == "https://localhost/hook"


# ---------------------------------------------------------------------------
# Tests for sign_payload
# ---------------------------------------------------------------------------


def test_sign_payload():
    secret = "whsec_test123"
    payload = '{"test": "data"}'
    timestamp = 1620000000

    signature = sign_payload(secret, payload, timestamp)
    assert len(signature) == 64
    assert isinstance(signature, str)


# ---------------------------------------------------------------------------
# Tests for process_delivery
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_process_delivery_success(db):
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

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_client.post.return_value = mock_response

    with patch("portal.webhooks.worker.validate_ssrf", return_value=[EXTERNAL_IPV4]):
        await process_delivery(mock_client, db, delivery)

    assert delivery.status == "succeeded"
    assert delivery.attempt_count == 1
    assert delivery.last_error is None

    mock_client.post.assert_called_once()
    args, kwargs = mock_client.post.call_args
    assert EXTERNAL_IPV4 in str(args[0])
    assert kwargs["headers"]["Host"] == "example.com"
    assert kwargs["extensions"]["sni_hostname"] == "example.com"

    sig_header = kwargs["headers"]["X-VoxBento-Signature"]
    assert sig_header.startswith("t=")
    assert ",v1=" in sig_header

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

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_client.post.side_effect = httpx.HTTPStatusError(
        "500 Server Error", request=MagicMock(), response=mock_response
    )

    with patch("portal.webhooks.worker.validate_ssrf", return_value=[EXTERNAL_IPV4]):
        await process_delivery(mock_client, db, delivery)

    assert delivery.status == "failed"
    assert delivery.attempt_count == 1
    assert delivery.last_error == "HTTP error: 500"
    assert delivery.next_attempt_at is not None
    assert delivery.next_attempt_at > datetime.now(timezone.utc)


@pytest.mark.anyio
async def test_process_delivery_circuit_breaker(db):
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

    with patch("portal.webhooks.worker.validate_ssrf", return_value=[EXTERNAL_IPV4]):
        await process_delivery(mock_client, db, delivery)

    assert delivery.status == "dead"
    assert delivery.attempt_count == 4

    assert sub.consecutive_failures == 5
    assert sub.is_active is False

    audit_result = await db.execute(select(OAuthAuditLog))
    audits = audit_result.scalars().all()
    assert len(audits) == 1
    assert audits[0].action == "webhook.circuit_breaker_tripped"
    assert audits[0].client_id is None


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

    with patch("portal.webhooks.worker.validate_ssrf", return_value=[EXTERNAL_IPV4]):
        await process_delivery(mock_client, db, delivery)

    audit_result = await db.execute(select(OAuthAuditLog))
    audits = audit_result.scalars().all()
    assert len(audits) == 1
    assert audits[0].action == "webhook.circuit_breaker_tripped"
    assert audits[0].client_id == client1.id


@pytest.mark.anyio
async def test_process_delivery_transient_dns_retry(db):
    """Transient DNS failure retries with backoff instead of killing the delivery."""
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
        subscription_id=sub.id, event_type="session.status_changed", payload={"ok": True}, status="delivering"
    )
    db.add(delivery)
    await db.flush()

    with patch(
        "portal.webhooks.worker.validate_ssrf",
        side_effect=HTTPException(status_code=400, detail="Invalid webhook URL: DNS resolution failed"),
    ):
        await process_delivery(AsyncMock(), db, delivery)

    assert delivery.status == "failed"
    assert delivery.attempt_count == 1
    assert "Temporary DNS failure" in delivery.last_error
    assert delivery.next_attempt_at is not None


@pytest.mark.anyio
async def test_process_delivery_ssrf_rejection_kills(db):
    """Non-transient SSRF rejection marks delivery dead immediately."""
    sub = WebhookSubscription(
        developer_account_id=1,
        target_url="https://evil.internal/hook",
        event_types=["session.status_changed"],
        secret_key="secret",
        is_active=True,
    )
    db.add(sub)
    await db.flush()

    delivery = WebhookDelivery(
        subscription_id=sub.id, event_type="session.status_changed", payload={}, status="delivering"
    )
    db.add(delivery)
    await db.flush()

    with patch(
        "portal.webhooks.worker.validate_ssrf",
        side_effect=HTTPException(status_code=400, detail="Invalid webhook URL: forbidden internal IP"),
    ):
        await process_delivery(AsyncMock(), db, delivery)

    assert delivery.status == "dead"
    assert "SSRF Validation failed" in delivery.last_error


# ---------------------------------------------------------------------------
# Tests for transport-level configuration
# ---------------------------------------------------------------------------


def test_worker_loop_uses_explicit_transport():
    """webhook_worker_loop uses AsyncHTTPTransport (bypasses env proxies, no keepalive)."""
    import pathlib

    src = pathlib.Path(portal.webhooks.worker.__file__).read_text()
    assert "AsyncHTTPTransport" in src, "worker must use explicit transport to bypass env proxies"
    assert "max_keepalive_connections=0" in src, "worker must disable keepalive to avoid cross-host TLS reuse"


# ---------------------------------------------------------------------------
# Tests for atomic queue claim
# ---------------------------------------------------------------------------


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

    for i in range(5):
        delivery = WebhookDelivery(
            subscription_id=sub.id, event_type="session.status_changed", payload={"id": i}, status="pending"
        )
        db.add(delivery)
    await db.flush()

    now = datetime.now(timezone.utc)
    from sqlalchemy import select, update

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

    assert len(claimed1) == 3
    assert len(claimed2) == 2

    ids1 = {d.id for d in claimed1}
    ids2 = {d.id for d in claimed2}
    assert ids1.isdisjoint(ids2)
