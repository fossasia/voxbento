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

The id is always minted by the portal. An upstream `X-Request-ID` is recorded
beside it as `upstream_request_id` rather than adopted — these endpoints are
public, so honouring the header would let a caller file its activity under
another trace's id. The supplied value is untrusted input either way: it is
stripped to `[A-Za-z0-9._-]` and capped at 64 characters so it cannot forge a
field or inject a newline into a log line.

Background work that is not tied to a request can open its own trail with
`set_request_id(new_request_id())`, and must `reset_request_id(token)` afterwards.

## What to log

Use `extra` for machine-readable fields and keep the message a short constant
string, so records group cleanly.

- **Requests** — handled by the middleware: method, route template, status,
  duration. The route *template* is logged (`/items/{item_id}`), never the
  concrete path, to keep ids out of the label and cardinality low. A request
  that matched no route is labelled `<unmatched>`; the concrete path is not a
  safe fallback, because an invite or revocation URL carries its token in it.
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

The redaction filter runs on every handler — including any the host installed
before startup — and rewrites the message, its args, every `extra` field
(recursing into dicts and lists) and the formatted traceback. It covers:

- JWTs — `user_token`, `session_token`, listener and participant tokens
- `Authorization` headers, both `Bearer` and `Basic`, and booth publish tickets
- Webhook signing secrets (`whsec_…`)
- Query-string, form and Python-mapping credentials — `token`, `key`,
  `api_key`, `client_secret`, `code`, `access_token`, `refresh_token`,
  `password`, `signature`, `stream_key`, `join_code` — including
  percent-encoded values
- RTMP/RTMPS destinations, which carry the YouTube stream key in the path
- Email addresses
- Any `extra` field *named* like a credential or personal detail
  (`password`, `api_key`, `cookie`, `email`, `phone`, `full_name`, `postal`,
  `address`, …), whose value need not look like a secret on its own

### The filter matches labels, not bare values

Redaction works on a credential's *context*, not its appearance. `code=ROOM42`
is redacted; a bare `ROOM42` in prose is not, because nothing distinguishes it
from an ordinary word. The same holds for a join code, a room name or an id
quoted without its label. JWTs, `whsec_` secrets and RTMP(S) URLs are the
exception — those are self-identifying by shape.

So write `logger.info("join rejected code=%s", code)` rather than
`logger.info("join rejected for %s", code)`: the labelled form is the one the
filter can see.

### Categories with no pattern

Some of the prohibited categories cannot be recognised by shape, so they are
caller rules rather than filter rules:

| Category | Status |
|---|---|
| Email addresses | Pattern-matched |
| Auth tokens, API keys, webhook secrets, join codes | Pattern-matched |
| Phone numbers, postal addresses | No such field exists in the schema; guarded by field name only |
| Payment data | Not handled by VoxBento at all |
| Full names | Cannot be pattern-matched — a name is just words. Log `user_id`, never a participant's display name |

A person's name looks like ordinary prose, so no filter can find it. Where a
participant display name would otherwise go in a log line, log the opaque id
instead. The same applies to any free-text a user typed, such as booth chat:
never log the body.

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
