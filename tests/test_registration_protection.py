from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy import func, select

from fastapi_app import app
from portal import globals as portal_globals
from portal import rate_limit
from portal.auth import verify_password
from portal.config import Settings, settings
from portal.database import configure, dispose, get_session, init_db
from portal.models import AuthToken, EmailDelivery, User
from portal.routers import auth as auth_routes


@pytest.fixture(autouse=True)
async def registration_db(monkeypatch):
    monkeypatch.setattr(rate_limit, "_rates", {})
    monkeypatch.setattr(settings, "registration_turnstile_enabled", False)
    monkeypatch.setattr(settings, "registration_requests_per_minute", 20)
    monkeypatch.setattr(settings, "turnstile_site_key", "test-public-key")
    monkeypatch.setattr(settings, "turnstile_secret_key", "test-private-key")
    monkeypatch.setattr(settings, "public_base_url", "https://test")
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        yield client


@pytest.mark.anyio
async def test_rotating_emails_and_forwarded_headers_cannot_evade_signup_limit(client):
    for index in range(21):
        response = await client.post(
            "/register",
            data={"email": f"spam{index}@example.com", "display_name": "Promo link"},
            headers={"X-Forwarded-For": f"192.0.2.{index}", "CF-Connecting-IP": f"192.0.2.{index}"},
        )
        assert response.status_code == (200 if index < 20 else 429)
    assert response.headers["retry-after"] == "60"
    async with get_session() as session:
        for model in (User, AuthToken, EmailDelivery):
            assert await session.scalar(select(func.count()).select_from(model)) == 20


@pytest.fixture
async def turnstile(monkeypatch):
    monkeypatch.setattr(settings, "registration_turnstile_enabled", True)
    responses = []
    calls = []

    def handle(request):
        calls.append(request)
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as provider:
        monkeypatch.setattr(portal_globals, "shared_http_client", provider)
        yield responses, calls


async def assert_no_signup_rows():
    async with get_session() as session:
        for model in (User, AuthToken, EmailDelivery):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


def reject_password_hash(_password):
    pytest.fail("A rejected signup must not hash a password")


@pytest.mark.anyio
@pytest.mark.parametrize("token", [None, "", " ", "x" * 2049])
async def test_missing_or_oversized_challenge_has_no_side_effects(client, turnstile, monkeypatch, token):
    monkeypatch.setattr(auth_routes, "hash_password", reject_password_hash)
    data = {
        "email": "spam@example.com",
        "display_name": "Spam",
        "password": "password123",
        "password_confirm": "password123",
    }
    if token is not None:
        data["cf-turnstile-response"] = token
    response = await client.post("/register", data=data, headers={"Accept": "text/html"})
    assert response.status_code == 403
    assert "Please complete the security check" in response.text
    assert "test-private-key" not in response.text
    assert "password123" not in response.text
    assert "spam@example.com" in response.text
    assert turnstile[1] == []
    await assert_no_signup_rows()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "result,status_code",
    [
        ({"success": False, "error-codes": ["timeout-or-duplicate"]}, 403),
        ({"success": True, "hostname": "attacker.example", "action": "register"}, 403),
        ({"success": True, "hostname": "test", "action": "login"}, 403),
        ({"success": True}, 403),
        ({"success": "true", "hostname": "test", "action": "register"}, 503),
        ([], 503),
        ({}, 503),
    ],
)
async def test_invalid_provider_verdict_cannot_create_accounts(client, turnstile, monkeypatch, result, status_code):
    monkeypatch.setattr(auth_routes, "hash_password", reject_password_hash)
    turnstile[0].append(httpx.Response(200, json=result))
    response = await client.post(
        "/register",
        data={
            "email": "spam@example.com",
            "display_name": "Spam",
            "cf-turnstile-response": "untrusted-token",
            "password": "password123",
            "password_confirm": "password123",
        },
    )
    assert response.status_code == status_code
    assert len(turnstile[1]) == 1
    await assert_no_signup_rows()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(200, text="not JSON"),
        httpx.ConnectError("offline"),
        httpx.ReadTimeout("timeout"),
    ],
)
async def test_provider_outage_fails_closed(client, turnstile, failure):
    turnstile[0].append(failure)
    response = await client.post(
        "/register",
        data={
            "email": "spam@example.com",
            "display_name": "Spam",
            "cf-turnstile-response": "untrusted-token",
        },
    )
    assert response.status_code == 503
    assert "temporarily unavailable" in response.text
    await assert_no_signup_rows()


@pytest.mark.anyio
@pytest.mark.parametrize("missing_setting", ["turnstile_site_key", "turnstile_secret_key", "public_base_url"])
async def test_enabled_but_incomplete_config_fails_closed(client, turnstile, monkeypatch, missing_setting):
    monkeypatch.setattr(settings, missing_setting, "")
    response = await client.post("/register", data={"email": "spam@example.com", "display_name": "Spam"})
    assert response.status_code == 503
    assert turnstile[1] == []
    await assert_no_signup_rows()


@pytest.mark.anyio
@pytest.mark.parametrize("password", ["", "a secure password"])
async def test_verified_challenge_preserves_registration_and_rejects_replay(client, turnstile, password):
    turnstile[0].extend(
        [
            httpx.Response(200, json={"success": True, "hostname": "test", "action": "register"}),
            httpx.Response(200, json={"success": False, "error-codes": ["timeout-or-duplicate"]}),
        ]
    )
    form = {
        "email": "person@example.com",
        "display_name": "Person",
        "cf-turnstile-response": "single-use-token",
        "password": password,
        "password_confirm": password,
    }
    response = await client.post("/register", data=form)
    assert response.status_code == 200
    assert "Check your email" in response.text
    assert "user_token" not in response.cookies
    replay = await client.post("/register", data={**form, "email": "replay@example.com"})
    assert replay.status_code == 403
    async with get_session() as session:
        user = await session.scalar(select(User))
        assert user.email == "person@example.com"
        assert user.email_verified is False
        assert verify_password(password, user.password_hash) if password else user.password_hash is None
        for model in (User, AuthToken, EmailDelivery):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
    request = turnstile[1][0]
    assert str(request.url) == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert parse_qs(request.content.decode()) == {"secret": ["test-private-key"], "response": ["single-use-token"]}
    assert request.extensions["timeout"]["read"] == 5.0


@pytest.mark.anyio
async def test_widget_only_loads_when_enabled_and_survives_form_errors(client, monkeypatch):
    response = await client.get("/register")
    assert "challenges.cloudflare.com" not in response.text
    monkeypatch.setattr(settings, "registration_turnstile_enabled", True)
    for response in (await client.get("/register"), await client.post("/register", data={})):
        assert "https://challenges.cloudflare.com/turnstile/v0/api.js" in response.text
        assert "data-action='register'" in response.text
        assert "test-public-key" in response.text
        assert "test-private-key" not in response.text
    assert response.status_code == 422


@pytest.mark.anyio
async def test_defaults_keep_turnstile_optional_and_hide_secret():
    config = Settings(_env_file=None, turnstile_secret_key="private-test-value")
    assert config.registration_turnstile_enabled is False
    assert "private-test-value" not in repr(config)


@pytest.mark.anyio
async def test_signup_limits_are_per_peer_and_recover_after_window(client, monkeypatch):
    monkeypatch.setattr(settings, "registration_requests_per_minute", 1)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1000.0)
    assert (await client.post("/register", data={})).status_code == 422
    limited = await client.post("/register", data={}, headers={"Accept": "text/html"})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("192.0.2.2", 123)), base_url="https://test"
    ) as other:
        assert (await other.post("/register", data={})).status_code == 422
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1061.0)
    assert (await client.post("/register", data={})).status_code == 422
    await assert_no_signup_rows()


@pytest.mark.anyio
async def test_rate_limit_precedes_provider_requests(client, turnstile, monkeypatch):
    monkeypatch.setattr(settings, "registration_requests_per_minute", 1)
    form = {"email": "spam@example.com", "display_name": "Spam"}
    assert (await client.post("/register", data=form)).status_code == 403
    form["cf-turnstile-response"] = "untrusted-token"
    assert (await client.post("/register", data=form)).status_code == 429
    assert turnstile[1] == []
    await assert_no_signup_rows()
