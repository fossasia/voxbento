"""Tests for authentication rate limiting (Issue #561).

Covers:
- POST /register IP rate limiting
- POST /login IP rate limiting
- POST /admin/login IP rate limiting
- Requests below configured limits succeed
- Requests exceeding limits return 429 Too Many Requests
- Rate limits apply independently per client IP
- Retry-After and rate-limit headers are present on 429 responses
- HTML requests receive rendered 429.html error page
- JSON/non-HTML requests receive JSON 429 response
- GET requests to auth pages are not rate-limited
- Spoofed proxy headers (X-Forwarded-For) are ignored
- Rate limits are configurable and toggleable
"""

from __future__ import annotations

import os

os.environ["BOOTH_ACCESS_TOKEN"] = ""
os.environ["ADMIN_PASSWORD"] = "test-admin-pass"

import pytest
from httpx import ASGITransport, AsyncClient

from fastapi_app import app
from portal.auth import hash_password
from portal.config import settings
from portal.limiter import limiter


@pytest.fixture(autouse=True)
async def setup_db():
    """Set up an in-memory database for testing."""
    from portal.database import configure, dispose, init_db

    configure("sqlite+aiosqlite://")
    await init_db()
    yield
    await dispose()


@pytest.fixture(autouse=True)
def reset_limiter_state():
    """Ensure limiter is clean and default settings are restored."""
    orig_enabled = settings.rate_limit_enabled
    orig_register = settings.rate_limit_register
    orig_login = settings.rate_limit_login
    orig_admin_login = settings.rate_limit_admin_login

    settings.rate_limit_enabled = True
    limiter.reset()

    yield

    settings.rate_limit_enabled = orig_enabled
    settings.rate_limit_register = orig_register
    settings.rate_limit_login = orig_login
    settings.rate_limit_admin_login = orig_admin_login
    limiter.reset()


def _client(client_ip: str = "10.0.0.1") -> AsyncClient:
    """Helper to create an AsyncClient with a specific client IP."""
    return AsyncClient(
        transport=ASGITransport(app=app, client=(client_ip, 1234)),
        base_url="http://test",
    )


async def _create_test_user(
    email: str = "test@example.com",
    display_name: str = "Test User",
    password: str = "securepass123",
):
    from portal.database import create_user, get_session

    pw_hash = hash_password(password) if password else None
    async with get_session() as s:
        user = await create_user(
            s,
            email=email,
            display_name=display_name,
            password_hash=pw_hash,
            email_verified=True,
        )
    return user


# ---------------------------------------------------------------------------
# POST /register Rate Limiting
# ---------------------------------------------------------------------------


class TestRegisterRateLimiting:
    @pytest.mark.anyio
    async def test_register_throttles_after_limit_exceeded(self):
        settings.rate_limit_register = "3/minute"
        limiter.reset()

        async with _client(client_ip="192.168.1.10") as client:
            # 3 requests allowed (they fail validation, returning 422, but count towards rate limit)
            for i in range(3):
                resp = await client.post(
                    "/register",
                    data={"email": f"user{i}@example.com", "display_name": f"User {i}", "password": "pw"},
                )
                assert resp.status_code == 200 or resp.status_code == 422
                assert resp.status_code != 429

            # 4th request must receive 429
            resp = await client.post(
                "/register",
                data={"email": "overflow@example.com", "display_name": "Overflow", "password": "pw"},
            )
            assert resp.status_code == 429
            assert "retry-after" in resp.headers
            assert int(resp.headers["retry-after"]) > 0

    @pytest.mark.anyio
    async def test_register_rate_limit_isolated_by_ip(self):
        settings.rate_limit_register = "2/minute"
        limiter.reset()

        ip1 = "10.1.1.1"
        ip2 = "10.2.2.2"

        async with _client(client_ip=ip1) as c1, _client(client_ip=ip2) as c2:
            # Exhaust ip1 limit
            await c1.post("/register", data={"email": "a@example.com", "display_name": "A", "password": "pw"})
            await c1.post("/register", data={"email": "b@example.com", "display_name": "B", "password": "pw"})
            blocked_c1 = await c1.post(
                "/register", data={"email": "c@example.com", "display_name": "C", "password": "pw"}
            )
            assert blocked_c1.status_code == 429

            # ip2 is unaffected
            allowed_c2 = await c2.post(
                "/register",
                data={"email": "valid@example.com", "display_name": "Valid", "password": "securepassword123"},
            )
            assert allowed_c2.status_code != 429

    @pytest.mark.anyio
    async def test_get_register_page_is_not_rate_limited(self):
        settings.rate_limit_register = "2/minute"
        limiter.reset()

        async with _client(client_ip="10.1.1.5") as client:
            for _ in range(5):
                resp = await client.get("/register")
                assert resp.status_code == 200
                assert b"Create your account" in resp.content


# ---------------------------------------------------------------------------
# POST /login Rate Limiting
# ---------------------------------------------------------------------------


class TestLoginRateLimiting:
    @pytest.mark.anyio
    async def test_login_throttles_after_limit_exceeded(self):
        settings.rate_limit_login = "3/minute"
        limiter.reset()

        async with _client(client_ip="192.168.1.20") as client:
            for _ in range(3):
                resp = await client.post(
                    "/login",
                    data={"email": "test@example.com", "password": "wrongpassword"},
                )
                assert resp.status_code == 403

            # 4th attempt from same IP exceeds limit
            resp = await client.post(
                "/login",
                data={"email": "test@example.com", "password": "wrongpassword"},
            )
            assert resp.status_code == 429
            assert "retry-after" in resp.headers

    @pytest.mark.anyio
    async def test_login_rate_limit_isolated_by_ip(self):
        await _create_test_user(email="login@example.com", password="correctpass123")
        settings.rate_limit_login = "2/minute"
        limiter.reset()

        ip1 = "10.5.5.1"
        ip2 = "10.5.5.2"

        async with _client(client_ip=ip1) as c1, _client(client_ip=ip2) as c2:
            # Exhaust ip1
            await c1.post("/login", data={"email": "login@example.com", "password": "wrong"})
            await c1.post("/login", data={"email": "login@example.com", "password": "wrong"})
            blocked_c1 = await c1.post("/login", data={"email": "login@example.com", "password": "correctpass123"})
            assert blocked_c1.status_code == 429

            # ip2 can log in successfully
            allowed_c2 = await c2.post(
                "/login",
                data={"email": "login@example.com", "password": "correctpass123"},
                follow_redirects=False,
            )
            assert allowed_c2.status_code == 303
            assert allowed_c2.headers["location"] == "/account"

    @pytest.mark.anyio
    async def test_get_login_page_is_not_rate_limited(self):
        settings.rate_limit_login = "2/minute"
        limiter.reset()

        async with _client(client_ip="10.5.5.9") as client:
            for _ in range(5):
                resp = await client.get("/login")
                assert resp.status_code == 200
                assert b"Sign in" in resp.content


# ---------------------------------------------------------------------------
# POST /admin/login Rate Limiting
# ---------------------------------------------------------------------------


class TestAdminLoginRateLimiting:
    @pytest.mark.anyio
    async def test_admin_login_throttles_after_limit_exceeded(self):
        settings.rate_limit_admin_login = "3/minute"
        limiter.reset()

        async with _client(client_ip="192.168.1.30") as client:
            for _ in range(3):
                resp = await client.post(
                    "/admin/login",
                    data={"password": "wrong-admin-pass"},
                    follow_redirects=False,
                )
                assert resp.status_code == 403

            resp = await client.post(
                "/admin/login",
                data={"password": "test-admin-pass"},
                follow_redirects=False,
            )
            assert resp.status_code == 429
            assert "retry-after" in resp.headers

    @pytest.mark.anyio
    async def test_admin_login_rate_limit_isolated_by_ip(self):
        settings.rate_limit_admin_login = "2/minute"
        limiter.reset()

        ip1 = "10.9.9.1"
        ip2 = "10.9.9.2"

        async with _client(client_ip=ip1) as c1, _client(client_ip=ip2) as c2:
            await c1.post("/admin/login", data={"password": "wrong"})
            await c1.post("/admin/login", data={"password": "wrong"})
            blocked_c1 = await c1.post("/admin/login", data={"password": "test-admin-pass"})
            assert blocked_c1.status_code == 429

            allowed_c2 = await c2.post(
                "/admin/login",
                data={"password": "test-admin-pass"},
                follow_redirects=False,
            )
            assert allowed_c2.status_code == 303
            assert allowed_c2.headers["location"] == "/admin/"

    @pytest.mark.anyio
    async def test_get_admin_login_page_is_not_rate_limited(self):
        settings.rate_limit_admin_login = "2/minute"
        limiter.reset()

        async with _client(client_ip="10.9.9.5") as client:
            for _ in range(5):
                resp = await client.get("/admin/login")
                assert resp.status_code == 200
                assert b"Admin" in resp.content


# ---------------------------------------------------------------------------
# Response Formatting & Headers
# ---------------------------------------------------------------------------


class TestRateLimitResponsesAndHeaders:
    @pytest.mark.anyio
    async def test_browser_request_renders_429_html_page(self):
        settings.rate_limit_login = "1/minute"
        limiter.reset()

        async with _client(client_ip="10.8.8.1") as client:
            await client.post("/login", data={"email": "a@b.com", "password": "pw"})

            resp = await client.post(
                "/login",
                data={"email": "a@b.com", "password": "pw"},
                headers={"accept": "text/html,application/xhtml+xml"},
            )
            assert resp.status_code == 429
            assert "text/html" in resp.headers["content-type"]
            assert b"Too Many Attempts" in resp.content
            assert "retry-after" in resp.headers
            assert int(resp.headers["retry-after"]) > 0
            assert resp.headers.get("x-ratelimit-limit") == "1"
            assert resp.headers.get("x-ratelimit-remaining") == "0"
            assert "x-ratelimit-reset" in resp.headers
            assert float(resp.headers["x-ratelimit-reset"]) > 0

    @pytest.mark.anyio
    async def test_api_request_returns_json_429(self):
        settings.rate_limit_login = "1/minute"
        limiter.reset()

        async with _client(client_ip="10.8.8.2") as client:
            await client.post("/login", data={"email": "a@b.com", "password": "pw"})

            resp = await client.post(
                "/login",
                data={"email": "a@b.com", "password": "pw"},
                headers={"accept": "application/json"},
            )
            assert resp.status_code == 429
            assert "application/json" in resp.headers["content-type"]
            data = resp.json()
            assert "detail" in data
            assert "Too many requests" in data["detail"]
            assert "retry-after" in resp.headers
            assert int(resp.headers["retry-after"]) > 0
            assert resp.headers.get("x-ratelimit-limit") == "1"
            assert resp.headers.get("x-ratelimit-remaining") == "0"
            assert "x-ratelimit-reset" in resp.headers
            assert float(resp.headers["x-ratelimit-reset"]) > 0

    @pytest.mark.anyio
    async def test_all_auth_endpoints_expose_ratelimit_headers_on_429(self):
        settings.rate_limit_register = "1/minute"
        settings.rate_limit_admin_login = "1/minute"
        limiter.reset()

        # Test POST /register headers
        async with _client(client_ip="10.8.8.3") as client:
            await client.post("/register", data={"email": "u@b.com", "display_name": "U", "password": "pw"})
            resp_reg = await client.post(
                "/register",
                data={"email": "u@b.com", "display_name": "U", "password": "pw"},
                headers={"accept": "application/json"},
            )
            assert resp_reg.status_code == 429
            assert int(resp_reg.headers["retry-after"]) > 0
            assert resp_reg.headers.get("x-ratelimit-limit") == "1"
            assert resp_reg.headers.get("x-ratelimit-remaining") == "0"
            assert float(resp_reg.headers["x-ratelimit-reset"]) > 0

        # Test POST /admin/login headers
        async with _client(client_ip="10.8.8.4") as client:
            await client.post("/admin/login", data={"password": "wrong"})
            resp_admin = await client.post(
                "/admin/login",
                data={"password": "wrong"},
                headers={"accept": "application/json"},
            )
            assert resp_admin.status_code == 429
            assert int(resp_admin.headers["retry-after"]) > 0
            assert resp_admin.headers.get("x-ratelimit-limit") == "1"
            assert resp_admin.headers.get("x-ratelimit-remaining") == "0"
            assert float(resp_admin.headers["x-ratelimit-reset"]) > 0

    @pytest.mark.anyio
    async def test_spoofed_forwarded_headers_are_ignored(self):
        """Verify that untrusted client-supplied X-Forwarded-For headers cannot bypass

        rate limits because SlowAPI's get_remote_address resolves request.client.host.
        """
        settings.rate_limit_login = "2/minute"
        limiter.reset()

        async with _client(client_ip="172.16.0.5") as client:
            # Client tries to bypass limit by rotating X-Forwarded-For header
            await client.post(
                "/login", data={"email": "a@b.com", "password": "pw"}, headers={"x-forwarded-for": "1.1.1.1"}
            )
            await client.post(
                "/login", data={"email": "a@b.com", "password": "pw"}, headers={"x-forwarded-for": "2.2.2.2"}
            )

            # Third request is blocked despite forged header
            resp = await client.post(
                "/login",
                data={"email": "a@b.com", "password": "pw"},
                headers={"x-forwarded-for": "3.3.3.3"},
            )
            assert resp.status_code == 429

    @pytest.mark.anyio
    async def test_trusted_proxy_client_ip_separation(self):
        """Verify rate limits apply independently once the trusted proxy / ASGI layer

        has resolved distinct client IPs into request.client.host.
        """
        settings.rate_limit_login = "1/minute"
        limiter.reset()

        proxy_client1 = "203.0.113.10"
        proxy_client2 = "203.0.113.20"

        async with _client(client_ip=proxy_client1) as c1, _client(client_ip=proxy_client2) as c2:
            # First client exhausts limit
            await c1.post("/login", data={"email": "a@b.com", "password": "pw"})
            blocked_c1 = await c1.post("/login", data={"email": "a@b.com", "password": "pw"})
            assert blocked_c1.status_code == 429

            # Second client behind the proxy is not blocked
            resp_c2 = await c2.post("/login", data={"email": "a@b.com", "password": "pw"})
            assert resp_c2.status_code != 429

    @pytest.mark.anyio
    async def test_rate_limiting_can_be_disabled_via_settings(self):
        settings.rate_limit_login = "1/minute"
        settings.rate_limit_register = "1/minute"
        settings.rate_limit_admin_login = "1/minute"
        settings.rate_limit_enabled = False
        limiter.reset()

        async with _client(client_ip="172.16.0.10") as client:
            # Multiple requests to /login succeed without 429
            for i in range(5):
                resp = await client.post("/login", data={"email": f"disabled_{i}@example.com", "password": "pw"})
                assert resp.status_code != 429

            # Multiple requests to /register succeed without 429
            for i in range(5):
                resp = await client.post(
                    "/register", data={"email": f"reg_{i}@example.com", "display_name": "U", "password": "pw"}
                )
                assert resp.status_code != 429

            # Multiple requests to /admin/login succeed without 429
            for _ in range(5):
                resp = await client.post("/admin/login", data={"password": "wrong"})
                assert resp.status_code != 429

        # Re-enabling enforces rate limiting again
        settings.rate_limit_enabled = True
        limiter.reset()

        async with _client(client_ip="172.16.0.10") as client:
            await client.post("/login", data={"email": "new_attempt@example.com", "password": "pw"})
            resp_blocked = await client.post("/login", data={"email": "new_attempt2@example.com", "password": "pw"})
            assert resp_blocked.status_code == 429
