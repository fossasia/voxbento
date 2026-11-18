from __future__ import annotations

import importlib
import logging
from datetime import timedelta, timezone
from email.utils import parseaddr

import pytest
from fastapi_mail import ConnectionConfig, FastMail
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import portal.notifications as notifications
from fastapi_app import app
from portal.database import configure, create_auth_token, create_user, dispose, get_session, init_db
from portal.models import AuthToken, EmailDelivery, User, utc_now


@pytest.fixture(autouse=True)
async def setup_db(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'email.db'}"
    configure(database_url)
    await init_db()
    monkeypatch.setattr("portal.rate_limit._rates", {})
    monkeypatch.setattr(notifications.settings, "jwt_secret", "local-only-email-regression-signing-key")
    monkeypatch.setattr(notifications.settings, "smtp_host", "")
    monkeypatch.setattr(notifications.settings, "hosting_requests_email", "sales@example.com")
    yield database_url
    await dispose()


@pytest.fixture
def outbox(monkeypatch):
    config = ConnectionConfig(
        MAIL_USERNAME="",
        MAIL_PASSWORD="",
        MAIL_FROM="notifications@example.com",
        MAIL_PORT=587,
        MAIL_SERVER="localhost",
        MAIL_STARTTLS=True,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=False,
        SUPPRESS_SEND=1,
    )
    monkeypatch.setattr(notifications.settings, "smtp_host", "localhost")
    monkeypatch.setattr(notifications.settings, "smtp_from_email", "notifications@example.com")
    monkeypatch.setattr(notifications, "_mail_config", lambda: config)
    with FastMail(config).record_messages() as messages:
        yield messages


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_user(email, *, email_verified=False):
    async with get_session() as session:
        user = await create_user(session, email=email, display_name="Alice", email_verified=email_verified)
    return user.id


async def _deliveries(key="onboarding"):
    async with get_session() as session:
        return list(
            (
                await session.scalars(
                    select(EmailDelivery).where(EmailDelivery.notification_key == key).order_by(EmailDelivery.id)
                )
            ).all()
        )


async def _queue_onboarding(user_id, due_at):
    async with get_session() as session:
        user = await session.get(User, user_id)
        delivery = await notifications.queue_notification(
            session,
            "onboarding",
            user.email,
            {"display_name": user.display_name},
            scheduled_at=due_at,
            idempotency_key=f"onboarding:{user.id}",
        )
    return delivery.id


@pytest.mark.anyio
@pytest.mark.parametrize(("token_type", "path"), [("verification", "/auth/verify"), ("magic_link", "/auth/magic")])
async def test_onboarding_email_is_persisted_and_sent_once(setup_db, outbox, monkeypatch, token_type, path):
    user_id = await _create_user("alice@example.com")
    async with get_session() as session:
        token = await create_auth_token(session, jti=f"tok-{token_type}", user_id=user_id, token_type=token_type)
        token_id = token.id

    before = utc_now().replace(tzinfo=None)
    async with _client() as client:
        response = await client.get(f"{path}/tok-{token_type}", follow_redirects=False)
    after = utc_now().replace(tzinfo=None)
    assert response.status_code == 303
    assert "user_token" in response.cookies
    assert outbox == []

    deliveries = await _deliveries()
    assert len(deliveries) == 1
    queued = deliveries[0]
    due_at = queued.scheduled_at.replace(tzinfo=None)
    assert before + timedelta(minutes=15) <= due_at <= after + timedelta(minutes=15)
    assert queued.status == "pending"
    assert queued.attempt_count == 0
    assert queued.recipient == "alice@example.com"
    assert await notifications.process_due_deliveries() == 0

    await dispose()
    configure(setup_db)
    reopened = await _deliveries()
    assert [(row.id, row.scheduled_at, row.status) for row in reopened] == [(queued.id, queued.scheduled_at, "pending")]
    async with get_session() as session:
        assert (await session.get(User, user_id)).email_verified
        assert (await session.get(AuthToken, token_id)).is_used
    assert await notifications.process_due_deliveries() == 0
    assert outbox == []

    monkeypatch.setattr(notifications, "utc_now", lambda: due_at.replace(tzinfo=timezone.utc) + timedelta(seconds=1))
    assert await notifications.process_due_deliveries() == 1
    assert await notifications.process_due_deliveries() == 0

    assert len(outbox) == 1
    assert parseaddr(outbox[0]["To"])[1] == "alice@example.com"
    assert outbox[0]["Subject"] == "Welcome to VoxBento"
    stored = (await _deliveries())[0]
    assert stored.status == "accepted"
    assert stored.accepted_at is not None
    assert stored.attempt_count == 1


@pytest.mark.anyio
async def test_onboarding_email_not_sent_before_due_or_to_existing_users(outbox):
    pending = await _create_user("pending@example.com")
    await _queue_onboarding(pending, utc_now() + timedelta(minutes=5))
    await _create_user("existing@example.com", email_verified=True)

    assert await notifications.process_due_deliveries() == 0

    assert outbox == []
    deliveries = await _deliveries()
    assert [(row.recipient, row.status, row.attempt_count) for row in deliveries] == [
        ("pending@example.com", "pending", 0)
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(("token_type", "path"), [("verification", "/auth/verify"), ("magic_link", "/auth/magic")])
@pytest.mark.parametrize("already_queued", [False, True], ids=["no-welcome", "queued-welcome"])
async def test_stale_auth_link_does_not_create_or_reschedule_onboarding_email(outbox, token_type, path, already_queued):
    user_id = await _create_user("alice@example.com", email_verified=True)
    async with get_session() as session:
        token = await create_auth_token(session, jti="stale-tok", user_id=user_id, token_type=token_type)
        token_id = token.id
    if already_queued:
        await _queue_onboarding(user_id, utc_now() + timedelta(minutes=5))
    before = [(row.id, row.scheduled_at, row.status) for row in await _deliveries()]

    async with _client() as client:
        response = await client.get(f"{path}/stale-tok", follow_redirects=False)

    assert response.status_code == 303
    assert "user_token" in response.cookies
    async with get_session() as session:
        assert (await session.get(AuthToken, token_id)).is_used
    assert outbox == []
    assert [(row.id, row.scheduled_at, row.status) for row in await _deliveries()] == before


@pytest.mark.anyio
async def test_failed_onboarding_send_does_not_drop_other_due_emails(setup_db, outbox, monkeypatch):
    for i in range(3):
        user_id = await _create_user(f"u{i}@example.com")
        await _queue_onboarding(user_id, utc_now() - timedelta(seconds=1))
    real_send = FastMail.send_message
    failed_once = False

    async def fail_once(self, message):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("simulated SMTP failure")
        await real_send(self, message)

    monkeypatch.setattr(FastMail, "send_message", fail_once)
    assert await notifications.process_due_deliveries(limit=1) == 1
    failed = (await _deliveries())[0]
    assert failed.status == "pending"
    assert failed.attempt_count == 1
    assert "simulated SMTP failure" in failed.last_error
    assert failed.scheduled_at > utc_now().replace(tzinfo=None)
    assert outbox == []

    await dispose()
    configure(setup_db)
    retained = await _deliveries()
    assert [row.status for row in retained] == ["pending", "pending", "pending"]
    assert retained[0].scheduled_at == failed.scheduled_at
    assert await notifications.process_due_deliveries() == 2
    assert [parseaddr(message["To"])[1] for message in outbox] == ["u1@example.com", "u2@example.com"]

    retry_at = failed.scheduled_at.replace(tzinfo=timezone.utc) + timedelta(seconds=1)
    monkeypatch.setattr(notifications, "utc_now", lambda: retry_at)
    assert await notifications.process_due_deliveries() == 1
    assert await notifications.process_due_deliveries() == 0
    assert sorted(parseaddr(message["To"])[1] for message in outbox) == [f"u{i}@example.com" for i in range(3)]
    delivered = await _deliveries()
    assert [row.status for row in delivered] == ["accepted", "accepted", "accepted"]
    assert [row.attempt_count for row in delivered] == [2, 1, 1]


@pytest.mark.anyio
async def test_demo_request_sender_is_not_changed_by_onboarding_email(outbox):
    user_id = await _create_user("alice@example.com")
    await _queue_onboarding(user_id, utc_now() - timedelta(seconds=1))
    assert await notifications.process_due_deliveries() == 1
    async with _client() as client:
        response = await client.post("/api/request-demo", json={"email": "lead@example.com", "firstName": "Lee"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert await notifications.process_due_deliveries() == 1

    assert len(outbox) == 2
    welcome, demo = outbox
    assert welcome["Subject"] == "Welcome to VoxBento"
    assert demo["Subject"] == "New VoxBento demo request"
    assert parseaddr(demo["To"])[1] == "sales@example.com"
    for message in outbox:
        assert message["From"] == "notifications@example.com"
        assert message["Reply-To"] is None
    assert notifications._mail_config().MAIL_FROM == "notifications@example.com"
    assert (await _deliveries("demo_request"))[0].status == "accepted"


@pytest.mark.anyio
async def test_nothing_sent_and_warning_logged_without_smtp(outbox, monkeypatch, caplog):
    monkeypatch.setattr(notifications.settings, "smtp_host", "")
    async with get_session() as session:
        await notifications.queue_notification(
            session, "demo_request", "sales@example.com", {"email": "lead@example.com"}
        )
        await notifications.queue_notification(session, "onboarding", "alice@example.com", {"display_name": "Alice"})

    with caplog.at_level(logging.WARNING, logger="portal.notifications"):
        assert await notifications.process_due_deliveries() == 2
        assert await notifications.process_due_deliveries() == 0

    assert outbox == []
    warnings = [record.getMessage() for record in caplog.records if "SMTP is not configured" in record.getMessage()]
    assert len(warnings) == 2
    async with get_session() as session:
        deliveries = list((await session.scalars(select(EmailDelivery))).all())
    assert len(deliveries) == 2
    assert all(row.status == "disabled" and row.accepted_at is None for row in deliveries)
    assert all(row.encrypted_subject and row.encrypted_html for row in deliveries)


@pytest.mark.anyio
@pytest.mark.parametrize("module_name", ["portal.email", "portal.email_sender"])
async def test_legacy_email_modules_are_gone(module_name):
    with pytest.raises(ModuleNotFoundError) as error:
        importlib.import_module(module_name)
    assert error.value.name == module_name
