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
from pydantic import ValidationError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from fastapi_app import app
from portal.auth import hash_password
from portal.config import Settings, settings
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
    orig_login_account = settings.rate_limit_login_account
    orig_admin_login = settings.rate_limit_admin_login
    orig_forwarded_allow_ips = settings.forwarded_allow_ips

    settings.rate_limit_enabled = True
    limiter.reset()

    yield

    settings.rate_limit_enabled = orig_enabled
    settings.rate_limit_register = orig_register
    settings.rate_limit_login = orig_login
    settings.rate_limit_login_account = orig_login_account
    settings.rate_limit_admin_login = orig_admin_login
    settings.forwarded_allow_ips = orig_forwarded_allow_ips
    limiter.reset()


def _client(client_ip: str = "10.0.0.1") -> AsyncClient:
    """Helper to create an AsyncClient with a specific client IP."""
    return AsyncClient(
        transport=ASGITransport(app=app, client=(client_ip, 1234)),
        base_url="http://test",
    )


def _proxied_client(
    peer_ip: str = "172.28.0.1",
    trusted_hosts: str | None = None,
) -> AsyncClient:
    """Helper to create an AsyncClient wrapped with Uvicorn's ProxyHeadersMiddleware.

    Matches production Uvicorn startup configuration with forwarded_allow_ips.
    """
    hosts = trusted_hosts if trusted_hosts is not None else settings.forwarded_allow_ips
    middleware = ProxyHeadersMiddleware(app, trusted_hosts=hosts)
    return AsyncClient(
        transport=ASGITransport(app=middleware, client=(peer_ip, 54321)),
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

    @pytest.mark.anyio
    async def test_app_limiter_init_preserves_rate_limit_enabled_false(self):
        """Ensure AppLimiter instantiation does not overwrite settings.rate_limit_enabled=False."""
        from slowapi.util import get_remote_address

        from portal.limiter import AppLimiter

        settings.rate_limit_enabled = False
        test_lim = AppLimiter(key_func=get_remote_address, default_limits=[])
        assert settings.rate_limit_enabled is False
        assert test_lim.enabled is False

        # Verify dynamic property setter
        test_lim.enabled = True
        assert settings.rate_limit_enabled is True
        assert test_lim.enabled is True

    @pytest.mark.anyio
    async def test_deterministic_rate_limit_headers(self, monkeypatch):
        """Verify deterministic calculation of X-RateLimit-Reset, Retry-After, and remaining.

        Contract:
        - window_stats[0] is the absolute Unix timestamp (reset_time) when the window resets.
        - X-RateLimit-Reset must equal the deterministic integer timestamp: ceil(reset_time).
        - Retry-After must equal ceil(reset_time - current_time).
        - X-RateLimit-Remaining must be 0 when rate limited.
        - X-RateLimit-Limit must match the configured limit amount.

        Regression check:
        The previous buggy arithmetic did:
            reset_in = 1 + window_stats[0]
            retry_after = max(1, int(reset_in - time.time()))
        which incorrectly set X-RateLimit-Reset to (1 + reset_time) and Retry-After to (1 + delta),
        adding an unwarranted 1-second delay (e.g. 1700000061 instead of 1700000060, and 61 instead of 60).
        """
        import time

        from fastapi import Request
        from limits import parse as parse_limit

        from portal.limiter import build_rate_limit_response

        fixed_now = 1700000000.0
        known_reset = 1700000060.0  # exactly 60 seconds after fixed_now
        monkeypatch.setattr(time, "time", lambda: fixed_now)

        item = parse_limit("10/minute")
        key = "test-header-key"

        class MockLimiterBackend:
            def get_window_stats(self, *args, **kwargs):
                return (known_reset, 0)

        class MockAppLimiter:
            limiter = MockLimiterBackend()

        class MockState:
            limiter = MockAppLimiter()

        class MockApp:
            state = MockState()

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/login",
            "headers": [(b"accept", b"application/json")],
            "app": MockApp(),
        }
        req = Request(scope)

        resp = build_rate_limit_response(req, view_limit=(item, [key]))

        assert resp.status_code == 429
        # Deterministic exact assertions
        assert resp.headers["x-ratelimit-reset"] == "1700000060"
        assert resp.headers["retry-after"] == "60"
        assert resp.headers["x-ratelimit-remaining"] == "0"
        assert resp.headers["x-ratelimit-limit"] == "10"

        # Sub-second fractional ceiling test:
        # At 1700000000.2, remaining time until 1700000060.0 is 59.8s.
        # math.ceil ensures Retry-After is 60 (safe for client retry), not truncated to 59.
        monkeypatch.setattr(time, "time", lambda: 1700000000.2)
        resp_fractional = build_rate_limit_response(req, view_limit=(item, [key]))
        assert resp_fractional.headers["x-ratelimit-reset"] == "1700000060"
        assert resp_fractional.headers["retry-after"] == "60"

    @pytest.mark.anyio
    async def test_login_no_hidden_legacy_rate_limit(self):
        """Ensure /login relies on configurable SlowAPI and account limits without a hardcoded 10/hour limit."""
        settings.rate_limit_login = "20/minute"
        settings.rate_limit_login_account = "20/minute"
        limiter.reset()

        # 12 requests with same email under configured limits (20/min) should not trigger 429
        async with _client(client_ip="10.8.8.99") as client:
            for _ in range(12):
                resp = await client.post(
                    "/login",
                    data={"email": "same_user@example.com", "password": "wrongpassword"},
                )
                assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Combined IP + Account Rate Limiting Policy (Issue 1)
# ---------------------------------------------------------------------------


class TestCombinedLoginRateLimiting:
    @pytest.mark.anyio
    async def test_login_repeated_attempts_same_ip_and_account(self):
        """Repeated login failures for the same account from the same IP trigger account rate limit."""
        settings.rate_limit_login = "10/minute"
        settings.rate_limit_login_account = "3/minute"
        limiter.reset()

        async with _client(client_ip="192.168.10.1") as client:
            for _ in range(3):
                resp = await client.post(
                    "/login",
                    data={"email": "victim@example.com", "password": "badpassword"},
                )
                assert resp.status_code == 403

            # 4th attempt exceeds the 3/minute account limit
            resp_blocked = await client.post(
                "/login",
                data={"email": "victim@example.com", "password": "badpassword"},
            )
            assert resp_blocked.status_code == 429
            assert resp_blocked.headers.get("x-ratelimit-limit") == "3"
            assert resp_blocked.headers.get("x-ratelimit-remaining") == "0"
            assert int(resp_blocked.headers["retry-after"]) > 0
            assert int(resp_blocked.headers["x-ratelimit-reset"]) > 0

    @pytest.mark.anyio
    async def test_login_distributed_ips_against_same_account(self):
        """Account rate limit protects against distributed brute force across multiple IP addresses."""
        settings.rate_limit_login = "10/minute"
        settings.rate_limit_login_account = "3/minute"
        limiter.reset()

        # Attacker distributes 3 attempts from IP 1
        async with _client(client_ip="192.168.20.1") as c1:
            for _ in range(3):
                resp = await c1.post(
                    "/login",
                    data={"email": "targeted_user@example.com", "password": "wrong"},
                )
                assert resp.status_code == 403

        # Attacker rotates to a fresh IP 2; the account limit is still exceeded!
        async with _client(client_ip="192.168.20.2") as c2:
            resp_blocked = await c2.post(
                "/login",
                data={"email": "targeted_user@example.com", "password": "wrong"},
            )
            assert resp_blocked.status_code == 429
            assert resp_blocked.headers.get("x-ratelimit-limit") == "3"

    @pytest.mark.anyio
    async def test_login_multiple_accounts_behind_same_ip(self):
        """IP rate limit prevents flooding/resource exhaustion when an attacker attempts many accounts from one IP."""
        settings.rate_limit_login = "3/minute"
        settings.rate_limit_login_account = "5/minute"
        limiter.reset()

        async with _client(client_ip="192.168.30.1") as client:
            # 3 attempts across 3 different accounts consume the IP bucket
            for i in range(3):
                resp = await client.post(
                    "/login",
                    data={"email": f"user_{i}@example.com", "password": "wrong"},
                )
                assert resp.status_code == 403

            # 4th attempt with a brand-new account is blocked by the IP limit
            resp_blocked = await client.post(
                "/login",
                data={"email": "brand_new_user@example.com", "password": "wrong"},
            )
            assert resp_blocked.status_code == 429
            assert resp_blocked.headers.get("x-ratelimit-limit") == "3"

    @pytest.mark.anyio
    async def test_login_account_identifier_normalization(self):
        """Account identifiers are normalized (trimmed, lowercased) and hashed with HMAC for privacy."""
        from portal.limiter import hash_account_key, normalize_account_identifier

        settings.rate_limit_login = "10/minute"
        settings.rate_limit_login_account = "2/minute"
        limiter.reset()

        # Verify normalization utility
        assert normalize_account_identifier("  User.Test@Example.COM  ") == "user.test@example.com"
        assert hash_account_key("  User.Test@Example.COM  ") == hash_account_key("user.test@example.com")
        # Ensure raw email is not stored in key
        assert "user.test@example.com" not in hash_account_key("user.test@example.com")

        async with _client(client_ip="192.168.40.1") as client:
            resp1 = await client.post(
                "/login",
                data={"email": "user.test@example.com", "password": "wrong"},
            )
            assert resp1.status_code == 403

            resp2 = await client.post(
                "/login",
                data={"email": "  USER.TEST@EXAMPLE.COM  ", "password": "wrong"},
            )
            assert resp2.status_code == 403

            # 3rd attempt with mixed case/whitespace hits the 2/minute account limit
            resp3 = await client.post(
                "/login",
                data={"email": "User.Test@Example.Com", "password": "wrong"},
            )
            assert resp3.status_code == 429

    @pytest.mark.anyio
    async def test_login_rate_limiting_disabled_bypasses_both_ip_and_account(self):
        """When RATE_LIMIT_ENABLED=false, both IP and account limits are disabled."""
        settings.rate_limit_login = "2/minute"
        settings.rate_limit_login_account = "2/minute"
        settings.rate_limit_enabled = False
        limiter.reset()

        async with _client(client_ip="192.168.50.1") as client:
            for _ in range(6):
                resp = await client.post(
                    "/login",
                    data={"email": "disabled_test@example.com", "password": "wrong"},
                )
                assert resp.status_code == 403

    @pytest.mark.anyio
    async def test_login_account_rate_limit_response_semantics_html_and_json(self):
        """Account rate limit produces consistent 429 response semantics matching IP rate limiting."""
        settings.rate_limit_login = "10/minute"
        settings.rate_limit_login_account = "1/minute"
        limiter.reset()

        async with _client(client_ip="192.168.60.1") as client:
            # 1st attempt consumes account limit
            await client.post(
                "/login",
                data={"email": "semantics@example.com", "password": "wrong"},
            )

            # HTML client receives 429 HTML page with standard headers
            resp_html = await client.post(
                "/login",
                data={"email": "semantics@example.com", "password": "wrong"},
                headers={"accept": "text/html,application/xhtml+xml"},
            )
            assert resp_html.status_code == 429
            assert "text/html" in resp_html.headers["content-type"]
            assert b"Too Many Attempts" in resp_html.content
            assert resp_html.headers.get("x-ratelimit-limit") == "1"
            assert resp_html.headers.get("x-ratelimit-remaining") == "0"
            assert int(resp_html.headers["retry-after"]) > 0
            assert int(resp_html.headers["x-ratelimit-reset"]) > 0

            # JSON client receives JSON 429 with standard headers
            resp_json = await client.post(
                "/login",
                data={"email": "semantics@example.com", "password": "wrong"},
                headers={"accept": "application/json"},
            )
            assert resp_json.status_code == 429
            assert "application/json" in resp_json.headers["content-type"]
            data = resp_json.json()
            assert "detail" in data
            assert resp_json.headers.get("x-ratelimit-limit") == "1"
            assert resp_json.headers.get("x-ratelimit-remaining") == "0"
            assert int(resp_json.headers["retry-after"]) > 0
            assert int(resp_json.headers["x-ratelimit-reset"]) > 0


# ---------------------------------------------------------------------------
# In-Process Limiter / Multi-Worker Storage Verification (Issue 3)
# ---------------------------------------------------------------------------


class TestProcessLocalLimiter:
    @pytest.mark.anyio
    async def test_limiter_uses_process_local_memory_storage(self):
        """Verify that SlowAPI uses in-process MemoryStorage by default.

        This documents and verifies that rate-limit counters are process-local:
        separate worker processes or container replicas do not share state.
        """
        from limits.storage import MemoryStorage

        storage = limiter.limiter.storage
        assert isinstance(storage, MemoryStorage)

        # Demonstrate that independent memory storage instances do not share state
        isolated_storage = MemoryStorage()
        test_key = "process_test_key"
        storage.incr(test_key, 60, 1)
        assert storage.get(test_key) == 1
        assert isolated_storage.get(test_key) == 0  # Separate process/instance sees 0


# ---------------------------------------------------------------------------
# Production Proxy Headers Middleware & Client IP Security Coverage
# ---------------------------------------------------------------------------


class TestProxyHeadersMiddlewareAndRateLimiting:
    @pytest.mark.anyio
    async def test_trusted_caddy_gateway_propagates_client_ip_to_independent_buckets(self):
        """Requests from the trusted Docker bridge gateway (172.28.0.1) have X-Forwarded-For

        trusted by Uvicorn's ProxyHeadersMiddleware, populating request.client.host so that
        distinct clients behind Caddy receive independent rate-limiting buckets.
        """
        settings.rate_limit_login = "2/minute"
        settings.rate_limit_login_account = "10/minute"
        limiter.reset()

        gateway_ip = "172.28.0.1"
        client_a_ip = "203.0.113.10"
        client_b_ip = "203.0.113.20"

        async with _proxied_client(peer_ip=gateway_ip) as client:
            # Client A sends 2 requests to reach the 2/minute IP limit
            r1 = await client.post(
                "/login",
                data={"email": "client_a1@example.com", "password": "wrong"},
                headers={"x-forwarded-for": client_a_ip},
            )
            assert r1.status_code == 403

            r2 = await client.post(
                "/login",
                data={"email": "client_a2@example.com", "password": "wrong"},
                headers={"x-forwarded-for": client_a_ip},
            )
            assert r2.status_code == 403

            # Client A 3rd request exceeds IP rate limit -> 429
            r3 = await client.post(
                "/login",
                data={"email": "client_a3@example.com", "password": "wrong"},
                headers={"x-forwarded-for": client_a_ip},
            )
            assert r3.status_code == 429

            # Client B behind the same Caddy gateway with distinct client IP
            # must NOT be blocked (has an independent bucket)
            r_b = await client.post(
                "/login",
                data={"email": "client_b@example.com", "password": "wrong"},
                headers={"x-forwarded-for": client_b_ip},
            )
            assert r_b.status_code == 403
            assert r_b.status_code != 429

    @pytest.mark.anyio
    async def test_untrusted_direct_internet_client_cannot_spoof_x_forwarded_for(self):
        """When an external client connects directly to published port 8000 without Caddy,

        its peer IP (e.g. 198.51.100.77) is NOT trusted by FORWARDED_ALLOW_IPS (127.0.0.1,172.28.0.1).
        ProxyHeadersMiddleware ignores the client's X-Forwarded-For header, request.client.host
        remains the direct peer IP, and rotating headers cannot evade rate limiting.
        """
        settings.rate_limit_login = "2/minute"
        settings.rate_limit_login_account = "10/minute"
        limiter.reset()

        direct_attacker_ip = "198.51.100.77"

        async with _proxied_client(peer_ip=direct_attacker_ip) as client:
            # Attacker sends 1st request with spoofed IP
            r1 = await client.post(
                "/login",
                data={"email": "attacker1@example.com", "password": "wrong"},
                headers={"x-forwarded-for": "1.1.1.1"},
            )
            assert r1.status_code == 403

            # Attacker sends 2nd request with different spoofed IP
            r2 = await client.post(
                "/login",
                data={"email": "attacker2@example.com", "password": "wrong"},
                headers={"x-forwarded-for": "2.2.2.2"},
            )
            assert r2.status_code == 403

            # Attacker sends 3rd request with yet another spoofed IP.
            # Because 198.51.100.77 is not in FORWARDED_ALLOW_IPS, Uvicorn ignores
            # X-Forwarded-For; SlowAPI keys all 3 requests to 198.51.100.77 and blocks.
            r3 = await client.post(
                "/login",
                data={"email": "attacker3@example.com", "password": "wrong"},
                headers={"x-forwarded-for": "3.3.3.3"},
            )
            assert r3.status_code == 429

    @pytest.mark.anyio
    async def test_multi_hop_forwarded_for_resolves_to_real_client(self):
        """When a trusted proxy forwards an X-Forwarded-For chain containing upstream hops,

        ProxyHeadersMiddleware inspects in reverse order and selects the first untrusted host
        as the real client IP, ignoring spoofed upstream prefixes.
        """
        settings.rate_limit_login = "1/minute"
        settings.rate_limit_login_account = "10/minute"
        limiter.reset()

        gateway_ip = "172.28.0.1"
        real_client_ip = "203.0.113.88"
        fake_upstream_ip = "198.51.100.1"

        async with _proxied_client(peer_ip=gateway_ip) as client:
            # 1st request consumes limit for real_client_ip
            r1 = await client.post(
                "/login",
                data={"email": "multihop1@example.com", "password": "wrong"},
                headers={"x-forwarded-for": f"{fake_upstream_ip}, {real_client_ip}"},
            )
            assert r1.status_code == 403

            # 2nd request with same real_client_ip but different spoofed upstream is blocked
            r2 = await client.post(
                "/login",
                data={"email": "multihop2@example.com", "password": "wrong"},
                headers={"x-forwarded-for": f"10.0.0.99, {real_client_ip}"},
            )
            assert r2.status_code == 429

    @pytest.mark.anyio
    async def test_loopback_proxy_is_trusted_for_host_deployments(self):
        """Verify that 127.0.0.1 is trusted in FORWARDED_ALLOW_IPS for native host reverse proxies."""
        settings.rate_limit_login = "1/minute"
        limiter.reset()

        async with _proxied_client(peer_ip="127.0.0.1") as client:
            r1 = await client.post(
                "/login",
                data={"email": "loopback1@example.com", "password": "wrong"},
                headers={"x-forwarded-for": "198.51.100.10"},
            )
            assert r1.status_code == 403

            # Same client IP through 127.0.0.1 hits limit on 2nd attempt
            r2 = await client.post(
                "/login",
                data={"email": "loopback2@example.com", "password": "wrong"},
                headers={"x-forwarded-for": "198.51.100.10"},
            )
            assert r2.status_code == 429

            # Different client IP through 127.0.0.1 is not blocked
            r3 = await client.post(
                "/login",
                data={"email": "loopback3@example.com", "password": "wrong"},
                headers={"x-forwarded-for": "198.51.100.20"},
            )
            assert r3.status_code == 403

    @pytest.mark.anyio
    async def test_register_and_admin_login_rate_limiting_with_proxy_middleware(self):
        """Verify POST /register and POST /admin/login rate limits work correctly through

        the ProxyHeadersMiddleware boundary.
        """
        settings.rate_limit_register = "1/minute"
        settings.rate_limit_admin_login = "1/minute"
        limiter.reset()

        gateway_ip = "172.28.0.1"

        async with _proxied_client(peer_ip=gateway_ip) as client:
            # /register for client 1
            r_reg1 = await client.post(
                "/register",
                data={
                    "email": "reg1@example.com",
                    "display_name": "R1",
                    "password": "pw",
                    "password_confirm": "pw",
                },
                headers={"x-forwarded-for": "203.0.113.1"},
            )
            assert r_reg1.status_code in (200, 303)
            assert r_reg1.status_code != 429

            # /register for client 1 second time -> 429
            r_reg2 = await client.post(
                "/register",
                data={
                    "email": "reg2@example.com",
                    "display_name": "R2",
                    "password": "pw",
                    "password_confirm": "pw",
                },
                headers={"x-forwarded-for": "203.0.113.1"},
            )
            assert r_reg2.status_code == 429

            # /register for client 2 -> succeeds (independent bucket)
            r_reg3 = await client.post(
                "/register",
                data={
                    "email": "reg3@example.com",
                    "display_name": "R3",
                    "password": "pw",
                    "password_confirm": "pw",
                },
                headers={"x-forwarded-for": "203.0.113.2"},
            )
            assert r_reg3.status_code in (200, 303)
            assert r_reg3.status_code != 429

            # /admin/login for client 3
            r_adm1 = await client.post(
                "/admin/login",
                data={"password": "bad"},
                headers={"x-forwarded-for": "203.0.113.3"},
            )
            assert r_adm1.status_code == 403
            assert r_adm1.status_code != 429

            # /admin/login for client 3 second time -> 429
            r_adm2 = await client.post(
                "/admin/login",
                data={"password": "bad"},
                headers={"x-forwarded-for": "203.0.113.3"},
            )
            assert r_adm2.status_code == 429

            # /admin/login for client 4 -> 403 (independent bucket, not 429)
            r_adm3 = await client.post(
                "/admin/login",
                data={"password": "bad"},
                headers={"x-forwarded-for": "203.0.113.4"},
            )
            assert r_adm3.status_code == 403
            assert r_adm3.status_code != 429

    @pytest.mark.anyio
    async def test_forwarded_allow_ips_wildcard_rejected_by_settings(self):
        """Settings explicitly rejects wildcard '*' for forwarded_allow_ips to prevent

        direct external clients from spoofing X-Forwarded-For against published port 8000.
        """
        # Valid explicit configurations succeed
        valid_s1 = Settings(forwarded_allow_ips="127.0.0.1,172.28.0.1")
        assert valid_s1.forwarded_allow_ips == "127.0.0.1,172.28.0.1"

        valid_s2 = Settings(forwarded_allow_ips="127.0.0.1,10.0.0.0/8")
        assert "10.0.0.0/8" in valid_s2.forwarded_allow_ips

        # Standalone wildcard is rejected
        with pytest.raises(ValidationError, match="Wildcard '\\*' is forbidden"):
            Settings(forwarded_allow_ips="*")

        # Wildcard within list is rejected
        with pytest.raises(ValidationError, match="Wildcard '\\*' is forbidden"):
            Settings(forwarded_allow_ips="127.0.0.1, *")
