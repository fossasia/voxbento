# Plan 015: Default `debug` to False and gate SQL echo / verbose behavior

> **Executor instructions**: Follow step by step, verify each, honor STOP
> conditions, update `plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat b4a92d7..HEAD -- portal/config.py portal/database.py`

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `b4a92d7`, 2026-06-29
- **Issue**: https://github.com/fossasia/voxbento/issues/217

## Why this matters

`debug` defaults to `True`, and `create_async_engine(echo=settings.debug)` logs
every SQL statement (and its parameters) by default. A production deploy that
forgets to set `DEBUG=false` ships full query logs — noise plus potential PII —
to stdout. Production-safe defaults belong in code, not deploy checklists.

## Current state

```python
# portal/config.py:19
debug: bool = Field(default=True)
# portal/database.py:55
_engine = create_async_engine(settings.database_url, echo=settings.debug)
```

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | all pass |
| Lint | `uv run ruff check portal/` | All checks passed! |

## Scope

**In scope:** `portal/config.py`, `.env.example` (add `DEBUG=true` for local), `tests/`. **Out of scope:** `database.py` line (it already reads the flag).

## Git workflow

- Branch: `advisor/015-debug-default-false`. Commit: `Default debug to False for safe prod logging`. Do NOT push.

## Steps

### Step 1: Flip default

`debug: bool = Field(default=False)`. Add `DEBUG=true` to `.env.example` so local
dev keeps verbose logging.

### Step 2: Confirm callers

`grep -rn "settings.debug" portal/` — confirm only SQL echo / dev-time logging
read it; no behavior depends on it being on. If a route exposes debug detail,
report instead of changing.

**Verify**: tests command → all pass.

## Test plan

- New: `from portal.config import Settings; assert Settings().debug is False`.

## Done criteria

- [ ] tests command exits 0; ruff clean; new config test passes
- [ ] only in-scope files changed; `plans/README.md` updated

## STOP conditions

- Coordinate with TODO plan 005 (secret_key guard) — both touch `config.py`; land sequentially. A required test depends on `debug=True`.

## Maintenance notes

- Verify CI sets `DEBUG=false`; document in `DEPLOYMENT_GUIDE.md`.
