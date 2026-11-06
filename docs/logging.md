# Logging

VoxBento emits one structured record per event, with a correlation id that ties
a single trail together from request through workers to outbound calls.
Redaction is enforced centrally, so a log call cannot leak a secret or personal
data even when the value is interpolated into the message.

Owned by `portal/logging_config.py` (configuration, redaction, formatters) and
`portal/request_logging.py` (per-request correlation ids).

## Configuration

`configure_logging()` runs once from the application lifespan. Two environment
variables tune it:

| Variable | Values | Default |
|---|---|---|
| `LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` | `INFO` |
| `LOG_FORMAT` | `json`, `text` | `text` when `ENVIRONMENT=development`, else `json` |

JSON is the production format because operators filter on `request_id`. Text is
easier to read while developing. uvicorn's own loggers are routed through the
same handler, so access lines are redacted and correlated too.

## Correlation ids

`RequestLoggingMiddleware` mints a 16-character id for every HTTP request and
WebSocket connection, binds it to a context variable, and echoes it as the
`X-Request-ID` response header. Any log call made while handling that request
carries the id without being passed it:

```python
import logging

logger = logging.getLogger(__name__)
logger.info("transcription started", extra={"event": "transcription.started", "room_id": room.id})
```

```json
{"ts":"2026-11-06T10:15:04+0000","level":"INFO","logger":"portal.transcription.worker",
 "msg":"transcription started","request_id":"9f2c1ab7de4051bc",
 "event":"transcription.started","room_id":14}
```

An upstream proxy may supply `X-Request-ID` to extend an existing trace. The
value is untrusted input: it is stripped to `[A-Za-z0-9._-]` and capped at 64
characters so it cannot forge fields or inject a newline into a log line.

Background work that is not tied to a request can open its own trail with
`set_request_id(new_request_id())`, and must `reset_request_id(token)` afterwards.

## What to log

Use `extra` for machine-readable fields and keep the message a short constant
string, so records group cleanly.

- **Requests** — handled by the middleware: method, route template, status,
  duration. The route *template* is logged (`/items/{item_id}`), never the
  concrete path, to keep ids out of the label and cardinality low.
- **Lifecycle** — booth, transcription, relay and meeting-agent transitions,
  with opaque ids (`booth_id`, `room_id`, `event_id`, `user_id`).
- **Outbound calls** — operation, status, duration, retry count. Never headers
  or payloads.
- **Background jobs** — enqueue, start, finish, fail, with job name and id.
- **Auth** — the outcome as a success/failure code. Never the credential, and
  never the identity that was attempted.

Levels: `DEBUG` for development detail, `INFO` for normal lifecycle, `WARNING`
for a degraded-but-handled condition (including a 5xx response), `ERROR` with
`logger.exception` for an unhandled failure.

## What is never logged

The redaction filter runs on every handler and rewrites the message, its args,
and any string `extra` field. It covers:

- JWTs — `user_token`, `session_token`, listener and participant tokens
- `Authorization` headers and booth publish tickets
- Webhook signing secrets (`whsec_…`)
- Query-string and form credentials — `token`, `key`, `api_key`,
  `client_secret`, `code`, `access_token`, `refresh_token`, `password`,
  `signature`, `stream_key`, `join_code`
- RTMP/RTMPS destinations, which carry the YouTube stream key in the path
- Email addresses

Redaction is a safety net, not a licence. Do not pass a secret or an email to a
log call and rely on the filter — log the opaque id instead:

```python
# Wrong: leaks PII if the filter ever misses a shape.
logger.warning(f"Failed to invite {user.email}")

# Right: the id is enough to find the record.
logger.warning("Failed to send invite to user %s", user.id)
```

Two further rules come from `AGENTS.md` and are not covered by pattern
matching, so they stay the caller's responsibility:

- Never log the managed Jitsi **meeting URL** (`portal/meeting_source.py` logs
  `room_id` only).
- Never log or forward FFmpeg output containing the **relay destination**
  (`portal/program_relay.py`).

Prefer `%s` lazy interpolation over f-strings. An f-string is formatted before
the filter sees it, which costs work on suppressed records and makes the pattern
of a message harder to group.

## Testing redaction

`tests/test_logging_config.py` asserts each secret shape is removed and that
operationally useful text (booth ids, room ids) survives. When adding a log call
that handles a new kind of credential, add its shape to `_REDACTIONS` and a case
to that test.

A redaction must never change the number of `%` placeholders in a format
string — that would break interpolation and lose the line. `_redact_format_string()`
discards any such rewrite, and both formatters redact the final interpolated
message as a last guard.
