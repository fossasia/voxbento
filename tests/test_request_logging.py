from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient

from portal.logging_config import current_request_id
from portal.request_logging import REQUEST_ID_HEADER, UNMATCHED_ROUTE, RequestLoggingMiddleware

pytestmark = pytest.mark.anyio


@pytest.fixture
def app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(RequestLoggingMiddleware)

    @application.get("/ping")
    async def ping():
        return {"request_id": current_request_id()}

    @application.get("/items/{item_id}")
    async def item(item_id: int):
        return {"item_id": item_id}

    @application.get("/boom")
    async def boom():
        raise RuntimeError("deliberate")

    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def test_response_carries_a_request_id(client):
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.headers[REQUEST_ID_HEADER]


def test_the_handler_sees_the_same_id_the_header_reports(client):
    response = client.get("/ping")
    assert response.json()["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_each_request_gets_a_distinct_id(client):
    first = client.get("/ping").headers[REQUEST_ID_HEADER]
    second = client.get("/ping").headers[REQUEST_ID_HEADER]
    assert first != second


def test_an_upstream_request_id_is_not_adopted(client):
    # The endpoint is public, so a caller must not be able to choose the id a
    # trace is filed under, or merge its activity into someone else's.
    response = client.get("/ping", headers={REQUEST_ID_HEADER: "edge-abc-123"})
    assert response.headers[REQUEST_ID_HEADER] != "edge-abc-123"
    assert len(response.headers[REQUEST_ID_HEADER]) == 16


def test_an_upstream_request_id_is_recorded_alongside_ours(client, caplog):
    with caplog.at_level(logging.INFO, logger="portal.request"):
        response = client.get("/ping", headers={REQUEST_ID_HEADER: "edge-abc-123"})
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    # The upstream trace is still linkable, just not authoritative.
    assert record.upstream_request_id == "edge-abc-123"
    assert response.headers[REQUEST_ID_HEADER] != "edge-abc-123"


def test_an_unsafe_upstream_id_is_sanitised_before_being_recorded(client, caplog):
    # A supplied id must not be able to inject a newline into a log line.
    with caplog.at_level(logging.INFO, logger="portal.request"):
        client.get("/ping", headers={REQUEST_ID_HEADER: "abc def;rm -rf"})
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    assert record.upstream_request_id == "abcdefrm-rf"


def test_an_overlong_upstream_id_is_truncated(client, caplog):
    with caplog.at_level(logging.INFO, logger="portal.request"):
        client.get("/ping", headers={REQUEST_ID_HEADER: "a" * 500})
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    assert len(record.upstream_request_id) == 64


def test_no_upstream_field_when_the_header_is_absent(client, caplog):
    with caplog.at_level(logging.INFO, logger="portal.request"):
        client.get("/ping")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    assert not hasattr(record, "upstream_request_id")


def test_the_id_does_not_leak_between_requests(client):
    client.get("/ping")
    # Outside a request the context variable must be empty again.
    assert current_request_id() == ""


def test_health_polling_is_not_logged(caplog):
    application = FastAPI()
    application.add_middleware(RequestLoggingMiddleware)

    @application.get("/healthz")
    async def healthz():
        return {"ok": True}

    with caplog.at_level(logging.INFO, logger="portal.request"):
        response = TestClient(application).get("/healthz")

    # The probe still gets an id for tracing, it just does not flood the log.
    assert response.headers[REQUEST_ID_HEADER]
    assert not [r for r in caplog.records if getattr(r, "event", "") == "request.completed"]


async def test_a_response_without_a_headers_key_does_not_crash():
    """"headers" is optional on http.response.start per the ASGI spec."""
    sent: list = []

    async def bare_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 204})
        await send({"type": "http.response.body", "body": b""})

    async def capture(message):
        sent.append(message)

    await RequestLoggingMiddleware(bare_app)(
        {"type": "http", "method": "GET", "path": "/bare", "headers": []},
        None,
        capture,
    )

    headers = dict(sent[0]["headers"])
    assert headers[REQUEST_ID_HEADER.encode()]


def test_a_failing_health_probe_is_still_logged(caplog):
    application = FastAPI()
    application.add_middleware(RequestLoggingMiddleware)

    @application.get("/healthz")
    async def healthz():
        from fastapi.responses import JSONResponse

        return JSONResponse({"ok": False}, status_code=503)

    with caplog.at_level(logging.INFO, logger="portal.request"):
        TestClient(application).get("/healthz")

    # Only *successful* probes are suppressed; a failing one is the one to see.
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    assert record.status == 503


def test_an_unmatched_route_does_not_log_the_concrete_path(client, caplog):
    # An invite or revocation URL carries its token in the path, so a request
    # that matched no route must not fall back to logging it.
    with caplog.at_level(logging.INFO, logger="portal.request"):
        client.get("/invite/sekrit-token-value-abc123")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    assert record.route == UNMATCHED_ROUTE
    assert "sekrit-token-value-abc123" not in record.route


def test_a_websocket_handshake_carries_the_request_id():
    application = FastAPI()
    application.add_middleware(RequestLoggingMiddleware)

    @application.websocket("/ws/echo")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    with TestClient(application).websocket_connect("/ws/echo") as ws:
        headers = {
            (k.decode() if isinstance(k, bytes) else k).lower(): (v.decode() if isinstance(v, bytes) else v)
            for k, v in (ws.extra_headers or [])
        }
    assert len(headers.get(REQUEST_ID_HEADER, "")) == 16


def test_a_handler_error_carries_the_correlation_header(client):
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert len(resp.headers.get(REQUEST_ID_HEADER, "")) == 16


def test_a_handler_error_records_the_resulting_status(client, caplog):
    with caplog.at_level(logging.INFO, logger="portal.request"):
        client.get("/boom")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.failed")
    assert record.status == 500


def test_completion_is_logged_with_route_status_and_duration(client, caplog):
    with caplog.at_level(logging.INFO, logger="portal.request"):
        client.get("/items/7")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    assert record.route == "/items/{item_id}"
    assert record.status == 200
    assert record.method == "GET"
    assert record.duration_ms >= 0


def test_the_route_template_keeps_ids_out_of_the_log(client, caplog):
    with caplog.at_level(logging.INFO, logger="portal.request"):
        client.get("/items/4242")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    # The concrete id belongs in the payload, never in the route label.
    assert "4242" not in record.route


def test_a_handler_error_is_logged_against_the_request(client, caplog):
    with caplog.at_level(logging.INFO, logger="portal.request"):
        client.get("/boom")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.failed")
    assert record.route == "/boom"
    assert record.levelno == logging.ERROR


def test_a_server_error_is_logged_at_warning(caplog):
    application = FastAPI()
    application.add_middleware(RequestLoggingMiddleware)

    @application.get("/fail")
    async def fail():
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "nope"}, status_code=500)

    with caplog.at_level(logging.INFO, logger="portal.request"):
        TestClient(application).get("/fail")
    record = next(r for r in caplog.records if getattr(r, "event", "") == "request.completed")
    assert record.status == 500
    assert record.levelno == logging.WARNING


def test_a_websocket_route_is_logged_on_close(caplog):
    application = FastAPI()
    application.add_middleware(RequestLoggingMiddleware)

    @application.websocket("/ws/echo")
    async def echo(websocket: WebSocket):
        await websocket.accept()
        await websocket.close()

    with caplog.at_level(logging.INFO, logger="portal.request"):
        with TestClient(application).websocket_connect("/ws/echo"):
            pass
    record = next(r for r in caplog.records if getattr(r, "event", "") == "socket.closed")
    assert record.route == "/ws/echo"
