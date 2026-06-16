# VoxBento — Technical Debt Report

> Derived from live implementation analysis. Focused on actionable issues, not speculative concerns.

---

## Critical Issues

### TD-01: [RESOLVED] `fastapi_app.py` is a monolith
**Status:** Fixed in recent refactor. Routes were modularized into `portal/routers/` and WebSockets into `portal/websockets/`.

---

### TD-02: No CSRF protection on admin form routes
**File:** `portal/routers/admin/` — all `POST /admin/*` routes
**Problem:** Admin form endpoints use `samesite='lax'` cookies, which provides partial CSRF protection for top-level navigations but not for cross-origin form POSTs in all browser configurations.
**Impact:** Low in typical deployment (admin is behind auth + known IP), but worth addressing for production hardening.
**Fix:** Add double-submit cookie or token form field on all admin POST forms.

---

### TD-03: In-memory booth state is not persistent
**File:** `portal/booth_state.py` — `BoothRegistry`
**Problem:** All booth state (active interpreter, participants, chat) lives in process memory. A portal restart loses all active sessions.
**Impact:** High during live events — active interpreters are silently dropped.
**Fix:** Either use Redis for booth state, or persist essential state to DB and recover on startup.

---

### TD-04: `ADMIN_PASSWORD` is a single shared secret
**File:** `portal/config.py` + `portal/routers/auth.py` POST `/admin/login`
**Problem:** Single plaintext password. No rate limiting, no lockout, no per-user admin accounts (bypassed only if a registered user has `is_admin=True`).
**Impact:** Medium — brute-force risk if exposed to the internet.
**Fix:** Enforce proper per-user admin auth; remove shared password login.

---

### TD-05: `_created_paths` is a process-global cache that is never invalidated
**File:** `portal/routers/api.py` — `_created_paths: set[str]`
**Problem:** When MediaMTX restarts and loses its in-memory path config, the portal's cache prevents re-creation of `alwaysAvailable` paths. There is partial mitigation (PATCH fallback in `_ensure_mediamtx_path`), but if MediaMTX restarts between cache entry and PATCH, paths may be stale.
**Impact:** Low — only affects edge case; manual portal restart resolves it.
**Fix:** Invalidate `_created_paths` on MediaMTX reachability failure, or check before every WHIP URL request.

---

### TD-06: Transcription worker MAX_TOTAL_WORKERS is hardcoded
**File:** `portal/transcription/worker.py` — `MAX_TOTAL_WORKERS = 10`
**Problem:** Not configurable via settings. Large events with many language channels may hit the limit.
**Impact:** Low for typical deployments (≤10 booths).
**Fix:** Add `max_transcription_workers: int` to `Settings` in `portal/config.py`.

---

### TD-07: `faster-whisper` model loading is not thread-safe under concurrent first-load
**File:** `portal/transcription/providers/local.py` — `get_model`
**Problem:** `_model_lock` is a `threading.Lock` but `get_model` is called from `asyncio.to_thread`. If two booths start with the same model size simultaneously, only the first call loads the model but there may be a brief window between check and creation.
**Impact:** Very low — `threading.Lock` protects the dict mutation; the worst case is a double load attempt that gets blocked.
**Fix:** The existing lock is adequate; this is a documentation gap more than a real bug.

---

### TD-08: No rate limiting on `/register` and `/login`
**File:** `portal/routers/public.py` + `portal/routers/auth.py`
**Problem:** No brute-force protection on password endpoints.
**Impact:** Medium — password enumeration and brute-force risk.
**Fix:** Add `slowapi` or a simple in-memory rate limiter keyed on client IP.

---

### TD-09: `alembic/versions/` uses integer revision IDs (001–008)
**File:** `alembic/versions/*.py`
**Problem:** Alembic expects random hex IDs. Using `001`–`008` works but creates ordering confusion if revisions are created out-of-order by different developers.
**Impact:** Low — functional for single-developer usage; becomes a problem with parallel branches.
**Fix:** Switch to Alembic-generated hex IDs for future migrations; update `alembic.ini` to use `revision_id_length`.

---

### TD-10: Legacy booth URL `/interpreter/{booth_id}` still active
**File:** `portal/routers/interpreter.py` — `GET /interpreter/{booth_id}`
**Problem:** Free-form booth IDs bypass the structured identity scheme. No event scope, no relay booth, no per-room Jitsi URL.
**Impact:** Low — kept for backward compatibility; new features do not support it.
**Fix:** Deprecate and remove once all clients migrate to `/interpreter/{event_slug}/{language_code}`.

---

### TD-11: No WebSocket reconnection handling on the server
**File:** `portal/websockets/manager.py` — `ws_booth` endpoint
**Problem:** If a participant reconnects after a network drop, they get a new `participant_id` and appear as a new participant. The old participant remains until server-side cleanup.
**Impact:** Medium — room_coordinator sees ghost participants; handoff state may be incorrect.
**Fix:** Allow reconnect with an existing `participant_id` (with token re-validation); clean up stale participant records after a timeout.

---

## Documentation Gaps

- `docs/` directory is partially outdated (pre-database auth design).
- `ARCHITECTURE.md` needs update to reflect 8-migration DB state and transcription subsystem.
- No OpenAPI documentation for REST API endpoints (FastAPI generates `/docs` automatically but no custom descriptions on most routes).

---

## Missing Tests

| Gap | Relevant test file |
|---|---|
| WebSocket token scope validation (booth_id mismatch) | `tests/test_fastapi_app.py` |
| `CaptionAggregator` 50-word and 15-second forced finalization | `tests/test_transcription_concurrency.py` |
| Fernet key rotation (`MultiFernet` with multiple keys) | `tests/test_crypto.py` |
| `BoothRegistry.set_active_interpreter` permission enforcement | `tests/test_booth_state.py` |
| Admin panel route auth guard (all admin routes) | `tests/test_admin_panel.py` |
