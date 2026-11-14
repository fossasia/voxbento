"""Tests for the global security headers middleware and the shared Secure-cookie decision."""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from portal.auth import is_cookie_secure
from portal.config import settings
from portal.security_headers import SecurityHeadersMiddleware

HSTS = "max-age=31536000"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/page")
    async def page():
        return PlainTextResponse("ok")

    @app.get("/overlay")
    async def overlay():
        return PlainTextResponse("ok", headers={"Content-Security-Policy": "frame-ancestors 'none'"})

    @app.get("/embed/frame")
    async def embed():
        return PlainTextResponse("ok", headers={"Content-Security-Policy": "frame-ancestors *"})

    @app.get("/embed/error")
    async def embed_without_policy():
        return PlainTextResponse("denied", status_code=403)

    @app.get("/secure")
    async def secure(request: Request):
        return PlainTextResponse(str(is_cookie_secure(request)))

    return app


@pytest.fixture
def client():
    return TestClient(_build_app(), base_url="http://testserver")


def test_baseline_headers_on_every_response(client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    res = client.get("/page")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert res.headers["content-security-policy"] == "frame-ancestors 'self'"
    assert "strict-transport-security" not in res.headers


def test_route_provided_headers_are_preserved(client):
    res = client.get("/overlay")
    assert res.headers["content-security-policy"] == "frame-ancestors 'none'"


def test_embed_keeps_its_own_frame_policy(client):
    assert client.get("/embed/frame").headers["content-security-policy"] == "frame-ancestors *"
    # No default clickjacking policy is imposed on embeds, even on errors.
    assert "content-security-policy" not in client.get("/embed/error").headers
    assert client.get("/embed/error").headers["x-content-type-options"] == "nosniff"


def test_hsts_sent_for_https_public_url(client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://voxbento.com")
    monkeypatch.setattr(settings, "debug", False)
    assert client.get("/page").headers["strict-transport-security"] == HSTS


def test_hsts_sent_for_forwarded_https(client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "http://localhost:8000")
    monkeypatch.setattr(settings, "debug", False)
    res = client.get("/page", headers={"x-forwarded-proto": "https"})
    assert res.headers["strict-transport-security"] == HSTS


def test_hsts_not_sent_in_debug(client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://voxbento.com")
    monkeypatch.setattr(settings, "debug", True)
    assert "strict-transport-security" not in client.get("/page").headers


@pytest.mark.parametrize(
    ("public_url", "debug", "headers", "expected"),
    [
        ("http://localhost:8000", False, {}, "False"),
        ("https://voxbento.com", False, {}, "True"),
        ("HTTPS://voxbento.com", False, {}, "True"),
        ("https://voxbento.com", True, {}, "False"),
        ("http://localhost:8000", False, {"x-forwarded-proto": "https"}, "True"),
        ("http://localhost:8000", False, {"x-forwarded-proto": "HTTPS"}, "True"),
        ("http://localhost:8000", False, {"x-forwarded-proto": "https, http"}, "True"),
        ("http://localhost:8000", False, {"x-forwarded-proto": "http, https"}, "False"),
        ("https://voxbento.com", True, {"x-forwarded-proto": "https"}, "True"),
    ],
)
def test_is_cookie_secure(client, monkeypatch, public_url, debug, headers, expected):
    monkeypatch.setattr(settings, "public_base_url", public_url)
    monkeypatch.setattr(settings, "debug", debug)
    assert client.get("/secure", headers=headers).text == expected


def test_hsts_does_not_cover_subdomains(client, monkeypatch):
    monkeypatch.setattr(settings, "public_base_url", "https://voxbento.com")
    monkeypatch.setattr(settings, "debug", False)
    assert "includesubdomains" not in client.get("/page").headers["strict-transport-security"].lower()


def test_middleware_accepts_a_scope_without_scheme():
    """`scheme` is optional in the ASGI spec; a server that leaves it out must not crash the request."""
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": "/page", "headers": []}
    asyncio.run(SecurityHeadersMiddleware(app)(scope, receive, send))
    assert (b"x-content-type-options", b"nosniff") in sent[0]["headers"]
