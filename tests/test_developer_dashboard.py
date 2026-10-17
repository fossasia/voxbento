"""Tests for the developer dashboard form endpoints.

Covers:
- Input validation when applying for developer access
- Input validation when registering an OAuth client (app name, redirect URIs)
- CSRF and account-status guards on client registration
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import re

import pytest
from sqlalchemy import select

from portal.auth import create_user_token, hash_password
from portal.database import configure, create_user, dispose, get_session, init_db
from portal.models import DeveloperAccount, OAuthClient
from portal.routers.developer import (
    MAX_NAME_LENGTH,
    MAX_REDIRECT_URI_LENGTH,
    MAX_REDIRECT_URIS,
    parse_redirect_uris,
)

CSRF = "csrf-test-token"


@pytest.fixture
async def setup_db():
    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


def _client():
    from httpx import ASGITransport, AsyncClient

    from fastapi_app import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _developer_cookies(account_status: str | None = "approved") -> dict[str, str]:
    """Create a user, optionally with a developer account, and return its dashboard cookies."""
    async with get_session() as s:
        user = await create_user(
            s, email="dev@test.com", display_name="Dev", password_hash=hash_password("securepass123")
        )
        if account_status is not None:
            s.add(DeveloperAccount(user_id=user.id, status=account_status, organization_name="Acme"))
        await s.commit()
        token = create_user_token(user_id=user.id, email=user.email)
    return {"user_token": token, "dashboard_csrf": CSRF}


async def _stored_clients() -> list[OAuthClient]:
    async with get_session() as s:
        return list((await s.execute(select(OAuthClient))).scalars().all())


async def _register(cookies: dict[str, str], app_name: str, redirect_uris: str, csrf_token: str = CSRF):
    async with _client() as c:
        return await c.post(
            "/api/developer/clients",
            data={"app_name": app_name, "redirect_uris": redirect_uris, "csrf_token": csrf_token},
            cookies=cookies,
        )


# ---------------------------------------------------------------------------
# Redirect URI parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://app.example/cb", ("https://app.example/cb",)),
        ("https://app.example/cb?tenant=1", ("https://app.example/cb?tenant=1",)),
        ("http://localhost/callback", ("http://localhost/callback",)),
        ("http://127.0.0.1:8080/cb", ("http://127.0.0.1:8080/cb",)),
        ("http://[::1]:8080/cb", ("http://[::1]:8080/cb",)),
        (" https://a.example/cb , https://b.example/cb ,", ("https://a.example/cb", "https://b.example/cb")),
        ("https://a.example/cb,https://a.example/cb", ("https://a.example/cb",)),
    ],
)
def test_parse_redirect_uris_accepts_valid_lists(raw, expected):
    assert parse_redirect_uris(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        " , ,",
        "not a url",
        "app.example/cb",
        "javascript:alert(document.cookie)",
        "ftp://app.example/cb",
        "https:///cb",
        "http://evil.example/cb",
        "http://localhost.evil.example/cb",
        "https://app.example/cb#fragment",
        "https://user:secret@app.example/cb",
        "https://app.example:notaport/cb",
        "https://app.example/c b",
        pytest.param("https://app.example/" + "a" * MAX_REDIRECT_URI_LENGTH, id="too-long"),
        pytest.param(",".join(f"https://app{i}.example/cb" for i in range(MAX_REDIRECT_URIS + 1)), id="too-many"),
    ],
)
def test_parse_redirect_uris_rejects_unusable_lists(raw):
    with pytest.raises(ValueError):
        parse_redirect_uris(raw)


# ---------------------------------------------------------------------------
# POST /api/developer/clients
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_register_client_with_valid_redirect_uris(setup_db):
    cookies = await _developer_cookies()

    resp = await _register(cookies, "  My Plugin  ", "https://app.example/cb, http://localhost:3000/cb")

    assert resp.status_code == 200
    assert b"App Created Successfully" in resp.content
    (client,) = await _stored_clients()
    assert client.name == "My Plugin"
    assert client.redirect_uris == ["https://app.example/cb", "http://localhost:3000/cb"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "redirect_uris, reason",
    [
        (",,", b"Enter at least one redirect URI."),
        ("not a url", b"it contains spaces or control characters"),
        ("app.example/cb", b"it must start with https://"),
        ("javascript:alert(1)", b"it must start with https://"),
        ("http://evil.example/cb", b"http is only allowed for localhost"),
        ("https://app.example/cb#frag", b"it must not contain a fragment"),
        ("https://user:pw@app.example/cb", b"it must not contain a username or password"),
        pytest.param(
            "https://app.example/" + "a" * MAX_REDIRECT_URI_LENGTH, b"it is longer than 500 characters", id="too-long"
        ),
        ("https://ok.example/cb, http://evil.example/cb", b"http is only allowed for localhost"),
    ],
)
async def test_register_client_rejects_invalid_redirect_uris(redirect_uris, reason, setup_db):
    cookies = await _developer_cookies()

    resp = await _register(cookies, "My Plugin", redirect_uris)

    assert resp.status_code == 400
    assert reason in resp.content
    assert b"App Created Successfully" not in resp.content
    assert await _stored_clients() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "app_name, reason",
    [
        pytest.param("   ", b"App name cannot be empty.", id="blank"),
        pytest.param("n" * (MAX_NAME_LENGTH + 1), b"App name must be 200 characters or fewer.", id="too-long"),
        pytest.param("My\x00Plugin", b"App name contains characters that are not allowed.", id="nul"),
        pytest.param("My\nPlugin", b"App name contains characters that are not allowed.", id="newline"),
    ],
)
async def test_register_client_rejects_invalid_app_name(app_name, reason, setup_db):
    cookies = await _developer_cookies()

    resp = await _register(cookies, app_name, "https://app.example/cb")

    assert resp.status_code == 400
    assert reason in resp.content
    assert await _stored_clients() == []


@pytest.mark.anyio
async def test_register_client_error_is_escaped_and_has_no_inline_styles(setup_db):
    cookies = await _developer_cookies()

    resp = await _register(cookies, "My Plugin", 'https://app.example/"><script>alert(1)</script>#x')

    assert resp.status_code == 400
    assert b"<script>alert(1)</script>" not in resp.content
    assert b"&lt;script&gt;" in resp.content
    assert not re.search(rb"\sstyle\s*=", resp.content, re.IGNORECASE)


@pytest.mark.anyio
async def test_register_client_requires_matching_csrf_token(setup_db):
    cookies = await _developer_cookies()

    resp = await _register(cookies, "My Plugin", "https://app.example/cb", csrf_token="wrong")

    assert resp.status_code == 403
    assert await _stored_clients() == []


@pytest.mark.anyio
@pytest.mark.parametrize("account_status", [None, "pending", "suspended", "rejected"])
async def test_register_client_requires_approved_account(account_status, setup_db):
    cookies = await _developer_cookies(account_status)

    async with _client() as c:
        resp = await c.post(
            "/api/developer/clients",
            data={"app_name": "My Plugin", "redirect_uris": "https://app.example/cb", "csrf_token": CSRF},
            cookies=cookies,
            follow_redirects=False,
        )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/developer"
    assert await _stored_clients() == []


# ---------------------------------------------------------------------------
# POST /api/developer/apply
# ---------------------------------------------------------------------------


async def _apply(cookies: dict[str, str], organization_name: str):
    async with _client() as c:
        return await c.post(
            "/api/developer/apply",
            data={"organization_name": organization_name, "csrf_token": CSRF},
            cookies=cookies,
            follow_redirects=False,
        )


async def _stored_accounts() -> list[DeveloperAccount]:
    async with get_session() as s:
        return list((await s.execute(select(DeveloperAccount))).scalars().all())


@pytest.mark.anyio
async def test_apply_creates_pending_account_with_trimmed_name(setup_db):
    cookies = await _developer_cookies(account_status=None)

    resp = await _apply(cookies, "  Eventyay Plugin  ")

    assert resp.status_code == 303
    (account,) = await _stored_accounts()
    assert account.status == "pending"
    assert account.organization_name == "Eventyay Plugin"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "organization_name, reason",
    [
        pytest.param("   ", b"Organization / project name cannot be empty.", id="blank"),
        pytest.param(
            "o" * (MAX_NAME_LENGTH + 1), b"Organization / project name must be 200 characters or fewer.", id="too-long"
        ),
        pytest.param("Acme\x00Org", b"Organization / project name contains characters that are not allowed.", id="nul"),
    ],
)
async def test_apply_rejects_invalid_organization_name(organization_name, reason, setup_db):
    cookies = await _developer_cookies(account_status=None)

    resp = await _apply(cookies, organization_name)

    assert resp.status_code == 400
    assert reason in resp.content
    assert b"Apply for Developer Access" in resp.content
    assert await _stored_accounts() == []


# ---------------------------------------------------------------------------
# /oauth/authorize with clients registered before redirect URIs were validated
# ---------------------------------------------------------------------------


async def _store_client(redirect_uri: str) -> str:
    """Insert an OAuth client directly, bypassing the dashboard validation."""
    async with get_session() as s:
        account = (await s.execute(select(DeveloperAccount))).scalars().one()
        s.add(
            OAuthClient(
                developer_account_id=account.id,
                client_id="client_legacy",
                name="Legacy",
                redirect_uris=[redirect_uri],
            )
        )
        await s.commit()
    return "client_legacy"


async def _deny(cookies: dict[str, str], client_id: str, redirect_uri: str):
    async with _client() as c:
        return await c.post(
            "/oauth/authorize",
            data={
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "state": "S",
                "code_challenge": "x",
                "code_challenge_method": "S256",
                "event_id": 1,
                "scope": "events:read",
                "action": "deny",
            },
            cookies=cookies,
            follow_redirects=False,
        )


@pytest.mark.anyio
@pytest.mark.parametrize("stored", ["not a url", "/relative/cb", "javascript:alert(1)", "https:///cb"])
async def test_authorize_refuses_stored_redirect_uri_that_is_not_an_absolute_http_url(stored, setup_db):
    cookies = await _developer_cookies()
    client_id = await _store_client(stored)

    resp = await _deny(cookies, client_id, stored)
    assert resp.status_code == 400
    assert "location" not in resp.headers

    async with _client() as c:
        resp = await c.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": stored,
                "response_type": "code",
                "code_challenge": "x",
                "code_challenge_method": "S256",
                "event": "testcon",
            },
            cookies=cookies,
            follow_redirects=False,
        )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid redirect_uri."


@pytest.mark.anyio
@pytest.mark.parametrize("stored", ["https://app.example/cb", "http://staging.internal/cb"])
async def test_authorize_still_redirects_to_stored_http_urls(stored, setup_db):
    """Plain http on a remote host is no longer accepted for new apps, but existing ones keep working."""
    cookies = await _developer_cookies()
    client_id = await _store_client(stored)

    resp = await _deny(cookies, client_id, stored)

    assert resp.status_code == 303
    assert resp.headers["location"] == f"{stored}?error=access_denied&state=S"
