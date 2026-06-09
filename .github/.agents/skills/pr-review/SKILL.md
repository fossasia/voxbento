# Skill: PR Review

> Use this skill to review pull requests for VoxBento.
> Covers correctness, security, architecture compliance, and testing.

---

## PR Review Checklist

### 1. Invariant Compliance
- [ ] No Vue, React, jQuery, inline `<script>` blocks.
- [ ] No Flask, Socket.IO, aiortc.
- [ ] No `AudioContext.destination` for interpreter mic audio.
- [ ] `from __future__ import annotations` at top of every new/modified Python file.
- [ ] New Python code uses `portal.*` imports (not relative imports or new top-level modules).
- [ ] `uv.lock` only changed if `uv sync --python 3.13 --dev` was run.
- [ ] Role is never trusted from client data in WS handlers.

### 2. Auth & Security
- [ ] All redirects use `safe_redirect()` — no raw `RedirectResponse(url=user_input)`.
- [ ] No open redirect: `next_url` / `next` query params validated before use.
- [ ] Admin routes have `dependencies=[Depends(require_admin)]`.
- [ ] API keys stored encrypted via `portal.crypto.encrypt_val`; never stored plaintext.
- [ ] No secrets logged or returned in API responses.
- [ ] JWT tokens use `settings.effective_jwt_secret`; no hardcoded secrets.
- [ ] New form inputs validated before DB write.

### 3. Database Changes
- [ ] Model changes in `portal/models.py` have a corresponding Alembic migration.
- [ ] Migration uses `batch_alter_table` for column operations on SQLite (see migration 008 as reference).
- [ ] New DB columns have appropriate defaults and nullability.
- [ ] Relationships that need `mediamtx_path` use `joinedload(DBBooth.event)`.
- [ ] CRUD functions use `async with get_session() as session:` pattern.

### 4. Route Changes
- [ ] New routes have correct auth dependency.
- [ ] Error cases raise `HTTPException` with appropriate status codes.
- [ ] New page routes redirect to login if unauthenticated (using `safe_redirect`).
- [ ] New API routes respect `_require_access(credentials, token)` if applicable.

### 5. WebSocket Protocol
- [ ] New WS message types have a handler in `fastapi_app.py` `ws_booth` loop.
- [ ] `session.granted_role` used, not `data['role']`.
- [ ] New `Booth.as_public_dict()` fields are intentional (broadcast to all clients).

### 6. Transcription Changes
- [ ] New provider implements `TranscriptionProvider` ABC.
- [ ] New provider added to `PROVIDERS` dict in `worker.py`.
- [ ] New provider registered in `ProviderEnum` and `ALLOWED_MODELS`.
- [ ] New API key column follows Fernet encryption pattern.
- [ ] Worker lifecycle handles `CancelledError` and cleans up ffmpeg process.

### 7. Frontend Changes
- [ ] Plain ES modules — no import maps, no build step, no npm.
- [ ] `node --check static/js/*.js` passes.
- [ ] No `AudioContext.destination` for mic audio.
- [ ] New UI elements have IDs/data attributes expected by JS (not hardcoded strings).
- [ ] WHIP/WHEP URLs constructed from `portal.dataset.*` — not hardcoded.

### 8. Tests
- [ ] New functionality has at least one test.
- [ ] Tests use `anyio` + `pytest.mark.anyio` fixture (see `conftest.py`).
- [ ] DB tests use `configure('sqlite+aiosqlite:///:memory:')` + `init_db()`.
- [ ] No test uses production DB URL.
- [ ] `uv run pytest tests/ -v` passes.

### 9. Documentation
- [ ] `README.md` updated if user-facing behavior changed.
- [ ] `ARCHITECTURE.md` updated if system design changed.
- [ ] Relevant context file in `.github/.agents/context/` updated.
- [ ] `agents.md` updated if invariants changed.

---

## High-Risk Patterns to Flag

| Pattern | Risk | Action |
|---|---|---|
| `RedirectResponse(url=request.query_params['next'])` | Open redirect | Replace with `safe_redirect` |
| `role = data.get('role')` in WS handler | Role injection | Use `session.granted_role` |
| `session.execute(f"... {user_input} ...")` | SQL injection | Use parameterized queries |
| `event.openai_api_key = openai_key` (plaintext) | API key exposure | Use `encrypt_val` |
| `logger.info(f"Key: {api_key}")` | Secret leakage | Remove log line |
| New npm/yarn/vite config | Violates no-build constraint | Remove |
| New `<script>` tag in template | Inline script | Move to ES module file |

---

## Running Validation Locally
```bash
uv sync --python 3.13 --dev
uv run pytest tests/ -v
node --check static/js/interpreter-booth.js
node --check static/js/whep-listener.js
uv run alembic upgrade head   # if migration added
```
