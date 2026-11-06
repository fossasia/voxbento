from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, WebSocket
from starlette.testclient import TestClient

from portal.logging_config import current_request_id
from portal.request_logging import REQUEST_ID_HEADER, RequestLoggingMiddleware

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


def test_an_upstream_request_id_is_reused(client):
    response = client.get("/ping", headers={REQUEST_ID_HEADER: "edge-abc-123"})
    assert response.headers[REQUEST_ID_HEADER] == "edge-abc-123"


def test_an_unsafe_upstream_id_is_sanitised(client):
    # A proxy-supplied id must not be able to inject newlines into a log line.
    response = client.get("/ping", headers={REQUEST_ID_HEADER: "abc def;rm -rf"})
    returned = response.headers[REQUEST_ID_HEADER]
    assert returned == "abcdefrm-rf"
    assert " " not in returned and ";" not in returned


def test_an_overlong_upstream_id_is_truncated(client):
    response = client.get("/ping", headers={REQUEST_ID_HEADER: "a" * 500})
    assert len(response.headers[REQUEST_ID_HEADER]) == 64


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
