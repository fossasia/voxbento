from __future__ import annotations

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from portal.config import settings

# No includeSubDomains: that would force HTTPS on every sibling subdomain, including ones that only speak HTTP.
_HSTS = "max-age=31536000"
# The /embed/ route builds its own configurable frame-ancestors policy.
_EMBED_PREFIX = "/embed/"


def forwarded_https(headers: Headers) -> bool:
    """Whether a proxy says the client used HTTPS.

    Reads the first (client-facing) value of ``X-Forwarded-Proto``, so ``HTTPS`` and a
    ``https, http`` chain from several proxies are handled.
    """
    return headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"


def apply_security_headers(headers: MutableHeaders, *, path: str, is_https: bool) -> None:
    """Add the baseline security headers, leaving any header the route already set untouched.

    HSTS is only sent when the request or the public URL is HTTPS outside debug mode.
    """
    _set_default(headers, "X-Content-Type-Options", "nosniff")
    _set_default(headers, "Referrer-Policy", "strict-origin-when-cross-origin")
    if not path.startswith(_EMBED_PREFIX):
        _set_default(headers, "Content-Security-Policy", "frame-ancestors 'self'")
    if (is_https or settings.public_base_url.lower().startswith("https://")) and not settings.debug:
        _set_default(headers, "Strict-Transport-Security", _HSTS)


def _set_default(headers: MutableHeaders, name: str, value: str) -> None:
    if name not in headers:
        headers[name] = value


class SecurityHeadersMiddleware:
    """Apply :func:`apply_security_headers` to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_https = scope.get("scheme", "http") == "https" or forwarded_https(Headers(scope=scope))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(MutableHeaders(scope=message), path=scope["path"], is_https=is_https)
            await send(message)

        await self.app(scope, receive, send_with_headers)
