from __future__ import annotations

import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

# Correlation id for the in-flight request, job or socket; empty outside one.
_request_id: ContextVar[str] = ContextVar("vb_request_id", default="")

# Fields LogRecord always carries, so everything else in __dict__ is caller-supplied.
_RESERVED_RECORD_FIELDS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
        "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
        "pathname", "process", "processName", "relativeCreated", "stack_info",
        "taskName", "thread", "threadName", "request_id",
        # uvicorn attaches an ANSI-coloured copy of its own message; it is for a
        # terminal handler, not for structured output.
        "color_message",
    }
)

_PLACEHOLDER = "[REDACTED]"

# Ordered so the narrow secret patterns win before the broad URL one.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JWTs — user_token, session_token, listener and participant tokens.
    (re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]+"), _PLACEHOLDER),
    # Authorization headers and bearer tickets. The value must look like a real
    # credential, so a "%s" placeholder in a format string is left intact.
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"), r"\1 " + _PLACEHOLDER),
    # Webhook signing secrets.
    (re.compile(r"\bwhsec_[A-Za-z0-9_-]+"), _PLACEHOLDER),
    # Query-string, form-encoded and mapping-repr credentials. The separator
    # allows either quote style so a Python dict repr ({'password': 'x'}) is
    # covered as well as "password=x" and '"password": "x"'.
    (
        re.compile(
            r"(?i)\b(token|key|api_key|apikey|client_secret|secret|code|access_token"
            r"|refresh_token|password|passwd|signature|stream_key|join_code)"
            r"(['\"]?\s*[:=]\s*['\"]?)"
            # A percent escape is part of the value, but a bare "%" is not, so
            # "token=%s" keeps its format placeholder while "token=a%2Fb" is covered.
            r"(?:%[0-9A-Fa-f]{2}|[^&\s,;}\"\'%])+"
        ),
        r"\1\2" + _PLACEHOLDER,
    ),
    # RTMP/RTMPS destinations embed the YouTube stream key in the path.
    (re.compile(r"(?i)\brtmps?://\S+"), _PLACEHOLDER),
    # Email addresses are PII even when the surrounding message is safe.
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), _PLACEHOLDER),
)


def redact(text: str) -> str:
    """Strip secrets and PII from a single string."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# An extra field named like a credential is redacted whole: its value need not
# look like a secret on its own (extra={"password": "hunter2"}).
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(token|api_?key|secret|password|passwd|signature|credential|authorization"
    r"|cookie|email|join_code|phone|full_name|postal|address)"
)


def _is_sensitive_name(name: str) -> bool:
    return bool(_SENSITIVE_FIELD_RE.search(name))


def _redact_structure(value: object, *, name: str = "") -> object:
    """Redact a value of any shape, recursing into containers.

    A log call may pass a dict or a list as an `extra` field, and a secret
    nested inside it reaches the formatter untouched unless it is walked.
    """
    if name and _is_sensitive_name(name):
        return _PLACEHOLDER
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {k: _redact_structure(v, name=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_structure(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


def _redact_value(value: object) -> object:
    return redact(value) if isinstance(value, str) else value


def _redact_format_string(msg: str) -> str:
    """Redact a format string without disturbing its "%" placeholders.

    A pattern that swallowed a placeholder would make the record fail to
    interpolate and lose the log line entirely, so a redaction that changes
    the placeholder count is discarded and the args pass redacted instead.
    """
    cleaned = redact(msg)
    return cleaned if cleaned.count("%") == msg.count("%") else msg


def new_request_id() -> str:
    """Mint a correlation id short enough to stay readable in a log line."""
    return uuid.uuid4().hex[:16]


def current_request_id() -> str:
    """Correlation id for the active request, job or socket; empty outside one."""
    return _request_id.get()


def set_request_id(value: str):
    """Bind a correlation id to this context; the returned token resets it."""
    return _request_id.set(value)


def reset_request_id(token) -> None:
    _request_id.reset(token)


class RequestIdFilter(logging.Filter):
    """Stamp every record with the active correlation id."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", ""):
            record.request_id = current_request_id()
        return True


class RedactionFilter(logging.Filter):
    """Scrub secrets and PII from the message, its args and any extra fields.

    Applied to every handler rather than at each call site, so a message that
    interpolates an email or a token cannot reach a log stream unredacted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_format_string(record.msg) if record.args else redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_value(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_value(value) for key, value in record.args.items()}
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED_RECORD_FIELDS:
                record.__dict__[key] = _redact_structure(value, name=key)
        # A traceback can carry a credential in its message or a local's repr.
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, so operators can filter on correlation id."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            # Redacted after interpolation, which catches a secret that only
            # forms once an arg lands in its placeholder (e.g. "token=%s").
            "msg": redact(record.getMessage()),
        }
        request_id = getattr(record, "request_id", "")
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS:
                payload[key] = value
        # A traceback or stack dump can quote a credential, an email or a local's
        # repr, so both go through redaction like any other message.
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = redact(self.formatStack(record.stack_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable development format that still shows the correlation id."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        # Redacted after interpolation, for the same reason as JsonFormatter.
        line = redact(super().format(record))
        request_id = getattr(record, "request_id", "")
        extras = " ".join(
            f"{key}={value}"
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_FIELDS
        )
        if request_id:
            line = f"{line} [rid={request_id}]"
        return f"{line} {extras}" if extras else line


class HealthCheckFilter(logging.Filter):
    """Drop the load balancer's /healthz polling from the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "GET /healthz" not in record.getMessage()
        except Exception:
            return True


def _resolve_level(level: str | None) -> int:
    if level:
        return logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    configured = os.getenv("LOG_LEVEL", "").upper()
    if configured:
        return logging.getLevelNamesMapping().get(configured, logging.INFO)
    return logging.INFO


def _use_json(explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    configured = os.getenv("LOG_FORMAT", "").strip().lower()
    if configured in {"json", "text"}:
        return configured == "json"
    # Development reads better as text; anything else ships JSON for operators.
    return os.getenv("ENVIRONMENT", "").strip().lower() != "development"


def configure_logging(*, level: str | None = None, json_output: bool | None = None) -> None:
    """Install the structured handler, redaction and correlation id on the root logger.

    Safe to call more than once: the portal's handler is replaced rather than
    stacked, so repeated calls cannot duplicate log lines.
    """
    root = logging.getLogger()
    for existing in [h for h in root.handlers if getattr(h, "_voxbento", False)]:
        root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler._voxbento = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter() if _use_json(json_output) else TextFormatter())
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactionFilter())

    root.addHandler(handler)
    root.setLevel(_resolve_level(level))

    # A handler the host installed before startup would otherwise emit records
    # unredacted. Its output is not ours to remove, but it must not leak.
    for existing in root.handlers:
        if not getattr(existing, "_voxbento", False):
            existing.addFilter(RequestIdFilter())
            existing.addFilter(RedactionFilter())

    # uvicorn ships its own handlers; let its records flow through ours instead
    # so they are redacted and carry the correlation id too. Its loggers also
    # pin an explicit INFO level, which would ignore LOG_LEVEL, so they are
    # reset to NOTSET and inherit the root level like everything else.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.setLevel(logging.NOTSET)
        uvicorn_logger.propagate = True
    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())
