from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException, Request

from portal.config import settings
from portal.globals import get_http_client
from portal.rate_limit import check_rate_limit

logger = logging.getLogger(__name__)


def limit_registration(request: Request) -> None:
    # Only the ASGI server's resolved peer is trusted. Forwarded headers must
    # be processed by a correctly configured trusted proxy chain, not here.
    client_ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(
        "registration_ip", client_ip, max_requests=settings.registration_requests_per_minute, window_seconds=60
    ):
        raise HTTPException(
            status_code=429,
            detail="Too many signup attempts. Please wait a minute and try again.",
            headers={"Retry-After": "60"},
        )


async def verify_registration_challenge(token: object) -> None:
    """Reject unverified submissions before hashing passwords or opening a transaction."""
    if not settings.registration_turnstile_enabled:
        return

    hostname = urlparse(settings.public_base_url).hostname
    unavailable = "Signup verification is temporarily unavailable. Please try again later."
    if not settings.turnstile_site_key.strip() or not settings.turnstile_secret_key.strip() or not hostname:
        logger.error("Registration Turnstile is enabled but its keys or public hostname are missing")
        raise HTTPException(status_code=503, detail=unavailable)
    if not isinstance(token, str) or not token.strip() or len(token) > 2048:
        raise HTTPException(status_code=403, detail="Please complete the security check and try again.")

    try:
        response = await get_http_client().post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data={"secret": settings.turnstile_secret_key, "response": token},
            timeout=5.0,
        )
        response.raise_for_status()
        result = response.json()
    except (httpx.HTTPError, ValueError):
        # Neither tokens, keys nor provider response bodies belong in logs.
        logger.warning("Registration Turnstile verification service failed")
        raise HTTPException(status_code=503, detail=unavailable) from None

    if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
        logger.warning("Registration Turnstile returned an invalid response")
        raise HTTPException(status_code=503, detail=unavailable)
    if result["success"] is not True or result.get("hostname") != hostname or result.get("action") != "register":
        raise HTTPException(status_code=403, detail="Security check failed or expired. Please try again.")
