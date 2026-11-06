from __future__ import annotations

import json
import logging

import pytest

from portal.logging_config import (
    JsonFormatter,
    RedactionFilter,
    RequestIdFilter,
    TextFormatter,
    configure_logging,
    current_request_id,
    new_request_id,
    redact,
    reset_request_id,
    set_request_id,
)


def _record(msg: str, args: tuple | dict = (), **extra) -> logging.LogRecord:
    record = logging.LogRecord(
        name="portal.test", level=logging.INFO, pathname="", lineno=0, msg=msg, args=args, exc_info=None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


@pytest.mark.parametrize(
    "raw,leaked",
    [
        ("signed in as alice@example.com", "alice@example.com"),
        ("cookie eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sIgNaTuRe", "sIgNaTuRe"),
        ("Authorization: Bearer abc123def", "abc123def"),
        ("rotating whsec_AbCdEf123456789", "whsec_AbCdEf123456789"),
        ("GET /oauth/token?client_secret=topsecret", "topsecret"),
        ("POST /login password=hunter2", "hunter2"),
        ("ffmpeg -f flv rtmps://a.rtmp.youtube.com/live2/abcd-efgh", "abcd-efgh"),
        ("overlay /overlay/demo/1/en?code=ROOM42", "ROOM42"),
        ("provider call api_key=sk-live-999", "sk-live-999"),
    ],
)
def test_redact_removes_every_secret_shape(raw, leaked):
    cleaned = redact(raw)
    assert leaked not in cleaned
    assert "[REDACTED]" in cleaned


@pytest.mark.parametrize(
    "safe",
    [
        "booth pycon2026-14-en went live",
        "transcription worker started for room 14",
        "closed 3 sockets for user 42",
        "MediaMTX path pycon2026/14/en recreated",
    ],
)
def test_redact_keeps_operationally_useful_text(safe):
    # Opaque ids and booth identities must survive, or the logs lose their value.
    assert redact(safe) == safe


def test_redaction_filter_scrubs_the_message():
    record = _record("signed in as alice@example.com")
    RedactionFilter().filter(record)
    assert "alice@example.com" not in record.getMessage()


def test_redaction_filter_scrubs_positional_args():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sIgNaTuRe"
    record = _record("user %s token %s", ("bob@example.com", jwt))
    RedactionFilter().filter(record)
    message = record.getMessage()
    assert "bob@example.com" not in message
    assert jwt not in message


def test_redaction_filter_scrubs_dict_args():
    # LogRecord unwraps a single mapping arg, which is how logger.info(msg, dict) arrives.
    record = _record("user %(who)s", ({"who": "carol@example.com"},))
    RedactionFilter().filter(record)
    assert "carol@example.com" not in record.getMessage()


def test_redaction_filter_never_breaks_placeholder_interpolation():
    # A pattern that ate "%s" would make getMessage() raise and lose the line.
    record = _record("auth token %s for user %s", ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sIg", 42))
    RedactionFilter().filter(record)
    message = record.getMessage()
    assert "42" in message
    assert "eyJhbGciOiJIUzI1NiJ9" not in message


@pytest.mark.parametrize("formatter", [JsonFormatter(), TextFormatter()])
def test_formatter_redacts_a_secret_formed_by_interpolation(formatter):
    # "token=%s" keeps its placeholder through the filter, so the secret only
    # appears once the arg lands in it — the formatter is the last guard.
    record = _record("sending token=%s", ("secretvalue",))
    RedactionFilter().filter(record)
    assert "secretvalue" not in formatter.format(record)


def test_redaction_filter_scrubs_extra_fields():
    record = _record("sent", target="dave@example.com")
    RedactionFilter().filter(record)
    assert "dave@example.com" not in record.target


def test_redaction_filter_leaves_non_string_extras_alone():
    record = _record("done", status=204, duration_ms=12.5)
    RedactionFilter().filter(record)
    assert record.status == 204
    assert record.duration_ms == 12.5


def test_request_id_filter_stamps_the_active_id():
    token = set_request_id("abc123")
    try:
        record = _record("hello")
        RequestIdFilter().filter(record)
        assert record.request_id == "abc123"
    finally:
        reset_request_id(token)


def test_request_id_is_empty_outside_a_request():
    assert current_request_id() == ""


def test_new_request_id_is_unique_and_short():
    first, second = new_request_id(), new_request_id()
    assert first != second
    assert len(first) == 16
    assert first.isalnum()


def test_json_formatter_emits_one_parsable_object():
    record = _record("booth live", event="booth.live", status=200)
    record.request_id = "rid-1"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["msg"] == "booth live"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "portal.test"
    assert payload["request_id"] == "rid-1"
    assert payload["event"] == "booth.live"
    assert payload["status"] == 200


def test_json_formatter_omits_an_absent_request_id():
    assert "request_id" not in json.loads(JsonFormatter().format(_record("plain")))


def test_json_formatter_includes_the_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _record("failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in payload["exc"]


def test_text_formatter_shows_the_request_id_and_extras():
    record = _record("request completed", status=200)
    record.request_id = "rid-2"
    line = TextFormatter().format(record)
    assert "rid=rid-2" in line
    assert "status=200" in line


def test_configure_logging_is_idempotent():
    configure_logging()
    configure_logging()
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, "_voxbento", False)]
    # A second call must replace rather than stack, or every line doubles.
    assert len(ours) == 1


def test_configure_logging_installs_redaction_on_the_handler(capsys):
    configure_logging(json_output=True)
    logging.getLogger("portal.test.redaction").info("signed in as erin@example.com")
    captured = capsys.readouterr()
    assert "erin@example.com" not in (captured.err + captured.out)


def test_configure_logging_routes_uvicorn_through_our_handler():
    configure_logging()
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.propagate is True
