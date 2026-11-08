"""Unit tests for portal.utils helper functions."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from portal.utils import get_client_ip


def _make_request(client_host: str | None = "192.0.2.1", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    client = (client_host, 12345) if client_host is not None else None
    scope = {
        "type": "http",
        "client": client,
        "headers": headers or [],
    }
    return Request(scope)


def test_get_client_ip_returns_resolved_client_host():
    req = _make_request(client_host="203.0.113.195")
    assert get_client_ip(req) == "203.0.113.195"


def test_get_client_ip_ignores_unauthenticated_forwarded_headers():
    req = _make_request(
        client_host="203.0.113.195",
        headers=[
            (b"x-forwarded-for", b"198.51.100.1, 10.0.0.1"),
            (b"cf-connecting-ip", b"198.51.100.2"),
        ],
    )
    assert get_client_ip(req) == "203.0.113.195"


def test_get_client_ip_falls_back_when_no_client():
    req = _make_request(client_host=None)
    assert get_client_ip(req) == "unknown"


def test_get_client_ip_falls_back_when_no_client_even_with_forwarded_headers():
    req = _make_request(
        client_host=None,
        headers=[(b"x-forwarded-for", b"198.51.100.1")],
    )
    assert get_client_ip(req) == "unknown"
