# VoxBento — AI Agent Workflows

> Step-by-step playbooks for common coding agent tasks.
> Each workflow references the exact files to read/edit and the validation commands to run.

---

## Prerequisites (Read First)

Before starting any task, read:
1. [REPOSITORY_CONTEXT.md](REPOSITORY_CONTEXT.md) — architecture, stack, invariants
2. [CHANGE_IMPACT_MAP.md](CHANGE_IMPACT_MAP.md) — which files are affected

---

## W1: Feature Development

### Step 1 — Understand the target area
- Backend route? → Read `fastapi_app.py` around similar routes, [ROUTE_MAP.md](ROUTE_MAP.md)
- Database change? → Read `portal/models.py`, `portal/database.py`, [DATABASE_MAP.md](DATABASE_MAP.md)
- Transcription? → Read `portal/transcription/`, [TRANSCRIPTION_MAP.md](TRANSCRIPTION_MAP.md)
- Frontend UI? → Read `static/js/interpreter-booth.js` or `whep-listener.js`; see `.github/instructions/js.instructions.md`
- Python? → See `.github/instructions/python.instructions.md`

### Step 2 — Check invariants
- No Vue/React/jQuery/inline scripts.
- No Flask/Socket.IO/aiortc.
- `from __future__ import annotations` at top of every Python file.
- Interpreter mic audio never routes to `AudioContext.destination`.
- One active publisher per channel.

### Step 3 — Implement
- Minimal, local edits only — do not refactor adjacent code.
- New Python: `portal.*` imports.
- New DB column: create Alembic migration in `alembic/versions/`.
- New route: add to `fastapi_app.py` with correct auth dependency (`Depends(require_admin)`, `require_user`, or cookie check).

### Step 4 — Validate
```bash
uv run pytest tests/ -v
node --check static/js/interpreter-booth.js
node --check static/js/whep-listener.js
uv run alembic upgrade head   # if DB changes
```

---

## W2: Bug Fixing

### Step 1 — Reproduce
- Read the failing test in `tests/` if one exists.
- If HTTP bug: check `fastapi_app.py` route + relevant template.
- If WebSocket bug: check `fastapi_app.py` WS handler + `portal/booth_state.py` + `static/js/interpreter-booth.js`.
- If DB bug: check `portal/models.py` + `portal/database.py`.
- If auth bug: check `portal/auth.py` + cookie handling in `fastapi_app.py`.

### Step 2 — Isolate
- Check [CHANGE_IMPACT_MAP.md](CHANGE_IMPACT_MAP.md) for files that own the buggy behavior.
- Check `tests/` for existing coverage.

### Step 3 — Fix
- Make the minimal change.
- If the bug is in `portal/booth_state.py`, note it uses `asyncio.Lock` — check for lock misuse.
- If the bug is in JWT validation, check both `session_token` and `user_token` paths in `portal/auth.py`.

### Step 4 — Validate
```bash
uv run pytest tests/ -v
```

---

## W3: Adding a Transcription Provider

1. Read `portal/transcription/constants.py` — understand `ProviderEnum` and `ALLOWED_MODELS`.
2. Read `portal/transcription/providers/base.py` — understand `TranscriptionProvider`, `ProviderConfig`, `run_stream`.
3. Create `portal/transcription/providers/{name}.py` — implement `TranscriptionProvider`.
4. Add `ProviderEnum.{NAME} = "{name}"` to `constants.py`.
5. Add `{name}: [models...]` to `ALLOWED_MODELS`.
6. Add to `PROVIDERS` dict in `worker.py`.
7. If provider needs API key: add encrypted column to `Event` in `portal/models.py`.
8. Create Alembic migration.
9. Update `get_api_key` in `providers/base.py` key_map.
10. Update `admin_event_api_settings_post` in `fastapi_app.py`.
11. Update `templates/admin/api_settings.html`.
12. Run: `uv run pytest tests/test_transcription_concurrency.py -v`

---

## W4: PR Review

1. Read `.github/instructions/python.instructions.md` and `.github/instructions/js.instructions.md`.
2. Check all invariants from [REPOSITORY_CONTEXT.md](REPOSITORY_CONTEXT.md) — **Key Invariants** section.
3. If DB changes: verify Alembic migration exists and is correct.
4. If new route: verify auth dependency is correct; check for open redirect risk in `safe_redirect`.
5. If new JS: verify no jQuery, no inline scripts, no `AudioContext.destination` usage.
6. Check OWASP top 10 for the changed code area.
7. Run tests + linting:
```bash
uv run pytest tests/ -v
node --check static/js/*.js
```

---

## W5: Incident Response

1. Check `/healthz` endpoint — returns `{ok, mediamtx_ok}`.
2. If `mediamtx_ok: false` — MediaMTX is down or unreachable. Check `docker-compose ps mediamtx`.
3. If interpreters can't go live — check `_ensure_mediamtx_path` in `fastapi_app.py` and MediaMTX Control API (port 9997).
4. If WebSocket disconnects — check `portal/booth_state.py` `asyncio.Lock` usage; check server logs for `WebSocketDisconnect`.
5. If transcription failing — check `portal/transcription/worker.py` `active_workers` state; check ffmpeg availability in container.
6. If login fails — check `portal/auth.py` `verify_password`; check `users.is_active` in DB.
7. See `.github/.agents/skills/incident-investigation/SKILL.md` for detailed checklist.

---

## W6: Database Migrations

1. Make model changes in `portal/models.py`.
2. Generate migration:
```bash
uv run alembic revision --autogenerate -m "description"
```
3. Review generated file in `alembic/versions/` — autogenerate is not always correct for SQLite batch mode.
4. For SQLite, `batch_alter_table` is required for column changes (see migration 008 as example).
5. Apply:
```bash
uv run alembic upgrade head
```
6. Verify:
```bash
uv run pytest tests/test_database.py -v
```

---

## W7: Security Audit

1. Read `portal/auth.py` — check token validation paths.
2. Check all `safe_redirect` calls — verifies path starts with `/` and has no netloc (prevents open redirect).
3. Check all form handlers for CSRF exposure (admin panel uses session cookie + same-site=lax).
4. Check `portal/crypto.py` — `API_KEY_ENCRYPTION_KEY` must be set and ≥32 chars.
5. Check `fastapi_app.py` WebSocket handler — role is never taken from client data.
6. Check `_require_access` — optional token guard (disabled if `booth_access_token` is empty).
7. See `.github/.agents/skills/security-audit/SKILL.md` for full checklist.

---

## W8: Test Generation

1. Check existing test structure in `tests/` (see `conftest.py` — anyio fixture, `configure()` override).
2. DB tests: call `portal.database.configure('sqlite+aiosqlite:///:memory:')` in fixture; call `init_db()`.
3. Route tests: use `httpx.AsyncClient(app=app, base_url='http://test')`.
4. WebSocket tests: use `httpx.AsyncClient(app=app)` with `ws_connect`.
5. Booth state tests: instantiate `BoothRegistry()` directly.
6. Run: `uv run pytest tests/ -v --tb=short`
