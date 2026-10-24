from __future__ import annotations

import hashlib
import hmac
import math
import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from limits import parse as parse_limit
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from portal.config import settings

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


class AppLimiter(Limiter):
    """SlowAPI Limiter that reflects dynamic settings.rate_limit_enabled."""

    def __init__(self, *args, **kwargs) -> None:
        if "enabled" not in kwargs:
            kwargs["enabled"] = settings.rate_limit_enabled
        super().__init__(*args, **kwargs)

    @property
    def enabled(self) -> bool:
        return settings.rate_limit_enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        settings.rate_limit_enabled = value


limiter = AppLimiter(
    key_func=get_remote_address,
    default_limits=[],
    enabled=settings.rate_limit_enabled,
)


def normalize_account_identifier(identifier: str) -> str:
    """Normalize account identifier (email/username) consistently.

    Strips leading and trailing whitespace and converts to lowercase.
    """
    return identifier.strip().lower()


def hash_account_key(identifier: str, action: str = "login") -> str:
    """Derive a privacy-safe deterministic rate-limit key for an account identifier.

    Uses HMAC-SHA256 with effective_jwt_secret so raw emails are not stored
    in memory or exposed in telemetry/rate-limit keys.
    """
    norm = normalize_account_identifier(identifier)
    secret = settings.effective_jwt_secret.encode("utf-8")
    h = hmac.new(secret, f"{action}:{norm}".encode("utf-8"), hashlib.sha256).hexdigest()
    return f"account:{action}:{h}"


def build_rate_limit_response(
    request: Request,
    view_limit: tuple[Any, list[str]] | None = None,
) -> Response:
    """Construct a 429 Response with standard headers and format (HTML or JSON).

    Computes standard rate limit headers (Retry-After, X-RateLimit-*) based on the
    actual limits library contract:
    - window_stats[0] (or reset_time): absolute Unix timestamp (in seconds since epoch)
      when the current window resets.
    - window_stats[1] (or remaining): remaining allowed requests in this window.

    Headers:
    - X-RateLimit-Limit: configured request limit amount
    - X-RateLimit-Remaining: remaining requests in current window
    - X-RateLimit-Reset: deterministic absolute Unix timestamp (integer seconds)
    - Retry-After: non-negative integer seconds until window reset
    """

    headers: dict[str, str] = {}
    limiter_obj = getattr(getattr(request, "app", None), "state", None)
    app_limiter = getattr(limiter_obj, "limiter", limiter)

    if view_limit and hasattr(app_limiter, "limiter"):
        try:
            window_stats = app_limiter.limiter.get_window_stats(view_limit[0], *view_limit[1])
            reset_time = float(window_stats[0])
            now = time.time()
            reset_at = int(math.ceil(reset_time))
            retry_after = max(1, int(math.ceil(reset_time - now)))
            headers["Retry-After"] = str(retry_after)
            headers["X-RateLimit-Limit"] = str(view_limit[0].amount)
            headers["X-RateLimit-Remaining"] = str(max(0, int(window_stats[1])))
            headers["X-RateLimit-Reset"] = str(reset_at)
        except Exception:
            pass

    headers.setdefault("Retry-After", "60")

    detail = "Too many requests. Please try again later."
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse(
            request=request,
            name="429.html",
            context={"request": request, "detail": detail},
            status_code=429,
            headers=headers,
        )
    return JSONResponse(
        content={"detail": detail},
        status_code=429,
        headers=headers,
    )


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded | None = None) -> Response:
    """Exception handler for slowapi RateLimitExceeded."""
    view_limit = getattr(request.state, "view_rate_limit", None)
    return build_rate_limit_response(request, view_limit)


def check_rate_limit_account(
    request: Request,
    identifier: str,
    action: str = "login",
    limit_str: str | None = None,
) -> Response | None:
    """Check account-level rate limit for sensitive actions like /login.

    Returns a 429 Response if rate limit is exceeded, or None if allowed.
    """
    if not settings.rate_limit_enabled or not limiter.enabled:
        return None

    norm = normalize_account_identifier(identifier)
    if not norm:
        return None

    if limit_str is None:
        limit_str = settings.rate_limit_login_account

    item = parse_limit(limit_str)
    account_key = hash_account_key(norm, action=action)

    if not limiter.limiter.hit(item, account_key):
        request.state.view_rate_limit = (item, [account_key])
        return build_rate_limit_response(request, (item, [account_key]))

    return None
