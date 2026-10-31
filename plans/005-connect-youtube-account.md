# Plan 005: Let event owners "Sign in with YouTube" and pick a broadcast

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report; do not improvise. When done, update the status row for this plan
> in `plans/README.md`, unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**: `git diff --stat 250aefde..HEAD -- portal/config.py portal/models.py portal/program_relay.py portal/routers/program_ingest.py portal/workspace_routing.py portal/templates/admin/_program_ingest.html portal/static/js/program-ingest.js fastapi_app.py alembic/versions`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition. If a migration numbered `039` already
> exists, use the next free number and update `down_revision` accordingly.

## Status

- **Priority**: P1
- **Effort**: L (split into 2 PRs: Steps 1–6 backend; Steps 7–9 UI + docs)
- **Risk**: MED (new third-party OAuth, stores a long-lived refresh token)
- **Depends on**: `plans/002-youtube-intake-spike.md` step 3 (confirms `streamName` and `closedCaptionsIngestionUrl` are returned). Works with or without plan 004.
- **Category**: direction
- **Planned at**: commit `250aefde`, 2026-11-01
- **Issue**: https://github.com/fossasia/voxbento/issues/1087

## Why this matters

Relay mode (#749) asks organisers to copy a YouTube stream key out of YouTube Studio
and paste it into VoxBento, then copy VoxBento's key into OBS. People get this wrong,
and they also want to "just give VoxBento the YouTube link". YouTube does **not**
allow pulling audio from a watch URL (Developer Policies III.E.1 and the
"separate, isolate, or modify the audio or video components" rule), but the
**YouTube Live Streaming API** lets the channel owner authorise VoxBento to read
their broadcasts and the bound stream's ingest address and key. With one Google
consent screen, VoxBento can list the channel's live/upcoming broadcasts, accept a
pasted link, fill in the relay destination automatically (the key is never shown),
show YouTube-side status, and enable native captions (plan 006).

## Current state

- **No Google/YouTube API code exists.** `portal/routers/oauth.py` is VoxBento acting
  as an OAuth *provider* for developer apps; it is unrelated, so don't reuse it.
- Relay destination storage, `portal/program_relay.py`:
  - `store_destination(room, server_url, stream_key)` (line ~95) normalises and
    Fernet-encrypts the key into `room.program_relay_key_enc`, the server into
    `room.program_relay_server`, sets `program_relay_updated_at`. It raises
    `ValueError` (bad input) or `RuntimeError` (no encryption key).
  - `normalize_destination()` maps `a.rtmp.youtube.com`/`b.rtmp.youtube.com` to their
    `rtmps` hosts (`YOUTUBE_RTMPS_HOSTS`, line ~37) and checks `allowed_hosts()`.
  - `RELAY_MODE = "rtmps_relay"`.
- Router conventions, `portal/routers/program_ingest.py`:
  - `router = APIRouter()`, routes under `/admin/events/{event_id}/rooms/{room_id}/…`
    with `dependencies=[Depends(require_event_owner)]`.
  - `_check_origin(request)` rejects a foreign `Origin` on state-changing requests.
  - Room changes run under `async with room_lock(room_id): async with get_session() as session:`
    and re-check `room.event_id == event_id` (404 otherwise).
  - Responses are `JSONResponse(..., headers={"Cache-Control": "no-store"})`; secrets are
    never returned after creation; logs carry ids only: `logger.info("program_relay destination_set room_id=%s", room_id)`.
- Auth: `portal/auth.py` `require_event_owner(request)` (line ~409) reads `event_id`
  from path params; `resolve_principal(request, db_session)` (line ~311) returns a dict
  with `is_admin`, `owned_event_ids`, and the user id. `get_current_user(request)`
  decodes the `user_token` cookie.
- `portal/utils.py:17` `safe_redirect(url)` is mandatory for redirects (no open redirects).
- Workspace mapping: `portal/workspace_routing.py` allowlist regexes (line ~32 has
  `r"/workspace/events/\d+/rooms/\d+/(?:program-ingest|program-relay|program-sender|meeting-source)"`).
  New owner routes must be added or `/workspace/...` URLs 404.
- Crypto: `portal/crypto.py` `encrypt_val(str) -> str`, `decrypt_val(str) -> str` (MultiFernet).
- HTTP: `portal/globals.py` `get_http_client()` returns the shared `httpx.AsyncClient`.
- Models: `portal/models.py` uses SQLAlchemy 2.0 `Mapped[...]`; exemplar `EventAPIKey`
  (line ~687): `event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)`,
  `created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)`.
  `Room` program columns are at lines ~133–149.
- Migrations: `alembic/versions/038_add_public_id_to_invite_tokens.py` is the latest;
  revision ids are strings `"038"`, `down_revision = "037"`; migrations inspect columns
  before adding (idempotent) and use `op.batch_alter_table` for SQLite.
- Tests: `tests/test_program_relay.py` fixture `relay_env` shows DB setup
  (`configure("sqlite+aiosqlite://")`, `init_db()`, `monkeypatch.setattr(settings, …)`)
  and an authenticated client via `create_user_token`. `tests/test_program_ingest.py`
  uses `httpx.MockTransport`; reuse that style for Google endpoints.

YouTube API facts (reference docs, verified 2026-11-01):
- Authorise: `https://accounts.google.com/o/oauth2/v2/auth` with `response_type=code`,
  `access_type=offline`, `prompt=consent`, `include_granted_scopes=true`, PKCE `S256`.
  Token: `POST https://oauth2.googleapis.com/token`. Revoke: `POST https://oauth2.googleapis.com/revoke?token=…`.
- Scope: `https://www.googleapis.com/auth/youtube.force-ssl` (needed by plan 006 to set
  caption type; also covers all reads below). It is a *sensitive* scope: unverified apps
  are limited to 100 test users and show a warning.
- `GET https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true`
- `GET …/liveBroadcasts?part=snippet,contentDetails,status&broadcastStatus=active|upcoming&broadcastType=all&maxResults=50`
  (`broadcastStatus` and `id` are alternative filters; use one per call).
- `GET …/liveBroadcasts?part=snippet,contentDetails,status&id=<videoId>`. A broadcast
  not on the authorised channel returns no item.
- `contentDetails.boundStreamId` → `GET …/liveStreams?part=cdn,status,contentDetails&id=<id>`
  → `cdn.ingestionInfo.ingestionAddress`, `cdn.ingestionInfo.streamName` (the stream key),
  `contentDetails.closedCaptionsIngestionUrl`, `status.streamStatus`, `status.healthStatus.status`.
- `status.lifeCycleStatus`: `created|ready|testStarting|testing|liveStarting|live|complete|revoked`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Install | `uv sync --python 3.13 --dev` | exit 0 |
| Migrate | `uv run alembic upgrade head` | exit 0, ends at `039` |
| Downgrade check | `uv run alembic downgrade -1 && uv run alembic upgrade head` | exit 0 |
| Focused tests | `uv run pytest tests/test_youtube_account.py -q` | all pass |
| Regression | `uv run pytest tests/test_program_relay.py tests/test_program_ingest.py tests/test_workspace_routing.py -q` (skip a file that does not exist) | all pass |
| Full | `uv run pytest tests/ -q` | all pass |
| Lint | `uv run ruff check .` | exit 0 |
| JS | `node --check portal/static/js/program-ingest.js && node --check portal/static/js/youtube-connect.js` | exit 0 |

## Suggested executor toolkit

- Read before starting: developers.google.com/youtube/v3/live/docs/liveBroadcasts/list,
  …/liveStreams, developers.google.com/identity/protocols/oauth2/web-server (PKCE + offline access),
  developers.google.com/youtube/terms/developer-policies sections III.E and III.F.
- Skills if available: `security-audit` (OAuth state/PKCE review), `route-analysis`.

## Scope

**In scope**:
- `portal/config.py` (settings + production validation)
- `portal/models.py` (new `YouTubeConnection`; two `Room` columns)
- `alembic/versions/039_youtube_connections.py` (create)
- `portal/youtube_account.py` (create): Google OAuth + API client, pure link parser
- `portal/routers/youtube.py` (create) + registration in `fastapi_app.py`
- `portal/workspace_routing.py` (allowlist regexes)
- `portal/routers/program_ingest.py` (add a `youtube` block to `ingest_status`)
- `portal/templates/admin/_youtube_connect.html` (create), include it from `_program_ingest.html`
- `portal/static/js/youtube-connect.js` (create)
- `tests/test_youtube_account.py` (create)
- Docs: `docs/youtube-connect.md` (create), `website/docs/configuration/environment-variables.mdx`,
  `.agents/context/ROUTE_MAP.md`, `.agents/context/DATABASE_MAP.md`, `agents.md` (Module Ownership row only)

**Out of scope**:
- Posting captions to YouTube (plan 006). Store nothing caption-related yet.
- Transitioning broadcasts (`liveBroadcasts.transition`), creating/binding streams,
  editing titles: VoxBento never changes the organiser's YouTube setup in this plan.
- Fetching, downloading or embedding YouTube media of any kind.
- The guided "From a YouTube stream" room-audio choice (plan 007).
- `portal/routers/oauth.py` (VoxBento's own OAuth provider).

## Git workflow

- Branch from `dev`: `feat/youtube-connect`. Two PRs: `feat(youtube): connect a YouTube channel and pick a broadcast` (backend),
  `feat(youtube): room page controls for the YouTube connection` (UI + docs).
- Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Settings

`portal/config.py`, after the relay settings:
```python
# "Sign in with YouTube" (Google OAuth web client). Off unless both are set.
youtube_oauth_client_id: str = ""
youtube_oauth_client_secret: str = ""
youtube_status_cache_secs: float = 30.0

@property
def youtube_connect_enabled(self) -> bool:
    return bool(self.youtube_oauth_client_id and self.youtube_oauth_client_secret)
```
In `validate_production_secrets()` add: if exactly one of id/secret is set, raise
`RuntimeError("Set both YOUTUBE_OAUTH_CLIENT_ID and YOUTUBE_OAUTH_CLIENT_SECRET, or neither.")`.
The redirect URI is always `f"{settings.public_base_url.rstrip('/')}/youtube/oauth/callback"`.

**Verify**: `uv run python -c "from portal.config import settings; print(settings.youtube_connect_enabled)"` → `False`.

### Step 2: Model + migration

`portal/models.py`:
```python
class YouTubeConnection(Base):
    """One YouTube channel authorised per event. The refresh token is Fernet-encrypted and write-only."""
    __tablename__ = "youtube_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), unique=True, index=True)
    channel_id: Mapped[str] = mapped_column(String(64))
    channel_title: Mapped[str] = mapped_column(String(200))
    refresh_token_enc: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(500))
    connected_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
```
On `Room` add:
```python
youtube_broadcast_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
youtube_stream_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```
Create `alembic/versions/039_youtube_connections.py` (`revision = "039"`,
`down_revision = "038"`), idempotent like 038: create the table if missing, add the two
room columns with `batch_alter_table` if missing; `downgrade()` drops them.

**Verify**: `uv run alembic upgrade head` → exit 0; `uv run alembic downgrade -1 && uv run alembic upgrade head` → exit 0.

### Step 3: `portal/youtube_account.py`

`from __future__ import annotations`; module docstring states the policy boundary:
*"Reads broadcast metadata and the organiser's own ingest settings. Never fetches,
stores or embeds YouTube audio/video."* Contents:

1. Constants: `AUTH_URL`, `TOKEN_URL`, `REVOKE_URL`, `API_BASE = "https://www.googleapis.com/youtube/v3"`,
   `SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"`, `VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")`.
2. `def parse_broadcast_id(text: str) -> str | None` (pure). Accept: bare 11-char id;
   `https://(www.|m.)youtube.com/watch?v=<id>`; `…/live/<id>`; `https://youtu.be/<id>`;
   `https://studio.youtube.com/video/<id>/livestreaming`. Reject any other host, any
   non-https scheme, and ids not matching `VIDEO_ID`. Strip whitespace.
3. `def pkce_pair() -> tuple[str, str]`: verifier `secrets.token_urlsafe(64)`, challenge
   `base64url(sha256(verifier))` without padding.
4. `def authorization_url(state: str, challenge: str) -> str`: urlencoded query with
   `client_id`, `redirect_uri`, `response_type=code`, `scope=SCOPE`, `access_type=offline`,
   `prompt=consent`, `include_granted_scopes=true`, `state`, `code_challenge`,
   `code_challenge_method=S256`.
5. `@dataclass class Broadcast`: `id, title, scheduled_start, life_cycle_status, bound_stream_id, latency_preference, closed_captions_type`.
6. `@dataclass class StreamInfo`: `id, ingestion_address, stream_status, health_status`,
   and `stream_name: str = field(repr=False)`, `captions_url: str | None = field(repr=False, default=None)`.
   (`repr=False` keeps secrets out of logs and tracebacks.)
7. `class YouTubeAuthError(Exception)` (needs re-sign-in) and `class YouTubeAPIError(Exception)`
   (carries `status: int` and a short reason **from the JSON `error.errors[0].reason` only**,
   never the raw body).
8. In-memory access-token cache `_tokens: dict[int, tuple[str, float]]` keyed by event id.
   `async def access_token(connection) -> str`: return cached if > 60 s left; else POST
   `TOKEN_URL` with `grant_type=refresh_token`. On HTTP 400 with `error == "invalid_grant"`
   raise `YouTubeAuthError("YouTube access was revoked or expired. Sign in with YouTube again.")`.
9. `async def exchange_code(code, verifier) -> dict` (returns `refresh_token`, `access_token`,
   `expires_in`, `scope`). If `refresh_token` is missing raise `YouTubeAuthError` (consent incomplete).
10. API helpers via `get_http_client()` with `timeout=10.0`:
    `my_channel(token)`, `list_broadcasts(connection)` (two calls: `active`, `upcoming`; merge,
    de-duplicate by id, sort live first then by scheduled start), `get_broadcast(connection, id)`
    (returns `None` when no item), `get_stream(connection, stream_id)`, `revoke(refresh_token)`
    (best effort; ignore network errors).
11. `async def link_broadcast(session, room, connection, text) -> Broadcast`:
    parse id (ValueError on bad input), `get_broadcast` (ValueError
    `"That broadcast is not on the connected channel."` when None), set
    `room.youtube_broadcast_id`, and when `bound_stream_id` exists set
    `room.youtube_stream_id`, `get_stream`, and **if** `room.program_ingest_mode == program_relay.RELAY_MODE`
    call `program_relay.store_destination(room, stream.ingestion_address, stream.stream_name)`.
    Return the broadcast. Never return or log `stream_name`.
12. Status cache: `async def room_status(connection, room) -> dict` returning
    `{"broadcast_id", "title", "life_cycle_status", "stream_status", "health", "latency_preference", "checked_at"}`
    cached per room for `settings.youtube_status_cache_secs` to save quota.
13. Logging: `logger.info("youtube event_id=%s action=%s", …)` style; never log tokens,
    stream keys, caption URLs, codes or response bodies.

**Verify**: `uv run python -c "from portal.youtube_account import parse_broadcast_id as p; print(p('https://youtu.be/dQw4w9WgXcQ'), p('https://evil.example/watch?v=dQw4w9WgXcQ'))"` → `dQw4w9WgXcQ None`.

### Step 4: Routes, `portal/routers/youtube.py`

All JSON routes: `Cache-Control: no-store`; state-changing ones call the same origin
check as `program_ingest._check_origin` (import or copy it); all 404 when
`not settings.youtube_connect_enabled` except status (returns `available: false`).

| Method | Path | Guard | Behaviour |
|---|---|---|---|
| GET | `/admin/events/{event_id}/youtube` | `require_event_owner` | `{available, connected, channel_title, channel_id, connected_at, last_error}` |
| GET | `/admin/events/{event_id}/youtube/connect?room_id=N` | `require_event_owner` | Create nonce + PKCE; set HttpOnly, Secure (unless `settings.debug`), SameSite=Lax cookie `vb_yt_oauth` (path `/youtube/oauth`, max-age 600) holding a JWT signed with `settings.effective_jwt_secret` containing `{nonce, verifier, event_id, room_id, sub}`; 303 to `authorization_url(nonce, challenge)` |
| GET | `/youtube/oauth/callback` | user cookie | Decode cookie JWT (reject expired/missing); `state` must equal `nonce` (`secrets.compare_digest`); current `user_token` sub must equal cookie `sub`; re-check ownership with `resolve_principal` (`is_admin` or `event_id in owned_event_ids`). On `error` param (user cancelled), redirect back with `?youtube=cancelled`. Exchange code, read `my_channel`, upsert `YouTubeConnection` (encrypt refresh token; replace an existing row for the event and best-effort revoke the old token), delete the cookie, `safe_redirect` to the room page (`/workspace/events/{id}/rooms/{room}/?youtube=connected#room-audio`, or `/admin/...` for Admins without ownership, following `portal/workspace_routing.py` helpers) |
| GET | `/admin/events/{event_id}/youtube/broadcasts` | `require_event_owner` | `{"broadcasts": [ {id, title, scheduled_start, life_cycle_status, latency_preference} ]}`; 409 with `last_error` on `YouTubeAuthError` |
| PUT | `/admin/events/{event_id}/rooms/{room_id}/youtube/broadcast` | `require_event_owner` | Body `{"broadcast": str (≤ 300)}`. Under `room_lock`, `link_broadcast`. Returns `{broadcast_id, title, life_cycle_status, stream_bound, destination_configured}`. 422 on ValueError, 409 on auth error, 502 on API error |
| DELETE | `/admin/events/{event_id}/rooms/{room_id}/youtube/broadcast` | `require_event_owner` | Clear `youtube_broadcast_id`/`youtube_stream_id`. Leave the relay destination (owner removes it explicitly) |
| DELETE | `/admin/events/{event_id}/youtube` | `require_event_owner` | Best-effort `revoke`, delete row, clear both room columns for every room of the event, drop cached tokens/status |

Register in `fastapi_app.py` next to `program_ingest_router`. Add to the
`portal/workspace_routing.py` allowlist:
`re.compile(r"/workspace/events/\d+/youtube(?:/(?:connect|broadcasts))?/?")` and
`re.compile(r"/workspace/events/\d+/rooms/\d+/youtube/broadcast")`.
The callback path `/youtube/oauth/callback` is **not** a workspace route. Keep `/oauth/` in
its path: `fastapi_app.py` `_UvicornTokenRedactor` only redacts `code=`/`token=` query values
in access logs for paths containing `/api/`, `/embed/`, `/overlay/`, `/ws/`, `/oauth/` or `/internal/`.

**Verify**: `uv run python -c "import fastapi_app; print([r.path for r in fastapi_app.app.routes if 'youtube' in r.path])"` → lists the 7 paths.

### Step 5: Room status

In `portal/routers/program_ingest.py::ingest_status`, add
`"youtube": {...}` when `settings.youtube_connect_enabled` and the room has
`youtube_broadcast_id` and the event has a connection: the `room_status()` dict, or
`{"error": "<message>"}` on `YouTubeAuthError`/`YouTubeAPIError`. Never block the
endpoint for more than the API timeout; on any exception log
`"youtube status_failed room_id=%s"` and return `{"error": "YouTube status unavailable."}`.

**Verify**: `uv run pytest tests/test_program_ingest.py tests/test_program_relay.py -q` → all pass.

### Step 6: Tests (`tests/test_youtube_account.py`)

Use an in-memory DB fixture like `relay_env`, settings with fake client id/secret,
`program_relay_enabled=True`, and a `httpx.MockTransport` handler routing on
`request.url.host`/`path` that returns canned Google JSON. Patch
`portal.youtube_account.get_http_client` to return `httpx.AsyncClient(transport=…)`.
Cases (≥ 14):
1. `parse_broadcast_id`: 5 accepted forms, plus rejects for http://, other hosts, 10-char id, empty.
2. `authorization_url` contains `code_challenge_method=S256`, `access_type=offline`, `prompt=consent`, the exact scope and redirect URI.
3. Connect route sets the `vb_yt_oauth` cookie (HttpOnly, path `/youtube/oauth`) and 303s to Google.
4. Callback with mismatched `state` → 400 and no row created.
5. Callback with a different signed-in user than the cookie `sub` → 403.
6. Callback happy path: row created, `refresh_token_enc` ≠ plaintext and `decrypt_val` round-trips, cookie deleted, redirect is same-origin.
7. Callback when Google omits `refresh_token` → error redirect, no row.
8. `GET …/youtube/broadcasts` merges active + upcoming, live first.
9. `PUT …/youtube/broadcast` with a watch URL in relay mode stores the destination: `program_relay.destination(room)` starts with `rtmps://a.rtmps.youtube.com/live2/` and the response JSON contains no stream key (assert the fake key string is absent from `response.text`).
10. Same in `whip` mode: links broadcast, does **not** store a destination.
11. Broadcast not on channel → 422 with the not-on-channel message.
12. `invalid_grant` on refresh → 409 and `last_error` set.
13. `DELETE …/youtube` calls revoke and clears room columns.
14. Non-owner user → 403 on every owner route; feature disabled → 404.
15. Log hygiene: run case 9 with `caplog` at DEBUG; assert neither the fake key, the fake refresh token nor the fake access token appears in `caplog.text`.
16. Access-log redaction: build a `logging.LogRecord` whose message is
    `'GET /youtube/oauth/callback?code=SECRETCODE&state=x HTTP/1.1'`, pass it through
    `fastapi_app._UvicornTokenRedactor().filter(record)`, and assert `SECRETCODE` is gone from `record.getMessage()`.

**Verify**: `uv run pytest tests/test_youtube_account.py -q` → ≥ 14 passed.

### Step 7: UI partial (PR 2)

Create `portal/templates/admin/_youtube_connect.html`, included near the top of the
relay destination step in `_program_ingest.html` (and reusable by plan 007). Contents
(no inline script; `data-*` URLs):
- If not available: nothing rendered.
- Not connected: **Sign in with YouTube** button (`<a class='btn btn-primary' href='…/youtube/connect?room_id=…'>`),
  one-line explainer "VoxBento reads your broadcasts and fills in the stream key for you. It never downloads your video.", and "or paste the stream key manually" (existing form stays).
- Connected: channel title, **Disconnect**, a broadcast `<select>` filled from
  `/youtube/broadcasts`, a text field "or paste the broadcast link", **Use this broadcast**,
  and a status line (`life_cycle_status`, stream health).
Create `portal/static/js/youtube-connect.js` (plain ES module, same style as
`program-ingest.js`): load status, load broadcasts, PUT/DELETE calls with
`credentials: 'same-origin'` and JSON bodies, show messages from `detail`.
Follow Google branding guidance for the button text ("Sign in with YouTube" / "Continue with Google").

**Verify**: `node --check portal/static/js/youtube-connect.js` → exit 0; the room page
renders in `uv run pytest tests/test_program_ingest.py -q`.

### Step 8: Docs

- Create `docs/youtube-connect.md`: what it does / does not do (policy boundary);
  operator setup (Google Cloud project, enable YouTube Data API v3, OAuth consent
  screen, web client, redirect URI `{PUBLIC_BASE_URL}/youtube/oauth/callback`, test
  users, verification for > 100 users with privacy-policy URL); env vars; quota notes
  (status cached 30 s per room; list calls are cheap); revocation (Disconnect, or
  myaccount.google.com/permissions); data stored (channel id/title, encrypted refresh token).
- `website/docs/configuration/environment-variables.mdx`: the three new variables.
- `.agents/context/ROUTE_MAP.md`, `.agents/context/DATABASE_MAP.md`: new routes, table and columns.
- `agents.md` Module Ownership table: add `portal/youtube_account.py`: Google OAuth and
  YouTube Live API metadata (no media).

**Verify**: `grep -n "youtube/oauth/callback" docs/youtube-connect.md` → ≥ 1 match.

### Step 9: Manual check (record in the PR)

With a test Google project and an unlisted broadcast: Sign in → broadcast listed →
choose it in a relay-mode room → *VoxBento → YouTube* goes Live when OBS streams to
VoxBento, and YouTube Studio shows the stream. Disconnect → connection gone in
myaccount.google.com/permissions.

## Test plan

See Step 6 (≥ 14 cases). Pattern: `tests/test_program_relay.py` fixture +
`httpx.MockTransport` like `tests/test_program_ingest.py`. Regression:
`tests/test_program_relay.py`, `tests/test_program_ingest.py` unchanged and passing.

## Done criteria

- [ ] `uv run alembic upgrade head` and downgrade/upgrade round-trip exit 0
- [ ] `uv run pytest tests/ -q` exits 0; `tests/test_youtube_account.py` ≥ 14 passed
- [ ] `uv run ruff check .` exits 0
- [ ] `grep -rn "stream_name\|refresh_token" portal --include=*.py | grep -i "log"` shows no logging of those values
- [ ] `grep -rn "youtube.com/embed\|iframe_api\|yt-dlp\|youtube-dl\|streamlink" portal` returns no matches
- [ ] `git diff --name-only dev...HEAD` lists only in-scope files
- [ ] `plans/README.md` status row updated

## STOP conditions

- `plans/002-results.md` says `cdn.ingestionInfo.streamName` is not returned for the
  owner's broadcast. The automatic-key feature is then impossible; report back.
- The ownership re-check in the callback cannot be implemented with `resolve_principal`
  as described (signature drifted).
- Google requires a restricted (not sensitive) scope review for `youtube.force-ssl` at
  the time of implementation. Report it; it changes the rollout cost.
- Any step seems to need fetching or embedding YouTube media.

## Maintenance notes

- Access tokens live only in process memory (single portal replica, like `BoothRegistry`).
  Horizontal scaling needs a shared cache; refresh tokens are already in the DB.
- If an organiser resets their stream key in Studio, the relay fails; re-choosing the
  broadcast (**Use this broadcast**) refreshes the key. A future improvement: refresh
  automatically when relay health reports an authentication failure.
- Reviewers: scrutinise the callback (state, PKCE, user binding, ownership re-check),
  log hygiene, and that disconnect revokes the token.
- Deferred to plan 006: caption ingestion URL use. Deferred to plan 007: room-audio flow.
