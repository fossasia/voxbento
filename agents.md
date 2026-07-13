# VoxBento — Agent Guide

> **Primary context file for coding agents.** Read this entire file before making changes.
> All implementation details are derived from the live codebase. Constraints below are non-negotiable.

---

## Quick-Start Context

Read these files in order before any task:

1. **This file** — guardrails, invariants, ownership
2. [`.agents/context/REPOSITORY_CONTEXT.md`](.agents/context/REPOSITORY_CONTEXT.md) — stack, auth, booth identity, ports, role hierarchy
3. [`.agents/context/CHANGE_IMPACT_MAP.md`](.agents/context/CHANGE_IMPACT_MAP.md) — which files to touch for your specific task
4. [`.agents/context/ROUTE_MAP.md`](.agents/context/ROUTE_MAP.md) — all HTTP + WS routes
5. [`.agents/context/DATABASE_MAP.md`](.agents/context/DATABASE_MAP.md) — table schemas, migrations, CRUD helpers
6. [`.agents/context/TRANSCRIPTION_MAP.md`](.agents/context/TRANSCRIPTION_MAP.md) — provider architecture, audio pipeline
7. [`.agents/context/AI_WORKFLOWS.md`](.agents/context/AI_WORKFLOWS.md) — step-by-step task playbooks
8. [`.agents/context/TECHNICAL_DEBT_REPORT.md`](.agents/context/TECHNICAL_DEBT_REPORT.md) — known issues and gaps

### File-Specific Instructions (apply when editing)

| File pattern | Instruction file |
|---|---|
| `**/*.py` | [`.github/instructions/python.instructions.md`](.github/instructions/python.instructions.md) |
| `**/*.{js,ts,vue}` | [`.github/instructions/js.instructions.md`](.github/instructions/js.instructions.md) |
| `**/jinja-templates/**/*.jinja` | [`.github/instructions/jinja.instructions.md`](.github/instructions/jinja.instructions.md) |

### Skills (use for specialised tasks)

| Task | Skill |
|---|---|
| Navigate codebase | [`.agents/skills/repo-navigation/SKILL.md`](.agents/skills/repo-navigation/SKILL.md) |
| Architecture review | [`.agents/skills/architecture-review/SKILL.md`](.agents/skills/architecture-review/SKILL.md) |
| Route analysis | [`.agents/skills/route-analysis/SKILL.md`](.agents/skills/route-analysis/SKILL.md) |
| Database analysis | [`.agents/skills/database-analysis/SKILL.md`](.agents/skills/database-analysis/SKILL.md) |
| Transcription changes | [`.agents/skills/transcription-analysis/SKILL.md`](.agents/skills/transcription-analysis/SKILL.md) |
| Provider integration | [`.agents/skills/provider-analysis/SKILL.md`](.agents/skills/provider-analysis/SKILL.md) |
| PR review | [`.agents/skills/pr-review/SKILL.md`](.agents/skills/pr-review/SKILL.md) |
| Security audit | [`.agents/skills/security-audit/SKILL.md`](.agents/skills/security-audit/SKILL.md) |
| Docker / infra | [`.agents/skills/docker-review/SKILL.md`](.agents/skills/docker-review/SKILL.md) |
| Deployment | [`.agents/skills/deployment-review/SKILL.md`](.agents/skills/deployment-review/SKILL.md) |
| Incident response | [`.agents/skills/incident-investigation/SKILL.md`](.agents/skills/incident-investigation/SKILL.md) |
| Writing tests | [`.agents/skills/test-generation/SKILL.md`](.agents/skills/test-generation/SKILL.md) |
| Production readiness | [`.agents/skills/production-readiness-review/SKILL.md`](.agents/skills/production-readiness-review/SKILL.md) |

---

## Product Intent

VoxBento is a production-grade **browser-first interpretation booth console** for Eventyay live events.

- Interpreters monitor the floor session via a self-hosted Jitsi iframe (receive-only).
- Interpreters publish audio via browser WebRTC → WHIP → MediaMTX.
- Attendees receive sub-second audio via WHEP from MediaMTX.
- All coordination (booth state, roles, chat, handoff) flows through FastAPI WebSockets.

**Stack:** FastAPI (ASGI/uvicorn) + MediaMTX (WHIP/WHEP/RTSP) + self-hosted Jitsi Meet (stable-9823).
**No** Flask, Socket.IO, aiortc.

---

## Module Ownership

| File / Directory | Owns |
|---|---|
| `fastapi_app.py` | Application lifespan, router aggregation, global exception handlers |
| `portal/routers/` | All HTTP routes (pages, admin, REST API), Jinja2 template rendering |
| `portal/websockets/` | WebSocket connection manager, message handlers (`_handle_join`, etc.) |
| `portal/booth_state.py` | In-memory `BoothRegistry`, `Booth`, `Participant`, handoff policy, chat history |
| `portal/auth.py` | JWT create/decode, bcrypt password, `require_admin`, `require_user`, `resolve_booth_role`, `can_perform_role` |
| `portal/config.py` | `Settings` — pydantic-settings; all env vars / `.env` |
| `portal/models.py` | SQLAlchemy declarative models: `Event`, `Room`, `DBBooth`, `InviteToken`, `User`, `EventMembership`, `BoothMembership` |
| `portal/database.py` | Async engine, session factory (`get_session`), all CRUD helpers |
| `portal/booth_identity.py` | `make_booth_id`, `make_mediamtx_path`, `parse_booth_id`, `validate_event_slug`, `validate_language_code` |
| `portal/roles.py` | `Permission` enum, `ROLE_PERMISSIONS` dict, `ALL_ROLES` set, `_ROLE_RANK` |
| `portal/crypto.py` | `encrypt_val` / `decrypt_val` (Fernet, SHA-256 key derivation) |
| `portal/transcription/` | Transcription subsystem — see `TRANSCRIPTION_MAP.md` |
| `templates/` | Jinja2 HTML — `base.html`, `interpreter_booth.html`, `listener-event.html`, `admin/` |
| `static/js/interpreter-booth.js` | Booth UI — WebRTC/WHIP, WebSocket, Jitsi iframe, mic controls, level meter |
| `static/js/whep-listener.js` | WHEP playback client — RTCPeerConnection, auto-reconnect with exponential back-off |
| `static/js/admin.js` | Admin panel JS helpers |
| `mediamtx.yml` | MediaMTX config — WHIP/WHEP paths, RTSP, Control API, `overridePublisher` |
| `docker-compose.yml` | portal + mediamtx + jitsi-web/prosody/jicofo/jvb |
| `alembic/versions/` | 8 migrations (001–008); run `uv run alembic upgrade head` |

---

## Core Invariants (Non-Negotiable)

1. **One active publisher per language channel.** MediaMTX enforces `overridePublisher: yes`; Python enforces via `BoothRegistry`.
2. **Interpreter mic audio never routes to `AudioContext.destination`.** No local loopback.
3. **No OBS/RTMP/external encoder.** Browser-only ingest via WHIP.
4. **Jitsi is monitoring only** — receive-only iframe. Not the ingest transport.
5. **No framework.** Frontend is plain ES modules in `static/js/`. No Vue, React, jQuery, inline `<script>` blocks.
6. **No Flask, Socket.IO, aiortc.**
7. **`uv.lock` is the dependency source of truth.** Never modify without running `uv sync --python 3.13 --dev` and confirming tests pass.
8. **`from __future__ import annotations`** at the top of every Python file.
9. **`portal.*` imports for all new Python code.**
10. **Role is never trusted from client data.** WS handler reads `Session.granted_role` (derived from cookies at connect time).
11. **No open redirects.** All redirects use `safe_redirect()` which validates path starts with `/` and has no netloc.

---

## Role Model

```
super_admin > event_owner > room_coordinator > interpreter > listener
```

| Role | Go Live (WHIP) | Set Active | Chat | Manage Booths/Events |
|---|---|---|---|---|
| `super_admin` | ✓ | ✓ | ✓ | ✓ |
| `event_admin` | ✓ | ✓ | ✓ | booths only |
| `coordinator` | ✓ | ✓ | ✓ | — |
| `interpreter` | ✓ (active only) | — | ✓ | — |
| `listener` | — | — | — | — |

Defined in: `portal/roles.py`
Enforced in: `portal/websockets/handlers.py` (`_handle_join`), `portal/routers/api.py` (WHIP URL endpoints), `portal/booth_state.py` (`set_active_interpreter`)

---

## Auth System

| Purpose | Token type | Cookie name | Key claims |
|---|---|---|---|
| Invite-link participant | `create_participant_token()` | `session_token` | `booth_id`, `role`, `event_slug`, `language_code` |
| Registered user | `create_user_token()` | `user_token` | `sub` (user_id), `email`, `is_admin`, `user=True` |
| Admin panel | `create_admin_token()` | `admin_token` | `admin=True` |

All tokens: HS256, `settings.effective_jwt_secret`, expiry = `jwt_expiry_seconds`.
Password hashing: bcrypt (`portal/auth.py` `hash_password` / `verify_password`).
API key storage: Fernet-encrypted in DB (`portal/crypto.py`).

---

## Booth Identity

```python
booth_id  = make_booth_id("pycon2026", "en")   # → "pycon2026-en"
mtx_path  = make_mediamtx_path("pycon2026", "en")  # → "pycon2026/en"
whip_url  = f"{settings.mediamtx_whip_base}/{mtx_path}/whip"
whep_url  = f"{settings.mediamtx_whip_base}/{mtx_path}/whep"
rtsp_url  = f"rtsp://mediamtx:8554/{mtx_path}"   # transcription only
```

Validation: `portal/booth_identity.py` — slug: `^[a-z0-9]+(?:-[a-z0-9]+)*$`; language: ISO 639-1.

---

## WebSocket Protocol (`/ws/booth/{booth_id}`)

| Client → Server | Handler | Trusted fields |
|---|---|---|
| `booth:join` | `_handle_join` | role overridden from `session.granted_role` |
| `booth:leave` | `_handle_leave` | — |
| `booth:chat` | `_handle_chat` | `body` only |
| `booth:set-active` | `_handle_set_active` | `target_id` |
| `booth:update-state` | `_handle_update_state` | `mic_active`, `ingest_connected` |

Server → Client: `booth:joined`, `booth:state`, `booth:chat`, `booth:error`

Caption feed: `/ws/captions/{booth_id}` (no auth) — receives `caption` + `booth:state` messages.

---

## Transcription Pipeline (summary)

```
MediaMTX RTSP (8554) → ffmpeg PCM → TranscriptionProvider → CaptionAggregator → WebSocket broadcast
```

Providers: `local` (faster-whisper, CPU), `openai` (whisper-1/realtime), `deepgram` (nova-2), `nvidia` (Parakeet), `elevenlabs` (scribe_v2).
Worker lifecycle: `start_transcription_worker` / `stop_transcription_worker` in `portal/transcription/worker.py`.
Max concurrent workers: 10 (`MAX_TOTAL_WORKERS`).

---

## Services & Ports

| Service | Port | Protocol |
|---|---|---|
| FastAPI portal | 8000 | HTTP / WS |
| MediaMTX WHIP/WHEP | 8889 | WebRTC HTTP |
| MediaMTX Control API | 9997 | HTTP (path management) |
| MediaMTX RTSP | 8554 | RTSP (transcription) |
| MediaMTX ICE/UDP | 8189 | UDP |
| Jitsi Web | 8443 / 8080 | HTTPS / HTTP |
| JVB | 10000 | UDP |

---

## Validation Before Submitting Changes

```bash
uv sync --python 3.13 --dev
uv run pytest tests/ -v
npm run typecheck
uv run alembic upgrade head   # if DB schema changed
```

Manual browser check:
1. Two interpreter tabs (active + backup) → only one shows active state.
2. Coordinator tab → can reassign active.
3. Mic test + level meter work on active tab.
4. `/listener/{event_slug}` shows correct WHEP stream.
5. Transcription caption overlay appears on listener page when enabled.

---

## Dependency Invariants

- Python runtime: `3.13.x` (enforced in `pyproject.toml`)
- `uv.lock` is the source of truth for all Python dependencies.
- Docker images: `bluenviron/mediamtx:1`, `jitsi/*:stable-9823`

---

## Documentation Requirements

Every PR that adds, removes, or changes a feature **must** update these files in the same commit:

- `README.md` — operational usage and setup
- `docs/how-it-works.mdx` — system design
- `agents.md` (this file) — guardrails, if they changed
- Relevant context file in `.agents/context/` — if the change affects routes, DB schema, or transcription

Do not defer documentation to a follow-up PR.
