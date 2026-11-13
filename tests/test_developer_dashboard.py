"""Tests for the developer dashboard form endpoints.

Covers:
- Input validation when applying for developer access
- Input validation when registering an OAuth client (app name, redirect URIs)
- CSRF and account-status guards on client registration
"""

from __future__ import annotations

import hashlib
import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""

import re
from html.parser import HTMLParser

import pytest
from sqlalchemy import select

from portal.auth import create_user_token, hash_password
from portal.config import settings
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
        ("", b"Enter at least one redirect URI."),
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
        pytest.param("", b"App name cannot be empty.", id="empty"),
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
        pytest.param("", b"Organization / project name cannot be empty.", id="empty"),
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
@pytest.mark.parametrize(
    "stored",
    [
        "not a url",
        "/relative/cb",
        "javascript:alert(1)",
        "https:///cb",
        "https://app.example:70000/cb",
        "https://app.example:notaport/cb",
    ],
)
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


@pytest.mark.anyio
async def test_developer_dashboard_apps_table_has_four_headers_and_cells(setup_db):
    """Ensure the Apps table header has 4 columns matching the 4 row cells."""
    cookies = await _developer_cookies()
    reg_resp = await _register(cookies, "Test App", "https://app.example/cb")
    assert reg_resp.status_code == 200

    async with _client() as c:
        resp = await c.get("/developer", cookies=cookies)
    assert resp.status_code == 200

    # Scope strictly to the Apps table
    match = re.search(r'<table class="data-table dev-table">.*?</table>', resp.text, re.DOTALL)
    assert match is not None, "Apps table was not rendered"
    apps_table_html = match.group(0)
    assert "dev-client-name" in apps_table_html

    # Verify header has 4 columns matching data row cells
    thead_match = re.search(r'<thead>(.*?)</thead>', apps_table_html, re.DOTALL)
    assert thead_match is not None
    headers = re.findall(r'<th\b[^>]*>(.*?)</th>', thead_match.group(1), re.DOTALL)
    header_texts = [re.sub(r'<[^>]+>', '', h).strip() for h in headers]
    assert header_texts == ["Name", "Client ID", "Status", "Actions"]
    assert len(headers) == 4

    tbody_match = re.search(r'<tbody>(.*?)</tbody>', apps_table_html, re.DOTALL)
    assert tbody_match is not None
    rows = re.findall(r'<tr class="dev-row">(.*?)</tr>', tbody_match.group(1), re.DOTALL)
    assert len(rows) == 1
    cells = re.findall(r'<td\b[^>]*>', rows[0])
    assert len(cells) == 4


@pytest.mark.anyio
@pytest.mark.parametrize("forwarded_proto, secure", [("https", True), (None, False)])
async def test_dashboard_csrf_cookie_secure_flag_follows_scheme(setup_db, monkeypatch, forwarded_proto, secure):
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    cookies = {"user_token": (await _developer_cookies())["user_token"]}
    headers = {"x-forwarded-proto": forwarded_proto} if forwarded_proto else {}

    async with _client() as c:
        resp = await c.get("/developer", cookies=cookies, headers=headers)

    cookie = next(v for k, v in resp.headers.multi_items() if k == "set-cookie" and v.startswith("dashboard_csrf="))
    assert ("Secure" in cookie) is secure
    assert "HttpOnly" in cookie


# ---------------------------------------------------------------------------
# Dashboard dialogs (#1243)
# ---------------------------------------------------------------------------


class _InlineScripts(HTMLParser):
    """Collect every script without a `src`, whatever its case or spacing."""

    def __init__(self) -> None:
        super().__init__()
        self.found: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "script" and "src" not in attributes:
            self.found.append(attributes)


def _assert_labelled_dialog(html: str, dialog_id: str, title: str) -> None:
    # A div never matches the dialog-only :not([open]) rule in admin.css, so the popups must be dialogs.
    tag = re.search(rf'<(\w+) id="{dialog_id}"[^>]*>', html)
    assert tag is not None and tag.group(1) == "dialog", dialog_id
    assert " open" not in tag.group(0), "showModal() opens it; an open attribute would make it non-modal"
    label = re.search(r'aria-labelledby="([^"]+)"', tag.group(0))
    assert label is not None, dialog_id
    assert re.search(rf'id="{label.group(1)}"[^>]*>{title}<', html), dialog_id


@pytest.mark.anyio
async def test_dashboard_create_app_popup_is_a_dialog_driven_by_developer_js(setup_db):
    cookies = await _developer_cookies()
    assert (await _register(cookies, "My Plugin", "https://app.example/cb")).status_code == 200
    (client,) = await _stored_clients()

    async with _client() as c:
        resp = await c.get("/developer", cookies=cookies)

    assert resp.status_code == 200
    _assert_labelled_dialog(resp.text, "modal-create-app", "Create OAuth Client")
    assert 'id="modal-new-secret"' not in resp.text
    assert re.search(r'<script type="module" src="[^"]*/js/developer\.js\?v=', resp.text)
    scripts = _InlineScripts()
    scripts.feed(resp.text)
    assert scripts.found == [], "no inline scripts"
    assert "onclick=" not in resp.text
    assert re.search(rf'<button type="button" class="copy-wrapper" data-copy-text="{client.client_id}">', resp.text)


@pytest.mark.anyio
async def test_new_secret_popup_is_a_dialog_with_copy_buttons(setup_db):
    cookies = await _developer_cookies()

    resp = await _register(cookies, "My Plugin", "https://app.example/cb")

    assert resp.status_code == 200
    _assert_labelled_dialog(resp.text, "modal-new-secret", "App Created Successfully")
    (client,) = await _stored_clients()
    secret = re.search(r'data-copy-text="(secret_[^"]+)"', resp.text).group(1)
    assert hashlib.sha256(secret.encode()).hexdigest() == client.client_secret_hash
    assert re.search(rf'<button type="button" class="[^"]*copy-wrapper" data-copy-text="{client.client_id}"', resp.text)
    assert "onclick=" not in resp.text
