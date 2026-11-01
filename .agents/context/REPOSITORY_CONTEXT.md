# VoxBento — Repository Context

> Primary context file for coding agents. Read this before any other context file.
> All facts derived from live implementation as of 2026-06-09.

---

## What Is This

**VoxBento** is a production-grade browser-first interpretation booth console for Eventyay live events.
Interpreters monitor a floor session via a self-hosted Jitsi iframe and publish audio via WebRTC/WHIP to MediaMTX.
Attendees receive sub-second audio via WHEP. All coordination flows through FastAPI WebSockets.

---

## Stack

| Layer | Technology | Version / Notes |
|---|---|---|
| Runtime | Python | 3.13.x (enforced in `pyproject.toml`) |
| Web framework | FastAPI + uvicorn | ASGI, async throughout |
| Media server | MediaMTX | `bluenviron/mediamtx:1` — WHIP/WHEP/RTSP/HLS |
| Floor monitoring | Jitsi Meet | `ghcr.io/jitsi/*:stable-11248` — rootless, read-only, self-hosted receive-only iframe |
| DB (dev) | SQLite + aiosqlite | `sqlite+aiosqlite:///./interpretation.db` |
| DB (prod) | PostgreSQL + asyncpg | via `DATABASE_URL` env override |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic | 38 migrations in `alembic/versions/` (001–038) |
| Auth | PyJWT (HS256) + bcrypt | three JWT token types (see Auth section) |
| API key crypto | Fernet (cryptography) | `portal/crypto.py`; keys hashed via SHA-256 |
| Transcription | faster-whisper + 4 cloud providers | `portal/transcription/` |
| Package manager | uv | `uv.lock` is the source of truth — never use pip |
| Templates | Jinja2 | `portal/templates/`, no build step |
| Frontend JS | Vanilla ES modules | `portal/static/js/`, no framework |
| Dependencies | See `pyproject.toml` | — |

---

## Entry Points

| File | Purpose |
|---|---|
| `fastapi_app.py` | Application lifespan, router aggregation |
| `portal/routers/` | All HTTP routes (pages, admin, REST API, API v1, OAuth, webhooks) |
| `portal/workspace_routing.py` | `/workspace` handler mapping, legacy organizer redirects, template URL context |
| `portal/websockets/` | WebSocket connection manager and handlers (`manager.py`, `handlers.py`) |
| `portal/config.py` | `Settings` (pydantic-settings); reads `.env` or env vars |
| `portal/models.py` | SQLAlchemy declarative models (events, rooms, booths, tokens, users, memberships, hosting requests, AI vocabulary) |
| `portal/database.py` | Async engine, session factory, all CRUD helpers |
| `portal/auth.py` | JWT creation/validation, user auth, role resolution |
| `portal/booth_state.py` | In-memory booth registry (`BoothRegistry`), participant state |
| `portal/booth_identity.py` | `make_booth_id`, `make_mediamtx_path`, validation |
| `portal/roles.py` | `Permission` enum, `ROLE_PERMISSIONS` mapping |
| `portal/crypto.py` | `encrypt_val`/`decrypt_val` using Fernet |
| `portal/transcription/` | Transcription subsystem (see `TRANSCRIPTION_MAP.md`) |
| `portal/translations/` | Translation subsystem — LLM workers, contextual prompts (`prompts.py`), glossaries (`vocabulary.py`) |
| `portal/tts/` | Direct-push Text-to-Speech (Deepgram Aura & Supertonic ONNX) |
| `portal/meeting_source.py` | Managed Jitsi floor agent lifecycle reconciler |
| `portal/program_ingest.py` | Program Stream Ingest floor authorization and reconciliation |
| `portal/program_relay.py` | YouTube RTMPS destination management and relay convergence |
| `portal/outage_log.py` | `OutageLog` — reconcile-loop failures logged once per outage, with reminders and recovery |
| `portal/user_admin.py` | Administrative access rules and user access switches |
| `portal/static/js/interpreter-booth.js` | Interpreter UI — WebRTC/WHIP, WebSocket, Jitsi, mic controls |
| `portal/static/js/whep-listener.js` | Listener WHEP client — RTCPeerConnection, auto-reconnect |
| `portal/static/js/admin.js` | Admin panel helpers |
| `portal/templates/` | Jinja2 HTML (base, booth, listener, auth, admin/) |
| `mediamtx.yml` | MediaMTX config — WHIP/WHEP paths, RTSP, Control API |
| `docker-compose.yml` | portal + mediamtx + jitsi-web/prosody/jicofo/jvb |
| `Dockerfile` | Portal container (uv-based, runs alembic then uvicorn) |
| `alembic/versions/` | 38 migrations (001–038) |

---

## Booth Identity Scheme

```
booth_id   = {event_slug}-{room_id}-{language_code}   e.g. "pycon2026-14-en"
mtx_path   = {event_slug}/{room_id}/{language_code}   e.g. "pycon2026/14/en"
whip_url   = {MEDIAMTX_WHIP_BASE}/{mtx_path}/whip
whep_url   = {MEDIAMTX_WHIP_BASE}/{mtx_path}/whep
rtsp_url   = rtsp://mediamtx:8554/{mtx_path}    (transcription use)
```

- `event_slug`: lowercase alphanumeric + hyphens, max 64 chars, validated by `portal.booth_identity.validate_event_slug`
- `room_id`: non-negative integer
- `language_code`: ISO 639-1 two-letter lowercase or 'floor', validated by `validate_language_code`

---

## Role Hierarchy

```
admin > event_owner > room_coordinator > interpreter
```

Four roles, defined once in `portal/roles.py` (#757): role names, plain-language
labels (`role_label`), where each can be given (`EVENT_ROLES`, `ROOM_ROLES`,
`BOOTH_ROLES`, `assignment_error`) and one `Permission` per action. Never type a
role name elsewhere; `tests/test_roles.py` fails if you do.

| Action | Admin | Event owner | Room coordinator | Interpreter |
|---|:-:|:-:|:-:|:-:|
| Manage users, Admins, hosting access, developer apps | ✓ | | | |
| See all events / create events (or hosting access) | ✓ | | | |
| Edit/delete event; manage rooms, booths, team, keys | ✓ | ✓ | | |
| Mission Control, floor transcription, choose active interpreter | ✓ | ✓ | ✓ | |
| Go live, booth chat, view booths | ✓ | ✓ | ✓ | ✓ |
| Can be given per | account | event | event, room, booth | event, booth |

"Admin" means only the platform role (`users.is_admin`, read from the database).
Access checks are named after who they admit: `require_admin` (Admins),
`require_event_owner`, `require_owner_or_coordinator`; Admins pass every check.
Hosting access is an account setting, not a role. Listening is not a role:
listener/embed tokens carry `kind: "listener"`. Booth participants are sent to
browsers with `can_go_live` / `can_set_active`, so client code never compares role names.

Defined in: `portal/roles.py` (`Permission` enum, `ROLE_PERMISSIONS` dict)
Enforced at: `portal/websockets/handlers.py` (WS `_handle_join`), `portal/routers/api.py` (WHIP endpoint) + `portal/booth_state.py`

---

## Auth System (Three JWT Types)

| Token | Cookie name | Claims | Issued by |
|---|---|---|---|
| Participant (invite) | `session_token` | booth_id, role, event_slug, room_id, language_code | `create_participant_token()` — `/join/{token}` |
| User (registered) | `user_token` | sub (user_id), email, display_name, is_admin, user=True | `create_user_token()` — POST /login |
| Admin | `user_token` | `sub`, `email`, `is_admin`, `user=True` | `create_user_token()` — POST /login (`/admin/login` redirects there) |

All JWT: HS256, secret = `settings.effective_jwt_secret` (falls back to `secret_key`), expiry = `jwt_expiry_seconds` (default 86400).

---

## WebSocket Protocol (`/ws/booth/{booth_id}`)

Client → Server messages:

| `type` | Handler | Effect |
|---|---|---|
| `booth:join` | `_handle_join` | Adds participant; first interpreter auto-assigned active |
| `booth:leave` | `_handle_leave` | Removes participant; triggers handoff if active leaves |
| `booth:chat` | `_handle_chat` | Appends message; broadcasts `booth:chat` + `booth:state` |
| `booth:set-active` | `_handle_set_active` | Changes active interpreter (room_coordinator/current-active only) |
| `booth:update-state` | `_handle_update_state` | Updates mic_active / ingest_connected flags |
| `booth:request-publish-ticket` | `_handle_request_publish_ticket` | Replies `booth:publish-ticket` with a single-use WHIP ticket (active interpreter, broadcast unlocked) |
| `booth:initiate-handoff` | `_handle_initiate_handoff` | Initiates interpreter handoff |
| `booth:accept-handoff` | `_handle_accept_handoff` | Accepts interpreter handoff |
| `booth:cancel-handoff` | `_handle_cancel_handoff` | Cancels interpreter handoff |

Server → Client broadcasts: `booth:joined`, `booth:state`, `booth:chat`, `booth:error`; direct reply: `booth:publish-ticket`

Role is NEVER trusted from client data — always read from the server-side `Session.granted_role` derived from cookies at connect time.

### Additional Real-Time Feeds
- Live captions feed: `/ws/captions/{booth_id}` — receives `caption` (`partial`, `final`, `clear`), `translated_caption`, and `booth:state` messages for listeners (authenticated via `resolve_ws_auth` when `BOOTH_ACCESS_TOKEN` is set). Supports canonical `{event_slug}-{room_id}-{language_code}`.
- Direct-push TTS feed: `/ws/tts/{room_id}/{language_code}/{booth_id}` — authorized before acceptance; receives structured binary bundle messages (1-byte version, 4-byte header length, JSON metadata header, optional 24 kHz 16-bit mono PCM payload) synthesized via Deepgram Aura or Supertonic ONNX. Translations are delivered concurrently via the caption feed and the TTS bundle header.

---

## Key Invariants

1. One active publisher per language channel (MediaMTX `overridePublisher: yes` enforces media level).
2. Interpreter mic audio NEVER routes to `AudioContext.destination`.
3. `uv.lock` is the single source of truth for Python dependencies.
4. No Flask, Socket.IO, aiortc, Vue, React, jQuery, inline `<script>` blocks.
5. All new Python imports use `portal.*` namespace.
6. `from __future__ import annotations` at the top of every Python file.

---

## Services & Ports

| Service | Port | Use |
|---|---|---|
| FastAPI portal | 8000 | HTTP + WebSocket |
| MediaMTX HTTP | 8888 | Health / internal |
| MediaMTX WHIP/WHEP | 8889 | WebRTC ingest + playback |
| MediaMTX ICE/UDP | 8189 | ICE candidate negotiation |
| MediaMTX Control API | 9997 | `alwaysAvailable` path creation |
| MediaMTX RTSP | 8554 | ffmpeg/transcription RTSP pull |
| Jitsi Web | 8443 / 8080 | HTTPS / HTTP floor monitoring |
| JVB UDP | 10000 | Jitsi media traffic |

---

## Related Context Files

- [ROUTE_MAP.md](ROUTE_MAP.md) — all HTTP + WS routes with methods and auth
- [DATABASE_MAP.md](DATABASE_MAP.md) — table schemas, relationships, CRUD functions
- [TRANSCRIPTION_MAP.md](TRANSCRIPTION_MAP.md) — provider architecture, audio pipeline
- [CHANGE_IMPACT_MAP.md](CHANGE_IMPACT_MAP.md) — which files to touch for common changes
- [AI_WORKFLOWS.md](AI_WORKFLOWS.md) — step-by-step agent workflows
- [TECHNICAL_DEBT_REPORT.md](TECHNICAL_DEBT_REPORT.md) — known issues and gaps
