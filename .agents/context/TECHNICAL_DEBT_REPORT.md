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

### TD-03: [RESOLVED] In-memory booth state is not persistent
**File:** `portal/booth_state.py` — `BoothRegistry`, `portal/booth_recovery.py`
**Resolution:** Each booth's Go Live switch and active interpreter are saved to `booth_live_state` (migration 042) and restored at startup. The active interpreter comes back as a placeholder only the same session can reclaim within `BOOTH_RESTART_GRACE_SECONDS`. Participants, chat and transcription workers are still in memory only: interpreters rejoin on reconnect, chat history starts empty, and floor/booth transcription must be restarted by its owner.

---

### TD-04: [RESOLVED] Shared `ADMIN_PASSWORD` login
**File:** `portal/config.py` + `portal/routers/auth.py` POST `/login`
**Resolution:** The shared environment-variable fallback was removed. Admin access now uses authenticated registered users and the `is_admin` flag or scoped memberships. See `docs/admin-password-removal-migration.md` for rollout and recovery guidance.

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

### TD-08: Rate limiting on `/register` (login rate limiting resolved)
**File:** `portal/routers/auth.py`
**Status:** Partially addressed. Sliding-window rate limiting is implemented on `/login` (10 attempts per normalized email per hour) and verification email requests. However, `POST /register` does not yet enforce a sliding-window limit on registration attempts. TD-08 remains open until registration rate limiting is implemented to prevent automated registration abuse and email flooding.

---

### TD-09: `alembic/versions/` uses integer revision IDs (001–038)
**File:** `alembic/versions/*.py`
**Problem:** Alembic expects random hex IDs. Using sequential integer IDs (`001`–`038`) works for linear migrations but creates merge conflict risk with parallel feature branches.
**Impact:** Low — functional; requires renumbering if branches land out of order.
**Fix:** Switch to Alembic-generated hex IDs for future migrations; update `alembic.ini` to use `revision_id_length`.

---

### TD-10: Legacy booth URL `/interpreter/{booth_id}` still active
**File:** `portal/routers/interpreter.py` — `GET /interpreter/{booth_id}`
**Problem:** Free-form booth IDs bypass the structured identity scheme. No event scope, no relay booth, no per-room Jitsi URL.
**Impact:** Low — kept for backward compatibility; new features do not support it.
**Fix:** Deprecate and remove once all clients migrate to `/interpreter/{event_slug}/{language_code}`.

---

### TD-11: [RESOLVED] No WebSocket reconnection handling on the server
**File:** `portal/booth_state.py`, `portal/websockets/manager.py`
**Resolution:** A dropped booth socket marks the participant `connected: false` and keeps their place, including the active slot, for `BOOTH_RECONNECT_GRACE_SECONDS`. A reconnect from the same session (`booth_session_key`) with the same `participant_id` reclaims it; any other session gets a fresh ID. A socket that still holds the participant is closed with 4009, which the client treats as final. Revoked access removes the participant at once.

---

## Documentation Gaps

- Legacy `docs/` notes partially reflect pre-database auth designs.
- REST API endpoint OpenAPI documentation can be enriched with detailed schema examples.

---

## Missing Tests

| Gap | Relevant test file |
|---|---|
| WebSocket token scope validation (booth_id mismatch) | `tests/test_fastapi_app.py` |
| `CaptionAggregator` 50-word and 15-second forced finalization | `tests/test_transcription_concurrency.py` |
| Fernet key rotation (`MultiFernet` with multiple keys) | `tests/test_crypto.py` |
| `BoothRegistry.set_active_interpreter` permission enforcement | `tests/test_booth_state.py` |
| Admin panel route auth guard (all admin routes) | `tests/test_admin_panel.py` |
