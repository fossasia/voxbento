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
| Floor monitoring | Jitsi Meet | `jitsi/*:stable-9823` — self-hosted, receive-only iframe |
| DB (dev) | SQLite + aiosqlite | `sqlite+aiosqlite:///./interpretation.db` |
| DB (prod) | PostgreSQL + asyncpg | via `DATABASE_URL` env override |
| ORM / migrations | SQLAlchemy 2.0 async + Alembic | 8 migrations in `alembic/versions/` |
| Auth | PyJWT (HS256) + bcrypt | three JWT token types (see Auth section) |
| API key crypto | Fernet (cryptography) | `portal/crypto.py`; keys hashed via SHA-256 |
| Transcription | faster-whisper + 4 cloud providers | `portal/transcription/` |
| Package manager | uv | `uv.lock` is the source of truth — never use pip |
| Templates | Jinja2 | `templates/`, no build step |
| Frontend JS | Vanilla ES modules | `static/js/`, no framework |
| Dependencies | See `pyproject.toml` | — |

---

## Entry Points

| File | Purpose |
|---|---|
| `fastapi_app.py` | Application lifespan, router aggregation |
| `portal/routers/` | All HTTP routes (pages, admin, REST API) |
| `portal/websockets/` | WebSocket connection manager and handlers |
| `portal/config.py` | `Settings` (pydantic-settings); reads `.env` or env vars |
| `portal/models.py` | SQLAlchemy declarative models (7 tables) |
| `portal/database.py` | Async engine, session factory, all CRUD helpers |
| `portal/auth.py` | JWT creation/validation, user auth, role resolution |
| `portal/booth_state.py` | In-memory booth registry (`BoothRegistry`), participant state |
| `portal/booth_identity.py` | `make_booth_id`, `make_mediamtx_path`, validation |
| `portal/roles.py` | `Permission` enum, `ROLE_PERMISSIONS` mapping |
| `portal/crypto.py` | `encrypt_val`/`decrypt_val` using Fernet |
| `portal/transcription/` | Transcription subsystem (see `TRANSCRIPTION_MAP.md`) |
| `static/js/interpreter-booth.js` | Interpreter UI — WebRTC/WHIP, WebSocket, Jitsi, mic controls |
| `static/js/whep-listener.js` | Listener WHEP client — RTCPeerConnection, auto-reconnect |
| `static/js/admin.js` | Admin panel helpers |
| `templates/` | Jinja2 HTML (base, booth, listener, auth, admin/) |
| `mediamtx.yml` | MediaMTX config — WHIP/WHEP paths, RTSP, Control API |
| `docker-compose.yml` | portal + mediamtx + jitsi-web/prosody/jicofo/jvb |
| `Dockerfile` | Portal container (uv-based, runs alembic then uvicorn) |
| `alembic/versions/` | 8 migrations (001–008) |

---

## Booth Identity Scheme

```
booth_id   = {event_slug}-{language_code}   e.g. "pycon2026-en"
mtx_path   = {event_slug}/{language_code}   e.g. "pycon2026/en"
whip_url   = {MEDIAMTX_WHIP_BASE}/{mtx_path}/whip
whep_url   = {MEDIAMTX_WHIP_BASE}/{mtx_path}/whep
rtsp_url   = rtsp://mediamtx:8554/{mtx_path}    (transcription use)
```

- `event_slug`: lowercase alphanumeric + hyphens, max 64 chars, validated by `portal.booth_identity.validate_event_slug`
- `language_code`: ISO 639-1 two-letter lowercase, validated by `validate_language_code`

---

## Role Hierarchy

```
super_admin > event_owner > room_coordinator > interpreter
```

| Role | Go Live | Set Active | Chat | Manage Booths | Manage Events |
|---|---|---|---|---|---|
| super_admin | ✓ | ✓ | ✓ | ✓ | ✓ |
| event_owner | ✓ | ✓ | ✓ | ✓ | — |
| room_coordinator | ✓ | ✓ | ✓ | — | — |
| interpreter | ✓ | — | ✓ | — | — |

Defined in: `portal/roles.py` (`Permission` enum, `ROLE_PERMISSIONS` dict)
Enforced at: `portal/websockets/handlers.py` (WS `_handle_join`), `portal/routers/api.py` (WHIP endpoint) + `portal/booth_state.py`

---

## Auth System (Three JWT Types)

| Token | Cookie name | Claims | Issued by |
|---|---|---|---|
| Participant (invite) | `session_token` | booth_id, role, event_slug, language_code | `create_participant_token()` — `/join/{token}` |
| User (registered) | `user_token` | sub (user_id), email, display_name, is_admin, user=True | `create_user_token()` — POST /login |
| Admin | `admin_token` | admin=True | `create_admin_token()` — POST /admin/login |

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

Server → Client broadcasts: `booth:joined`, `booth:state`, `booth:chat`, `booth:error`

Role is NEVER trusted from client data — always read from the server-side `Session.granted_role` derived from cookies at connect time.

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
