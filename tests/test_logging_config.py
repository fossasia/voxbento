from __future__ import annotations

import json
import logging

import pytest

from portal.logging_config import (
    JsonFormatter,
    RedactionFilter,
    RequestIdFilter,
    TextFormatter,
    _is_sensitive_name,
    _redact_structure,
    configure_logging,
    current_request_id,
    new_request_id,
    redact,
    reset_request_id,
    set_request_id,
)


@pytest.fixture
def isolated_logging():
    """Restore global logging state, which configure_logging() mutates.

    Without this, root handlers/levels and the uvicorn loggers stay changed for
    every later test in the session, and records can be written to a capture
    stream that has already been torn down.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    uvicorn_names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    saved_uvicorn = {
        name: (list(logging.getLogger(name).handlers),
               logging.getLogger(name).level,
               logging.getLogger(name).propagate,
               list(logging.getLogger(name).filters))
        for name in uvicorn_names
    }
    saved_filters = {id(h): list(h.filters) for h in saved_handlers}
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for handler in saved_handlers:
            handler.filters[:] = saved_filters[id(handler)]
        for name, (handlers, level, propagate, filters) in saved_uvicorn.items():
            uvicorn_logger = logging.getLogger(name)
            uvicorn_logger.handlers[:] = handlers
            uvicorn_logger.setLevel(level)
            uvicorn_logger.propagate = propagate
            uvicorn_logger.filters[:] = filters


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


def test_redaction_filter_closes_the_split_context_leak():
    """logger.info("key=%s", "my-secret") had the context in msg and the value
    in args; neither matches alone. A foreign handler calling getMessage()
    directly would have leaked.
    """
    record = _record("key=%s", ("my-secret-value",))
    RedactionFilter().filter(record)
    message = record.getMessage()
    assert "my-secret-value" not in message
    assert "[REDACTED]" in message


def test_split_context_fallback_does_not_touch_an_innocuous_call():
    """A non-credential format string must not lose its args to the fallback."""
    record = _record("user %s did %s", ("alice", "signed in"))
    RedactionFilter().filter(record)
    assert record.args == ("alice", "signed in")
    assert record.getMessage() == "user alice did signed in"


def test_split_context_fallback_leaves_accessformatter_args_intact():
    """Each uvicorn.access arg carries its own context, so per-arg redaction is
    already complete and the fallback must find nothing new. args stays so
    AccessFormatter can read them positionally.
    """
    record = _record("%s - %s %d", ("127.0.0.1", "/x?token=abcd1234xyz", 200))
    RedactionFilter().filter(record)
    assert record.args is not None
    assert "abcd1234xyz" not in record.getMessage()
    assert "token=[REDACTED]" in record.getMessage()


def test_redaction_filter_recurses_into_a_dict_positional_arg():
    """logger.info("Data: %s", my_dict) used to dump the dict unchanged."""
    record = _record("Data: %s", ({"password": "secret", "ok": True},))
    RedactionFilter().filter(record)
    message = record.getMessage()
    assert "secret" not in message
    assert "[REDACTED]" in message
    # Non-sensitive fields survive — the walk doesn't nuke the whole container.
    assert "'ok': True" in message


def test_redaction_filter_recurses_into_a_list_positional_arg():
    """A list of records with emails must be scrubbed element by element."""
    record = _record("Users: %s", ([{"email": "a@example.com"}, {"email": "b@example.com"}],))
    RedactionFilter().filter(record)
    message = record.getMessage()
    assert "a@example.com" not in message
    assert "b@example.com" not in message


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


@pytest.mark.parametrize(
    "raw,leaked",
    [
        ("params {'password': 'hunter2'}", "hunter2"),
        ('params {"client_secret": "s3cr3t"}', "s3cr3t"),
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("GET /x?token=abc%2Fdef%2Fghi", "abc%2Fdef%2Fghi"),
    ],
)
def test_redact_covers_mapping_reprs_basic_auth_and_encoded_values(raw, leaked):
    cleaned = redact(raw)
    assert leaked not in cleaned
    assert "[REDACTED]" in cleaned


@pytest.mark.parametrize(
    "url,leaked",
    [
        # The OAuth authorize redirect carries the user's email as a query parameter.
        ("/oauth/authorize?client_id=c&login_hint=evy-admin%40demo.eventyay.test&event=demo", "evy-admin"),
        ("/oauth/authorize?login_hint=someone@example.com", "someone"),
        # A percent-encoded address is still an address, whatever the parameter is called.
        ("/lookup?who=jo.doe%40example.org&page=2", "jo.doe"),
        ("/oauth/token?code_verifier=dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk", "dBjftJeZ4CVP"),
    ],
)
def test_an_access_line_does_not_carry_an_encoded_email_or_pkce_verifier(url, leaked):
    record = _record("%s - %s %d", ("127.0.0.1", url, 200))
    RedactionFilter().filter(record)
    message = record.getMessage()
    assert leaked not in message
    assert "[REDACTED]" in message


def test_redacting_an_encoded_email_leaves_the_rest_of_the_query_readable():
    url = "/oauth/authorize?client_id=c&login_hint=a%40b.example&event=demo"
    record = _record("%s - %s %d", ("127.0.0.1", url, 200))
    RedactionFilter().filter(record)
    message = record.getMessage()
    assert "client_id=c" in message
    assert "event=demo" in message


def test_redaction_filter_scrubs_a_credential_named_extra():
    # The value looks like nothing on its own; the field name is the signal.
    record = _record("saved", password="hunter2")
    RedactionFilter().filter(record)
    assert record.password == "[REDACTED]"


@pytest.mark.parametrize("name", ["private_key", "api_key", "stream_key", "signing_key", "encryption_key"])
def test_a_qualified_key_name_is_sensitive(name):
    assert _is_sensitive_name(name)


@pytest.mark.parametrize("name", ["key", "code", "status", "route", "monkey", "duration_ms", "request_id"])
def test_a_bare_key_or_code_name_stays_operational(name):
    # "key" alone is as likely a cache key, and "code" an outcome code.
    assert not _is_sensitive_name(name)


@pytest.mark.anyio
async def test_a_background_task_inherits_the_correlation_id():
    """Work spawned during a request stays on the same trail.

    asyncio copies the current context into a new task, so a save-and-translate
    or webhook-delivery task started mid-request carries its id without being
    passed one.
    """
    import asyncio

    token = set_request_id("rid-parent")
    try:
        seen: dict[str, str] = {}

        async def background() -> None:
            seen["id"] = current_request_id()

        await asyncio.create_task(background())
        assert seen["id"] == "rid-parent"
    finally:
        reset_request_id(token)


_SENSITIVE_EXTRAS = [
    "stream_key", "api_key", "private_key", "join_code",
    "phone", "full_name", "postal", "address", "cookie", "password",
]


@pytest.mark.parametrize("field", _SENSITIVE_EXTRAS)
def test_redaction_filter_scrubs_each_sensitive_extra_name(field):
    record = _record("saved", **{field: "abcd-efgh"})
    RedactionFilter().filter(record)
    assert getattr(record, field) == "[REDACTED]"


@pytest.mark.parametrize("field", _SENSITIVE_EXTRAS)
def test_redaction_filter_scrubs_a_sensitive_name_nested_in_an_extra(field):
    record = _record("saved", context={"outer": {field: "abcd-efgh"}})
    RedactionFilter().filter(record)
    assert record.context["outer"][field] == "[REDACTED]"


@pytest.mark.parametrize("field", _SENSITIVE_EXTRAS)
def test_both_formatters_hide_a_sensitive_nested_extra(field):
    record = _record("saved", context={field: "abcd-efgh"})
    RedactionFilter().filter(record)
    assert "abcd-efgh" not in JsonFormatter().format(record)
    assert "abcd-efgh" not in TextFormatter().format(record)


@pytest.mark.parametrize("field,value", [("status", 200), ("code", "booth_full"), ("duration_ms", 12.5)])
def test_redaction_filter_keeps_operational_extras(field, value):
    # A bare "code" or "key" names outcome codes too; redacting those by name
    # would cost diagnostics without protecting anything.
    record = _record("done", **{field: value})
    RedactionFilter().filter(record)
    assert getattr(record, field) == value


def test_redaction_filter_renders_a_redacted_traceback_for_foreign_formatters():
    import sys

    try:
        raise ValueError("rejected lee@example.com")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()

    RedactionFilter().filter(record)

    # A standard formatter reuses a populated exc_text instead of rendering
    # its own, so a handler the host installed gets the redacted version.
    assert record.exc_text
    assert "lee@example.com" not in record.exc_text
    assert "lee@example.com" not in logging.Formatter().format(record)


def test_redaction_filter_scrubs_stack_info():
    record = _record("trace")
    record.stack_info = 'File "x.py", line 1\n  connect("mia@example.com")'
    RedactionFilter().filter(record)
    assert "mia@example.com" not in record.stack_info


def test_redaction_filter_recurses_into_a_nested_extra():
    record = _record("sent", context={"user": {"email": "gina@example.com"}, "ok": True})
    RedactionFilter().filter(record)
    assert "gina@example.com" not in json.dumps(record.context)


@pytest.mark.parametrize(
    "raw,leaked",
    [
        ('{"password": "my secret"}', "secret"),
        ("{'password': 'my secret value'}", "secret value"),
        ('password="my secret"', "secret"),
        ('{"client_secret": "a b c", "ok": 1}', "a b c"),
    ],
)
def test_redact_covers_a_quoted_multi_word_credential(raw, leaked):
    # The unquoted pattern stops at the first space, which left the rest in.
    cleaned = redact(raw)
    assert leaked not in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_keeps_what_follows_a_quoted_credential():
    assert '"ok": 1' in redact('{"client_secret": "a b c", "ok": 1}')


def test_redact_structure_survives_a_self_reference():
    payload: dict = {"a": 1}
    payload["self"] = payload
    # Would raise RecursionError before the guard.
    assert _redact_structure(payload)["self"] == "[TRUNCATED]"


def test_redact_structure_survives_mutual_recursion():
    first: dict = {}
    second = {"first": first}
    first["second"] = second
    assert _redact_structure(first)["second"]["first"] == "[TRUNCATED]"


def test_redact_structure_caps_depth_without_leaking_past_the_cap():
    deep: dict = {}
    cursor = deep
    for _ in range(50):
        cursor["next"] = {}
        cursor = cursor["next"]
    cursor["password"] = "leak"
    assert "leak" not in json.dumps(_redact_structure(deep))


def test_redact_structure_still_walks_a_shared_child_twice():
    # Seen-tracking is per path, so a sibling reference is not a cycle.
    shared = {"k": "v"}
    out = _redact_structure({"x": shared, "y": shared})
    assert out == {"x": {"k": "v"}, "y": {"k": "v"}}


def test_redaction_filter_recurses_into_a_list_extra():
    record = _record("sent", recipients=["h@example.com", "i@example.com"])
    RedactionFilter().filter(record)
    assert not [r for r in record.recipients if "@example.com" in r]


def test_json_formatter_redacts_the_traceback():
    import sys

    try:
        raise ValueError("rejected token=sekritvalue for jane@example.com")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "sekritvalue" not in payload["exc"]
    assert "jane@example.com" not in payload["exc"]


def test_text_formatter_redacts_the_traceback():
    import sys

    try:
        raise ValueError("rejected kyle@example.com")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()
    assert "kyle@example.com" not in TextFormatter().format(record)


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


def test_json_formatter_drops_uvicorns_ansi_copy_of_the_message():
    # uvicorn attaches color_message with raw escape codes; it is terminal-only.
    record = _record("Uvicorn running", color_message="Uvicorn running on \u001b[1m%s\u001b[0m")
    payload = json.loads(JsonFormatter().format(record))
    assert "color_message" not in payload


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


def test_configure_logging_is_idempotent(isolated_logging):
    configure_logging()
    configure_logging()
    root = logging.getLogger()
    ours = [h for h in root.handlers if getattr(h, "_voxbento", False)]
    # A second call must replace rather than stack, or every line doubles.
    assert len(ours) == 1


def test_configure_logging_installs_redaction_on_the_handler(isolated_logging, capsys):
    configure_logging(level="INFO", json_output=True)
    logging.getLogger("portal.test.redaction").info("signed in as erin@example.com")
    output = "".join(capsys.readouterr())
    # Assert the record was emitted *and* redacted — absence alone would also
    # pass if logging had dropped the record entirely.
    assert "signed in as [REDACTED]" in output
    assert "erin@example.com" not in output


def test_configure_logging_redacts_through_a_preexisting_handler(isolated_logging, capsys):
    """A handler installed before startup must not emit records unredacted."""
    root = logging.getLogger()
    foreign = logging.StreamHandler()
    foreign.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(foreign)
    try:
        configure_logging(level="INFO", json_output=True)
        logging.getLogger("portal.test.foreign").info("code=ROOM42 for frank@example.com")
        output = "".join(capsys.readouterr())
        assert "ROOM42" not in output
        assert "frank@example.com" not in output
    finally:
        root.removeHandler(foreign)


def test_configure_logging_applies_the_level_to_uvicorn(isolated_logging, capsys):
    configure_logging(level="ERROR", json_output=True)
    # uvicorn pins its loggers to INFO; without a reset those records would
    # still be emitted when LOG_LEVEL asks for ERROR.
    access = logging.getLogger("uvicorn.access")
    access.info("GET / 200")
    access.error("boom at the access layer")
    output = "".join(capsys.readouterr())
    assert "GET / 200" not in output
    # Positive control: without it this would also pass if the handler wrote
    # nowhere, or dropped every record regardless of level.
    assert "boom at the access layer" in output


def test_configure_logging_routes_uvicorn_through_our_handler(isolated_logging):
    configure_logging()
    access = logging.getLogger("uvicorn.access")
    assert access.handlers == []
    assert access.propagate is True
