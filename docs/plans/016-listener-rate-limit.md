# Plan 016: Rate-limit listener join-code validation

> **Executor instructions**: Follow step by step, verify each, honor STOP
> conditions, update `plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat b4a92d7..HEAD -- portal/routers/listener.py`

## Status

- **Priority**: P3
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `b4a92d7`, 2026-06-29
- **Issue**: https://github.com/fossasia/voxbento/issues/218

## Why this matters

`/listener/{event_slug}` validates a 6-char join code on every load with no
backoff. The code space (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, 6 chars ≈ 1B) is
large but not unguessable over time, and an attacker faces zero friction. A
simple per-IP throttle removes online brute-force without changing the UX.

## Current state

```python
# portal/routers/listener.py:27
def has_listener_access(request, event_slug, listener_join_code, code) -> bool:
    payload = get_booth_session(request)
    if payload and payload.get("user"): return True
    cookie_code = request.cookies.get(f"listener_code_{event_slug}")
    active_code = code or cookie_code
    return bool(listener_join_code and active_code == listener_join_code)
# code generated: admin.py:288 — 6 chars from a 32-symbol alphabet
```

No `slowapi`/rate-limit dependency present; implement in-process.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | all pass |
| Lint | `uv run ruff check portal/` | All checks passed! |

## Scope

**In scope:** `portal/routers/listener.py`, `tests/`. **Out of scope:** join-code generation, new deps, admin routes.

## Git workflow

- Branch: `advisor/016-listener-rate-limit`. Commit: `Throttle failed listener join-code attempts`. Do NOT push.

## Steps

### Step 1: In-process throttle

Add a module-level `{ip: (count, window_start)}`. On a failed code check, count
attempts per IP; after N (e.g. 10) in 60s, return 429 before rendering. Reset on
success. Successful cookie holders bypass.

### Step 2: Wire into both listener routes

Apply before re-rendering the join form. Use `request.client.host`.

**Verify**: tests command → all pass.

## Test plan

- New: 11 bad codes from one client → 429; valid code → 200; cookie holder unthrottled. Model after `tests/test_admin_panel.py`.

## Done criteria

- [ ] tests command exits 0; ruff clean; new tests pass
- [ ] only in-scope files changed; `plans/README.md` updated

## STOP conditions

- App runs multi-worker (in-proc counter insufficient) → report, recommend shared store. Behind a proxy, `request.client.host` is the proxy → require trusted `X-Forwarded-For` handling first.

## Maintenance notes

- Multi-worker prod needs Redis-backed limit; flag in review.
