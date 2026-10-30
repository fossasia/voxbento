from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from portal.config import settings
from portal.routers.listener import _build_embed_frame_policy


def apply_security_headers(headers: MutableHeaders, path: str) -> None:
    """Add baseline security headers without overriding ones a route set itself.

    /embed/ keeps its own frame and referrer policy on every response, including errors,
    so third-party hosts still see the embed's error page and the token never leaks via Referer.
    """
    if path.startswith("/embed/"):
        frame_ancestors = _build_embed_frame_policy()[0]
        referrer_policy = "no-referrer"
    else:
        frame_ancestors = "frame-ancestors 'self'"
        referrer_policy = "strict-origin-when-cross-origin"
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", referrer_policy)
    headers.setdefault("Content-Security-Policy", frame_ancestors)
    if settings.is_https:
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                apply_security_headers(MutableHeaders(scope=message), scope["path"])
            await send(message)

        await self.app(scope, receive, send_with_headers)
