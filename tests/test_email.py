"""Tests for outbound email in portal.email: demo requests and the delayed onboarding email."""

from __future__ import annotations

import importlib
import logging
from datetime import timedelta

import pytest
from fastapi_mail import ConnectionConfig, FastMail

import portal.email as email_mod
from portal.models import utc_now


@pytest.fixture(autouse=True)
async def setup_db():
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
def outbox(monkeypatch):
    config = ConnectionConfig(
        MAIL_USERNAME="",
        MAIL_PASSWORD="",
        MAIL_FROM="noreply@voxbento.com",
        MAIL_PORT=587,
        MAIL_SERVER="localhost",
        MAIL_STARTTLS=True,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=False,
        SUPPRESS_SEND=1,
        TEMPLATE_FOLDER=str(email_mod._TEMPLATE_DIR),
    )
    monkeypatch.setattr(email_mod, "_mail_config", config)
    with FastMail(config).record_messages() as messages:
        yield messages


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_user(email, *, email_verified=False):
    from portal.database import create_user, get_session

    async with get_session() as s:
        user = await create_user(s, email=email, display_name="Alice", email_verified=email_verified)
    return user.id


async def _get_user(user_id):
    from portal.database import get_session, get_user_by_id

    async with get_session() as s:
        return await get_user_by_id(s, user_id)


async def _set_due(user_id, due_at):
    from portal.database import get_session, get_user_by_id

    async with get_session() as s:
        user = await get_user_by_id(s, user_id)
        user.onboarding_email_due_at = due_at


@pytest.mark.anyio
@pytest.mark.parametrize(("token_type", "path"), [("verification", "/auth/verify"), ("magic_link", "/auth/magic")])
async def test_onboarding_email_is_persisted_and_sent_once(outbox, token_type, path):
    from portal.database import create_auth_token, get_session

    user_id = await _create_user("alice@example.com")
    async with get_session() as s:
        await create_auth_token(s, jti=f"tok-{token_type}", user_id=user_id, token_type=token_type)

    before = utc_now().replace(tzinfo=None)
    async with _client() as c:
        resp = await c.get(f"{path}/tok-{token_type}", follow_redirects=False)
    after = utc_now().replace(tzinfo=None)
    assert resp.status_code == 303
    assert outbox == []

    due_at = (await _get_user(user_id)).onboarding_email_due_at
    assert due_at is not None
    assert before + timedelta(minutes=15) <= due_at.replace(tzinfo=None) <= after + timedelta(minutes=15)

    # The due time lives in the database, so a fresh process sends it once the delay has passed.
    await _set_due(user_id, utc_now() - timedelta(seconds=1))
    await email_mod.send_due_onboarding_emails()
    await email_mod.send_due_onboarding_emails()

    assert len(outbox) == 1
    msg = outbox[0]
    assert "<alice@example.com>" in msg["To"]
    assert msg["Subject"] == "Welcome to VoxBento! How can we help?"
    assert "Arnav Angarkar, Team Voxbento" in msg["From"]
    assert "<voxbento.dev@gmail.com>" in msg["Reply-To"]
    assert (await _get_user(user_id)).onboarding_email_due_at is None


@pytest.mark.anyio
async def test_onboarding_email_not_sent_before_due_or_to_existing_users(outbox):
    pending = await _create_user("pending@example.com")
    await _set_due(pending, utc_now() + timedelta(minutes=5))
    await _create_user("existing@example.com", email_verified=True)

    await email_mod.send_due_onboarding_emails()

    assert outbox == []


@pytest.mark.anyio
async def test_stale_verification_link_does_not_reschedule_onboarding_email(outbox):
    from portal.database import create_auth_token, get_session

    user_id = await _create_user("alice@example.com", email_verified=True)
    async with get_session() as s:
        await create_auth_token(s, jti="stale-tok", user_id=user_id, token_type="verification")

    async with _client() as c:
        resp = await c.get("/auth/verify/stale-tok", follow_redirects=False)

    assert resp.status_code == 303
    assert (await _get_user(user_id)).onboarding_email_due_at is None


@pytest.mark.anyio
async def test_failed_onboarding_send_does_not_drop_other_due_emails(outbox, monkeypatch):
    users = [await _create_user(f"u{i}@example.com") for i in range(3)]
    for user_id in users:
        await _set_due(user_id, utc_now() - timedelta(seconds=1))

    real_send = email_mod.send_onboarding_email

    async def crash_once(email_to, display_name):
        monkeypatch.setattr(email_mod, "send_onboarding_email", real_send)
        raise RuntimeError("process died mid-send")

    monkeypatch.setattr(email_mod, "send_onboarding_email", crash_once)
    with pytest.raises(RuntimeError):
        await email_mod.send_due_onboarding_emails()
    await email_mod.send_due_onboarding_emails()

    assert len(outbox) == 2


@pytest.mark.anyio
async def test_demo_request_sender_is_not_changed_by_onboarding_email(outbox):
    await email_mod.send_onboarding_email("alice@example.com", "Alice")
    await email_mod.send_demo_request_email({"email": "lead@example.com", "firstName": "Lee"})

    demo = outbox[1]
    assert demo["Subject"] == "New Demo Request from lead@example.com"
    assert "<voxbento.dev@gmail.com>" in demo["To"]
    assert demo["From"] == "noreply@voxbento.com"


@pytest.mark.anyio
async def test_nothing_sent_and_warning_logged_without_smtp(monkeypatch, caplog):
    monkeypatch.setattr(email_mod, "_mail_config", None)

    with caplog.at_level(logging.WARNING, logger="portal.email"):
        await email_mod.send_demo_request_email({"email": "lead@example.com"})
        await email_mod.send_onboarding_email("alice@example.com", "Alice")

    warnings = [r.getMessage() for r in caplog.records if "SMTP is not configured" in r.getMessage()]
    assert len(warnings) == 2


@pytest.mark.anyio
async def test_legacy_email_sender_module_is_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("portal.email_sender")
