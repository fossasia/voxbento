from __future__ import annotations

import time
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from portal.config import settings

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


class AppLimiter(Limiter):
    """SlowAPI Limiter that reflects dynamic settings.rate_limit_enabled."""

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


def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> Response:
    """Exception handler for slowapi RateLimitExceeded.

    Computes standard rate limit headers (Retry-After, X-RateLimit-*) and returns
    either an HTML 429 page (if client accepts text/html) or a JSON 429 response.
    """
    view_limit = getattr(request.state, "view_rate_limit", None)
    headers: dict[str, str] = {}
    if view_limit and hasattr(request.app.state, "limiter"):
        try:
            window_stats = request.app.state.limiter.limiter.get_window_stats(view_limit[0], *view_limit[1])
            reset_in = 1 + window_stats[0]
            retry_after = max(1, int(reset_in - time.time()))
            headers["Retry-After"] = str(retry_after)
            headers["X-RateLimit-Limit"] = str(view_limit[0].amount)
            headers["X-RateLimit-Remaining"] = "0"
            headers["X-RateLimit-Reset"] = str(reset_in)
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
