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
| `**/portal/templates/**/*.html` | [`.github/instructions/jinja.instructions.md`](.github/instructions/jinja.instructions.md) |

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

**Stack:** FastAPI (ASGI/uvicorn) + MediaMTX (WHIP/WHEP/RTSP) + self-hosted Jitsi Meet (stable-11248).
**No** Flask, Socket.IO, aiortc.

---

## Module Ownership

| File / Directory | Owns |
|---|---|
| `fastapi_app.py` | Application lifespan, router aggregation, global exception handlers |
| `portal/routers/` | All HTTP routes (pages, admin, REST API), Jinja2 template rendering |
| `portal/room_setup.py` | Room plan parsing, available AI services, booth/translation-language reconciliation, and room summaries |
| `portal/workspace_routing.py` | `/workspace` handler mapping, legacy organizer redirects, management URL/template namespace helpers |
| `portal/websockets/` | WebSocket connection manager, message handlers (`_handle_join`, etc.) |
| `portal/booth_state.py` | In-memory `BoothRegistry`, `Booth`, `Participant`, handoff policy, chat history |
| `portal/auth.py` | JWT create/decode, bcrypt password, `require_admin`, `require_user`, `resolve_booth_role`, `can_perform_role` |
| `portal/config.py` | `Settings` — pydantic-settings; all env vars / `.env` |
| `portal/models.py` | SQLAlchemy declarative models: `Event`, `Room`, `DBBooth`, `InviteToken`, `User`, `EventMembership`, `RoomMembership`, `BoothMembership`, `HostingRequest`, `AIVocabularyEntry`, etc. |
| `portal/database.py` | Async engine, session factory (`get_session`), all CRUD helpers |
| `portal/booth_publish.py` | Single-use WHIP publish tickets for interpreter booth paths; checked by the MediaMTX auth hook |
| `portal/booth_identity.py` | `make_booth_id`, `make_mediamtx_path`, `parse_booth_id`, `validate_event_slug`, `validate_language_code` |
| `portal/roles.py` | `Permission` enum, `ROLE_PERMISSIONS` dict, `ALL_ROLES` set, `_ROLE_RANK` |
| `portal/crypto.py` | `encrypt_val` / `decrypt_val` (Fernet, SHA-256 key derivation) |
| `portal/transcription/` | Transcription subsystem — see `TRANSCRIPTION_MAP.md` |
| `portal/translations/` | Translation subsystem — LLM workers, contextual prompts (`prompts.py`), glossaries (`vocabulary.py`) |
| `portal/tts/` | Text-to-speech subsystem — direct-push Deepgram Aura and Supertonic ONNX |
| `portal/meeting_source.py` | Managed Jitsi floor agent lifecycle reconciler |
| `portal/program_ingest.py` | Floor authorization and reconciliation for Program Stream Ingest |
| `portal/program_relay.py` | YouTube RTMPS destination management and relay health convergence |
| `portal/outage_log.py` | `OutageLog` — reconcile loops log a repeating failure once per outage, not per tick |
| `portal/user_admin.py` | Administrative access rules, user flags (`can_host`, `is_admin`, `is_active`, `email_verified`) |
| `portal/templates/` | Jinja2 HTML — `base.html`, `interpreter_booth.html`, `listener-event.html`, `admin/` |
| `portal/static/css/tokens.css` | Shared design tokens (`--vb-*` colours, radii, shadows, fonts), linked first on every page; new CSS uses these instead of new hex values |
| `portal/static/js/interpreter-booth.js` | Booth UI — WebRTC/WHIP, WebSocket, Jitsi iframe, mic controls, level meter |
| `portal/static/js/whep-listener.js` | WHEP playback client — RTCPeerConnection, auto-reconnect with exponential back-off |
| `portal/static/js/admin.js` | Admin panel JS helpers |
| `mediamtx.yml` | MediaMTX config — WHIP/WHEP paths, RTSP, Control API, `overridePublisher` |
| `docker-compose.yml` | portal + mediamtx + jitsi-web/prosody/jicofo/jvb |
| `alembic/versions/` | 38 migrations (001–038); run `uv run alembic upgrade head` |

---

## Core Invariants (Non-Negotiable)

1. **One active publisher per language channel.** MediaMTX enforces `overridePublisher: yes`; Python enforces via `BoothRegistry`.
2. **Interpreter mic audio never routes to `AudioContext.destination`.** No local loopback.
3. **Interpreter ingest remains browser-only WHIP.** Explicit exception: room-scoped Program Stream Ingest (#690) accepts authenticated HTTPS WHIP from an external encoder, and its operator-enabled relay mode (#749) accepts room-authenticated RTMPS (never plaintext RTMP) and pushes it to an allow-listed YouTube RTMPS destination from the dedicated `program-relay` service. No YouTube pulling, no unauthenticated or plaintext RTMP, no SRT. Floor ownership is `jitsi_bot` or `program_ingest`; never last-publisher-wins.
4. **Jitsi is monitoring only** — receive-only iframe. Not the ingest transport.
5. **No framework.** Frontend is plain ES modules in `portal/static/js/`. No Vue, React, jQuery, inline `<script>` blocks.
6. **No Flask, Socket.IO, aiortc.**
7. **`uv.lock` is the dependency source of truth.** Never modify without running `uv sync --python 3.13 --dev` and confirming tests pass.
8. **`from __future__ import annotations`** at the top of every Python file.
9. **`portal.*` imports for all new Python code.**
10. **Role is never trusted from client data.** WS handler reads `Session.granted_role` (derived from cookies at connect time).
11. **No open redirects.** All redirects use `safe_redirect()` which validates path starts with `/` and has no netloc.
12. **Management namespaces reflect privilege.** Event owners use `/workspace/*`; room coordinators use the room-scoped `/mission-control/*` surface. `/admin/*` is reserved for Admins, apart from legacy room-scoped coordinator handlers retained for compatibility. Legacy event-owner URLs redirect without changing OAuth routes or token behavior.

---

## Role Model

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

Defined in: `portal/roles.py`
Enforced in: `portal/websockets/handlers.py` (`_handle_join`), `portal/routers/api.py` (WHIP URL endpoints), `portal/booth_state.py` (`set_active_interpreter`)

---

## Auth System

| Purpose | Token type | Cookie name | Key claims |
|---|---|---|---|
| Invite-link participant | `create_participant_token()` | `session_token` | `booth_id`, `role`, `event_slug`, `room_id`, `language_code` |
| Registered user / Admin | `create_user_token()` | `user_token` | `sub` (user_id), `email`, `is_admin`, `user=True` |

All tokens: HS256, `settings.effective_jwt_secret`, expiry = `jwt_expiry_seconds`.
Password hashing: bcrypt (`portal/auth.py` `hash_password` / `verify_password`).
API key storage: Fernet-encrypted in DB (`portal/crypto.py`).

---

## Booth Identity

```python
booth_id  = make_booth_id("pycon2026", 14, "en")   # → "pycon2026-14-en"
mtx_path  = make_mediamtx_path("pycon2026", 14, "en")  # → "pycon2026/14/en"
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
| `booth:request-publish-ticket` | `_handle_request_publish_ticket` | — (participant from the WS session) |

Server → Client: `booth:joined`, `booth:state`, `booth:chat`, `booth:error`, `booth:publish-ticket`

Booth WHIP publishing is authenticated (#783): MediaMTX forwards the `Authorization: Bearer` ticket to
`/internal/media-auth`, which redeems it once (`portal/booth_publish.py`, 30 s TTL, bound to the booth's
server-derived path) and re-checks that the holder is still the active interpreter and that Mission Control has
broadcast unlocked. Anonymous publishes get 401. A `booth:join` naming another socket's `participant_id` gets a fresh ID.

Caption feed: `/ws/captions/{booth_id}` receives `caption`, `translated_caption` + `booth:state` messages. Auth goes through `resolve_ws_auth()`: a `?token=` JWT is always validated and scope-checked (listener tokens to the booth's event, participant tokens to the booth). Without a token the feed is open unless `BOOTH_ACCESS_TOKEN` is set; then it rejects a foreign `Origin` and requires a `session_token` or `user_token` cookie with the same scope checks. Rejections close with 4001 (missing/invalid credentials) or 4003 (scope or origin mismatch). Its `booth:state` carries only `booth_id`, `event_slug`, `room_id`, `language_code`, `broadcast_unlocked` and `ingest_status` (never participants or booth chat), is sent once on connect if the booth exists, and again whenever one of those fields changes.
Direct-push TTS feed: `/ws/tts/{room_id}/{language_code}/{booth_id}` — pre-authorized binary bundle stream (version byte, length-prefixed JSON header, optional 24 kHz PCM audio). Translation text is delivered concurrently over the captions feed and inside TTS bundle headers.

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

## Pull Request Target Branch

- Create feature and fix branches from `dev`, and target all pull requests to `dev`.
- Always specify the base explicitly when creating a PR: `gh pr create --base dev`.
- Verify the PR's base branch is `dev` before reporting it as ready. Do not rely on
  GitHub's default branch or target `main` unless the user explicitly requests it.

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
- Docker images: `bluenviron/mediamtx:1.18.2`, `ghcr.io/jitsi/*:stable-11248`

Meeting source: `portal/meeting_source.py` owns the managed Jitsi agent lifecycle
(desired state `rooms.meeting_agent_enabled`, migration 027, bounded restarts,
restart reconciliation); `portal/routers/meeting_source.py` owns its owner controls.
Never log or return the meeting URL. Read `docs/meeting-source.md` first.

Program ingest: `portal/program_ingest.py` owns floor authorization/reconciliation;
`portal/routers/program_ingest.py` owns private media auth and event-owner controls.
Migration 025 adds protected credentials, source ownership and persistent floor
sequence. One portal replica; program media never traverses Python. Read
`docs/program-stream-ingest.md` before changing this boundary or deploying it.

Program relay (#749): `portal/program_relay.py` owns the YouTube destination
(Fernet-encrypted, write-only), relay health and convergence of the `program-relay`
service (`program-relay/relay.py`, the media plane; `docker-compose.relay.yml`);
migration 028 adds `program_ingest_mode`, which binds the program key to WHIP or RTMPS.
Never log, return or forward FFmpeg text containing the destination. Read
`docs/program-relay.md` before changing relay mode. Relay deployments select the override
with `COMPOSE_FILE` in `.env` (`scripts/setup-youtube-relay.sh`); the deploy workflow must
never remove a running `program-relay`.

---

## Documentation Requirements

Keep documentation scoped to the change. Routine feature PRs, bug fixes, and
refactors **must not update `README.md` by default**. The README is a stable
project overview and quick start, not a feature changelog or a home for detailed
UI walkthroughs, provider options, implementation notes, or rollout instructions.

Update `README.md` only when explicitly requested, to correct an existing factual
error or broken link, or when a change makes its installation or quick-start
instructions inaccurate. Keep those edits minimal; document feature details in
the relevant `website/docs/` page or focused `docs/` guide instead.

Update documentation in the same PR when the change affects what it describes:

- Relevant `website/docs/` pages or `docs/` guides — changed usage, configuration,
  or operational procedures.
- `website/docs/how-it-works.mdx` — changed system design only.
- `agents.md` — changed agent guardrails or invariants only.
- Relevant `.agents/context/` files — changed routes, DB schema, transcription,
  or module ownership.

Do not add unrelated documentation edits just to satisfy a checklist, and do not
append feature summaries to the README or agent guide for every PR.

Password sign-in requires `User.email_verified`; everyone, admins included, signs in
at `/login` (`login` rate-limit bucket, 10 attempts per normalized email per hour).
`/admin/login` only redirects to `/login?next=/admin/`.

Registration protection lives in `portal/registration_protection.py`. Throttle
by the ASGI-resolved client IP, never raw forwarding headers. When
`REGISTRATION_TURNSTILE_ENABLED` is true, validate Cloudflare success, hostname
and `register` action before signup side effects; service failures must reject
the signup. Turnstile defaults off for compatible rollout. See
`docs/registration-protection.md` for configuration and proxy trust.

Production deployments validate a separate candidate checkout before advancing
the live checkout. Portal source and dependencies must come from the built image,
with persistent data volumes retained under the `voxbento` Compose project.
