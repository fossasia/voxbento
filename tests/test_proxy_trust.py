"""The deployed proxy chain must resolve real visitors, not the Docker gateway or Cloudflare."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "172.18.0.1"
CLOUDFLARE_PEER = "104.16.0.10"


def _caddy_trusted() -> list[str]:
    match = re.search(r"^\s*trusted_proxies (.+)$", (ROOT / "Caddyfile").read_text(), re.MULTILINE)
    assert match, "Caddyfile must trust Cloudflare's ranges"
    return match.group(1).split()


def _uvicorn_trusted() -> list[str]:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    value = compose["services"]["portal"]["environment"]["FORWARDED_ALLOW_IPS"]
    return value.replace("${PORTAL_PROXY_GATEWAY:-127.0.0.1}", GATEWAY).split(",")


def test_caddy_and_uvicorn_trust_the_same_cloudflare_ranges():
    uvicorn = _uvicorn_trusted()
    assert uvicorn[0] == GATEWAY
    assert uvicorn[1:] == _caddy_trusted()


async def _resolve(peer: str, forwarded_for: str) -> str:
    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"][0]

    scope = {
        "type": "http",
        "client": (peer, 5000),
        "headers": [(b"x-forwarded-for", forwarded_for.encode())],
    }
    await ProxyHeadersMiddleware(app, trusted_hosts=",".join(_uvicorn_trusted()))(scope, None, None)
    return seen["client"]


@pytest.mark.anyio
async def test_visitor_behind_cloudflare_is_resolved_and_spoofed_entries_ignored():
    # Cloudflare appends the visitor; Caddy appends the Cloudflare peer.
    assert await _resolve(GATEWAY, f"6.6.6.6, 198.51.100.7, {CLOUDFLARE_PEER}") == "198.51.100.7"


@pytest.mark.anyio
async def test_direct_origin_request_cannot_choose_its_address():
    # Caddy replaces X-Forwarded-For with the peer for untrusted (non-Cloudflare) clients.
    assert await _resolve(GATEWAY, "203.0.113.9") == "203.0.113.9"
    # A peer other than the gateway is never trusted to forward.
    assert await _resolve("172.18.0.5", "198.51.100.7") == "172.18.0.5"
