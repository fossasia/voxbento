---
name: repo-navigation
description: Use this skill to find files, understand module ownership, and locate code in VoxBento.
---

# Skill: Repo Navigation

> Use this skill to find files, understand module ownership, and locate code in VoxBento.

---

## Module Map (Quick Reference)

| What you're looking for | Where to look |
|---|---|
| Route definition for any URL | `portal/routers/` — search for `@router.get`, `@app.post`, `@app.websocket` |
| Auth logic (JWT, cookies, role) | `portal/auth.py` |
| Booth state / participant logic | `portal/booth_state.py` |
| Database models (table schema) | `portal/models.py` |
| Database CRUD helpers | `portal/database.py` |
| Booth ID / MediaMTX path math | `portal/booth_identity.py` |
| Role permissions | `portal/roles.py` |
| App settings / env vars | `portal/config.py` |
| API key encryption / decryption | `portal/crypto.py` |
| Transcription provider logic | `portal/transcription/providers/{provider}.py` |
| Worker lifecycle (start/stop) | `portal/transcription/worker.py` |
| Caption aggregation | `portal/transcription/aggregator.py` |
| Interpreter UI (JS) | `static/js/interpreter-booth.js` |
| Listener WHEP client (JS) | `static/js/whep-listener.js` |
| Admin JS | `static/js/admin.js` |
| HTML templates | `templates/` (base, booth, listener, auth) + `templates/admin/` |
| DB migrations | `alembic/versions/001_*.py` through `008_*.py` |
| MediaMTX configuration | `mediamtx.yml` |
| Docker services | `docker-compose.yml` |
| Tests | `tests/` — see test file map below |

---

## Test File Map

| Test file | What it covers |
|---|---|
| `tests/conftest.py` | anyio fixture, sys.path setup |
| `tests/test_fastapi_app.py` | HTTP routes, auth flows, page renders |
| `tests/test_booth_state.py` | `BoothRegistry` in-memory logic |
| `tests/test_booth_identity.py` | `make_booth_id`, `make_mediamtx_path`, validation |
| `tests/test_database.py` | CRUD helpers with in-memory SQLite |
| `tests/test_database_e2e.py` | End-to-end database flows |
| `tests/test_admin_panel.py` | Admin route auth and operations |
| `tests/test_roles.py` | `Permission` enum, `ROLE_PERMISSIONS` |
| `tests/test_crypto.py` | `encrypt_val` / `decrypt_val` |
| `tests/test_user_auth.py` | Registration, login, user token |
| `tests/test_join_flow.py` | Invite token redemption → JWT cookie |
| `tests/test_memberships_tokens.py` | Membership and token CRUD |
| `tests/test_transcription_concurrency.py` | Worker start/stop, concurrency limits |
| `tests/docker_e2e_test.py` | Full Docker stack smoke tests |
| `tests/verify_persistence.py` | DB persistence checks |

---

## Searching Effectively

### Find all routes
```bash
grep -rn "@router\." portal/routers/
```

### Find where a WS message type is handled
```bash
grep -rn "booth:join\|booth:chat\|booth:set-active" portal/websockets/
```

### Find all settings
```bash
grep -rn "settings\." portal/ | head -40
```

### Find all auth cookie checks
```bash
grep -rn "session_token\|user_token\|admin_token" portal/routers/
```

### Find where a DB model is used
```bash
grep -rn "DBBooth\|InviteToken\|BoothMembership" portal/
```

---

## File Size Guide

| File | ~Lines | Complexity |
|---|---|---|
| `portal/routers/` | Various | High |
| `portal/database.py` | ~400 | Medium |
| `portal/models.py` | ~250 | Medium |
| `portal/booth_state.py` | ~300 | Medium |
| `portal/auth.py` | ~230 | Medium |
| `static/js/interpreter-booth.js` | ~900 | High — use search |
| `portal/transcription/worker.py` | ~130 | Low |
| `portal/transcription/aggregator.py` | ~120 | Low |

---

## Navigation Tips

1. **Routes are split into sub-modules in `portal/routers/`.
2. **Admin routes** all start with `/admin/` and use `dependencies=[Depends(require_admin)]`.
3. **WebSocket handlers** are `_handle_join`, `_handle_leave`, `_handle_chat`, `_handle_set_active`, `_handle_update_state` — all in `portal/websockets/handlers.py`.
4. **The in-memory `booths` variable** is a module-level `BoothRegistry()` instance in `portal/booth_state.py` — the only source of live booth state.
5. **Templates inherit from** `templates/base.html` (user pages) or `templates/admin/base.html` (admin pages).
