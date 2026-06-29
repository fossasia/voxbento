# Plan 014: Add route tests for `demo.py` and `listener.py`

> **Executor instructions**: Follow step by step, verify each, honor STOP
> conditions, update `plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat b4a92d7..HEAD -- portal/routers/demo.py portal/routers/listener.py`

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none (lands cleaner after 012)
- **Category**: tests
- **Planned at**: commit `b4a92d7`, 2026-06-29
- **Issue**: https://github.com/fossasia/voxbento/issues/216

## Why this matters

The two newest route files have no direct tests. `listener.py` join-code
gating and `demo.py` manifest/regenerate are only touched incidentally. Tests
lock in the join-code form/cookie behavior and the manifest status contract so
future changes can't break them silently.

## Current state

- `portal/routers/listener.py:39` `listen_event_page` — shows `listener_join.html`
  without a valid code, sets `listener_code_<slug>` cookie with a valid `?code=`.
- `portal/routers/demo.py:14` `/api/demo/manifest` → `pending`/`generating`/`ready`;
  `/admin/demo/regenerate` admin-only.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | all pass |

## Scope

**In scope:** `tests/test_listener_routes.py` (new), `tests/test_demo_routes.py` (new). **Out of scope:** all `portal/` source.

## Git workflow

- Branch: `advisor/014-route-tests`. Commit: `Add tests for demo and listener routes`. Do NOT push.

## Steps

### Step 1: Listener tests

Use `_client()`, `seed_event`, `admin_cookie`, `setup_db` exactly as in
`tests/test_admin_panel.py`. Cover: 404 missing event; no code → join form
(200, contains form); valid code → 200 with `booths_json` + cookie set; admin
endpoint 403 without cookie.

### Step 2: Demo tests

`/api/demo/manifest` returns a `status` field; `/admin/demo/regenerate` 403
without admin cookie. Do NOT trigger real Supertonic generation — assert on
status contract only.

**Verify**: tests command → all pass.

## Done criteria

- [ ] tests command exits 0; ≥7 new tests pass
- [ ] only new test files changed; `plans/README.md` updated

## STOP conditions

- Manifest test requires the heavy TTS model — assert status only instead.

## Maintenance notes

- After 012, add a concurrent-regenerate test.
