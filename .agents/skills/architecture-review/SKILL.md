---
name: architecture-review
description: Use this skill to evaluate proposed architecture changes against VoxBento's design principles.
---

# Skill: Architecture Review

> Use this skill to evaluate proposed architecture changes against VoxBento's design principles.

---

## Fundamental Architecture Principles

1. **Browser-first media.** Audio never touches the Python process. Browser → WHIP → MediaMTX → WHEP → listener browser.
2. **FastAPI is coordination only.** Routes, WebSocket signalling, auth, admin. No audio processing.
3. **Jitsi is monitoring only.** Interpreters watch/hear the floor session. It is not the ingest path.
4. **Single publisher per channel.** Enforced at two layers: Python (`BoothRegistry.set_active_interpreter`) and MediaMTX (`overridePublisher: yes`).
5. **In-memory booth state.** `BoothRegistry` is module-level in `portal/booth_state.py`. No external state store yet (see TD-03 in `TECHNICAL_DEBT_REPORT.md`).

---

## Component Responsibilities (strict boundaries)

| Component | Does | Does NOT do |
|---|---|---|
| FastAPI portal | Routes, auth, admin, WS coordination, DB queries | Audio processing, transcoding, media relay |
| MediaMTX | WHIP ingest, WHEP playback, RTSP for ffmpeg | Auth, coordination, UI |
| Jitsi Meet | Floor session monitoring (receive-only iframe) | Audio ingest, interpreter publishing |
| Browser JS | WebRTC/WHIP, WebSocket, Jitsi iframe, mic meter | Server-side logic, DB access |
| ffmpeg (spawned) | PCM extraction from RTSP for transcription | Anything else |
| Transcription providers | Text from PCM audio | Media relay, broadcast, DB write |

---

## Evaluating Proposed Changes

### Does this introduce a new dependency?
- Check if it's already in `pyproject.toml`.
- Ask: can this be done with existing FastAPI/SQLAlchemy/httpx capabilities?
- New Python dependencies → run `uv add {pkg}` (not pip); never edit `uv.lock` manually.

### Does this add server-side audio processing?
- **STOP.** Audio must not flow through the Python process.
- Route audio directly: browser → MediaMTX → listener.
- If transcription is needed: use the existing `portal/transcription/` subsystem.

### Does this add a frontend framework?
- **STOP.** Frontend is plain ES modules. No Vue, React, jQuery, build step.
- See `.github/instructions/js.instructions.md` for JavaScript conventions.

### Does this require a new DB column?
- Add to `portal/models.py` with proper `Mapped` typing.
- Create an Alembic migration in `alembic/versions/`.
- For SQLite compatibility: use `batch_alter_table` in the migration (see migration 008 as reference).

### Does this change the WebSocket protocol?
- The protocol is used by `static/js/interpreter-booth.js` — both files must change together.
- New message types must be handled in `portal/websockets/manager.py` `ws_booth` loop + `_handle_*` function.
- Role enforcement: role is always from `session.granted_role`, never from client `data['role']`.

### Does this change auth?
- Three separate token types exist (`session_token`, `user_token`, `admin_token`). Do not conflate.
- All tokens use HS256 with `settings.effective_jwt_secret`.
- WebSocket auth: cookies are read at connect time and stored in `Session.granted_role`.

---

## Red Flags in Architecture Proposals

| Proposal | Risk |
|---|---|
| "Add a WebSocket message to send audio data" | Violates browser-first principle |
| "Use Redis for real-time booth state" | Valid but requires careful migration of `BoothRegistry` |
| "Add a REST endpoint that returns the JWT secret" | Security violation |
| "Encode the role in the WebSocket join message and trust it" | Violates role trust model |
| "Store all session data in a cookie" | Risk of cookie size limits + replay attacks |
| "Add Vue for the admin panel" | Violates no-framework constraint |
| "Use aiortc for SFU" | Explicitly forbidden in invariants |
| "Proxy WHIP through FastAPI" | Breaks browser-first media architecture |

---

## Good Architecture Patterns in This Codebase

- **`safe_redirect(url)`** — validates redirects to prevent open redirect attacks. Use it for all redirects.
- **`_ensure_mediamtx_path(channel_id)`** — creates alwaysAvailable paths. Call before returning WHIP URL.
- **`asyncio.Lock` in `BoothRegistry`** — all booth mutations serialized. Prevents race conditions.
- **Lazy DB engine init** — `_get_engine()` in `portal/database.py` defers connection until first use.
- **`from __future__ import annotations`** — deferred evaluation prevents circular import issues.
- **Fernet encryption for API keys** — SHA-256 derived key, supports key rotation via `MultiFernet`.
