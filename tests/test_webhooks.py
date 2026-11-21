from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
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
from portal.webhooks.worker import (
    DeliveryAttempt,
    claim_due_deliveries,
    load_delivery_job,
    process_delivery,
    record_delivery_attempt,
    recover_stale_claims,
    run_worker_iteration,
    sign_payload,
)

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
    target_url: str = "https://example.com/webhook",
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
            target_url=target_url,
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
    assert str(args[0]) == "https://93.184.216.34/webhook"
    assert kwargs["headers"]["Host"] == "example.com"
    assert kwargs["extensions"] == {"sni_hostname": "example.com"}

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
@pytest.mark.parametrize("dns_errno", [socket.EAI_AGAIN, socket.EAI_NONAME])
@pytest.mark.parametrize("recovered_ip, expected_status", [("93.184.216.34", "succeeded"), ("127.0.0.1", "dead")])
async def test_process_delivery_revalidates_after_dns_failure(
    worker_db, dns, monkeypatch, dns_errno, recovered_ip, expected_status
):
    delivery_id, _, _ = await seed_delivery(target_url="https://recovery.example.test/hook")
    mock_client = successful_client()
    before = datetime.now(timezone.utc)

    with monkeypatch.context() as resolver:
        resolver.setattr(
            socket, "getaddrinfo", MagicMock(side_effect=socket.gaierror(dns_errno, "resolver unavailable"))
        )
        await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.attempt_count == 1
        assert (
            before + timedelta(seconds=2)
            <= delivery.next_attempt_at.replace(tzinfo=timezone.utc)
            <= datetime.now(timezone.utc) + timedelta(seconds=2)
        )
        assert "resolver unavailable" in delivery.last_error
    mock_client.post.assert_not_called()

    dns["recovery.example.test"] = [recovered_ip]
    # Simulate the retry: the worker claims the delivery again after its backoff.
    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        delivery.status = "delivering"
        delivery.next_attempt_at = datetime.now(timezone.utc)

    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)

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
async def test_process_delivery_dns_failures_use_retry_limit(
    worker_db, monkeypatch, attempt_count, expected_status, backoff
):
    delivery_id, subscription_id, _ = await seed_delivery(
        target_url="https://outage.example.test/hook",
        attempt_count=attempt_count,
        consecutive_failures=4,
    )
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    monkeypatch.setattr(
        socket, "getaddrinfo", MagicMock(side_effect=socket.gaierror(socket.EAI_AGAIN, "resolver unavailable"))
    )
    before = datetime.now(timezone.utc)

    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        sub = await session.get(WebhookSubscription, subscription_id)

    assert delivery.status == expected_status
    assert delivery.attempt_count == attempt_count + 1
    mock_client.post.assert_not_called()
    if backoff is not None:
        assert (
            before + timedelta(seconds=backoff)
            <= delivery.next_attempt_at.replace(tzinfo=timezone.utc)
            <= datetime.now(timezone.utc) + timedelta(seconds=backoff)
        )
        assert sub.consecutive_failures == 4
        assert sub.is_active
    else:
        assert sub.consecutive_failures == 5
        assert not sub.is_active


@pytest.mark.anyio
@pytest.mark.parametrize("rebound_ip", ["127.0.0.1", "::7f00:1", "::a00:1"])
async def test_process_delivery_rejects_rebound_dns(worker_db, dns, rebound_ip):
    dns["rebind.attacker.test"] = ["93.184.216.34"]
    delivery_id, _, _ = await seed_delivery(target_url="https://rebind.attacker.test:9443/hook")

    dns["rebind.attacker.test"] = [rebound_ip]
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)

    mock_client.post.assert_not_called()
    assert delivery.status == "dead"
    assert rebound_ip in delivery.last_error


@pytest.mark.anyio
@pytest.mark.parametrize("connect_failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_process_delivery_keeps_port_and_tries_next_vetted_ip(worker_db, dns, connect_failure):
    dns["dual.example.org"] = ["2606:2800:220:1::1", "2606:2800:220:1::2", "93.184.216.34"]
    delivery_id, _, _ = await seed_delivery(target_url="https://dual.example.org:8443/hook?a=1")

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = [connect_failure("IPv6 unreachable"), MagicMock()]
    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)

    assert delivery.status == "succeeded"
    urls = [str(call.args[0]) for call in mock_client.post.call_args_list]
    assert urls == ["https://[2606:2800:220:1::1]:8443/hook?a=1", "https://93.184.216.34:8443/hook?a=1"]
    assert mock_client.post.call_args.kwargs["headers"]["Host"] == "dual.example.org:8443"


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
async def test_process_delivery_invalid_url_is_recorded(worker_db):
    delivery_id, _, _ = await seed_delivery()

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.post.side_effect = httpx.InvalidURL("invalid URL")

    await process_delivery(mock_client, delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        assert delivery.status == "failed"
        assert delivery.last_error.startswith("Invalid URL:")


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
    """Two concurrent workers cannot claim the same delivery."""
    await seed_delivery(delivery_count=15, status="pending")
    now = datetime.now(timezone.utc)

    async def claim_once() -> list[int]:
        async with database.get_session() as session:
            return await claim_due_deliveries(session, now)

    claimed1, claimed2 = await asyncio.gather(claim_once(), claim_once())

    # Batch size caps the first claim; the winner of the race is not deterministic.
    assert sorted([len(claimed1), len(claimed2)]) == [5, 10]
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
async def test_worker_isolates_unexpected_delivery_failure(worker_db, monkeypatch):
    """One failing dispatch does not strand the rest of the claimed batch."""
    await seed_delivery(delivery_count=2, status="pending")
    real_process_delivery = worker.process_delivery
    calls = 0

    async def flaky_process_delivery(client, delivery_id, job=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        await real_process_delivery(client, delivery_id, job)

    monkeypatch.setattr(worker, "process_delivery", flaky_process_delivery)
    mock_client = successful_client()

    claimed = await run_worker_iteration(mock_client)

    assert claimed == 2
    mock_client.post.assert_called_once()
    async with database.get_session() as session:
        deliveries = list((await session.scalars(select(WebhookDelivery).order_by(WebhookDelivery.id))).all())
    assert [delivery.status for delivery in deliveries] == ["failed", "succeeded"]
    assert [delivery.attempt_count for delivery in deliveries] == [1, 1]
    assert deliveries[0].last_error == "Unexpected dispatch error."


@pytest.mark.anyio
async def test_recovery_dead_letters_exhausted_claim(worker_db):
    now = datetime.now(timezone.utc)
    delivery_id, subscription_id, _ = await seed_delivery(
        status="delivering",
        next_attempt_at=now - timedelta(seconds=5),
        attempt_count=3,
        consecutive_failures=4,
    )

    async with database.get_session() as session:
        recovered = await recover_stale_claims(session, now)
    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        subscription = await session.get(WebhookSubscription, subscription_id)
        audits = (await session.execute(select(OAuthAuditLog))).scalars().all()

    assert recovered == 1
    assert delivery.status == "dead"
    assert delivery.attempt_count == 4
    assert subscription.consecutive_failures == 5
    assert subscription.is_active is False
    assert len(audits) == 1


@pytest.mark.anyio
async def test_late_worker_cannot_overwrite_reclaimed_delivery(worker_db):
    """A worker whose lease expired must not write over a newer claim."""
    now = datetime.now(timezone.utc)
    delivery_id, _, _ = await seed_delivery(
        status="delivering",
        next_attempt_at=now - timedelta(seconds=5),
    )
    async with database.get_session() as session:
        stale_job = await load_delivery_job(session, delivery_id)
    assert stale_job is not None

    async with database.get_session() as session:
        await recover_stale_claims(session, now)
    async with database.get_session() as session:
        claimed = await claim_due_deliveries(session, now)
    assert claimed == [delivery_id]

    async with database.get_session() as session:
        await record_delivery_attempt(session, stale_job, DeliveryAttempt(success=True))

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        assert delivery.status == "delivering"


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
async def test_process_delivery_records_success_without_refreshing(worker_db):
    """The outcome is written from a fresh row read, so no stale ORM object can break it.

    A delivery that succeeds must also clear the subscription's failure counter.
    """
    delivery_id, subscription_id, _ = await seed_delivery(consecutive_failures=2)

    await process_delivery(successful_client(), delivery_id)

    async with database.get_session() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        subscription = await session.get(WebhookSubscription, subscription_id)

    assert delivery.status == "succeeded"
    assert delivery.last_error is None
    assert subscription.consecutive_failures == 0


@pytest.mark.anyio
async def test_webhook_debug_dump_endpoint_removed():
    """Ensure the unauthenticated debug dump endpoint is not present or accessible."""
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Unauthenticated request
        response = await client.get("/api/v1/webhooks/debug/dump")
        assert response.status_code == 404

        # Request with arbitrary Bearer token
        response_with_auth = await client.get(
            "/api/v1/webhooks/debug/dump",
            headers={"Authorization": "Bearer test_token"},
        )
        assert response_with_auth.status_code == 404
