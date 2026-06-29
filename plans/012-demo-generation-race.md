# Plan 012: Serialize demo regeneration and track background tasks

> **Executor instructions**: Follow step by step, verify each, honor STOP
> conditions, update `plans/README.md` when done.
>
> **Drift check (run first)**: `git diff --stat b4a92d7..HEAD -- portal/routers/demo.py fastapi_app.py portal/tts/demo_gen.py`

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `b4a92d7`, 2026-06-29
- **Issue**: https://github.com/fossasia/voxbento/issues/214

## Why this matters

`/admin/demo/regenerate` reads a module flag, then sets it, with no lock — two
near-simultaneous admin requests both pass the check and start overlapping
writes to the same WAV files. The background tasks (`_run` in demo.py, `_gen` at
startup) are not stored, so they can be garbage-collected mid-generation, and a
crash inside `generate_demo_assets()` is swallowed so the frontend polls
`status: ready` forever.

## Current state

```python
# portal/routers/demo.py:43
if getattr(dg, "_generating", False):
    return JSONResponse({"ok": False, "detail": "Already generating"})
MANIFEST_PATH.unlink(missing_ok=True)
async def _run() -> None:
    dg._generating = True
    try:
        await generate_demo_assets()
    finally:
        dg._generating = False
asyncio.create_task(_run())   # not stored
```

```python
# fastapi_app.py:44
dg._generating = True
async def _gen():
    try: await ensure_demo_generated()
    finally: dg._generating = False
asyncio.create_task(_gen())   # not stored
```

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | all pass |
| Lint | `uv run ruff check portal/` | All checks passed! |

## Scope

**In scope:** `portal/tts/demo_gen.py`, `portal/routers/demo.py`, `fastapi_app.py`, `tests/` (new file).
**Out of scope:** Supertonic synthesis, manifest schema, frontend JS.

## Git workflow

- Branch: `advisor/012-demo-generation-race`. Commit: `Serialize demo regen and track background tasks`. Do NOT push.

## Steps

### Step 1: Lock + tracked tasks in `demo_gen.py`

Add module-level `_generating = False`, `_generation_lock = asyncio.Lock()`, and
`_tasks: set[asyncio.Task] = set()`. Wrap regenerate check-and-set in
`async with _generation_lock`. Store every spawned task and add a done-callback
that discards it and logs `t.exception()`.

### Step 2: Use them

Route `regenerate_demo` and the lifespan `_gen` task through the lock + task set.
On caught generation failure, set `_generation_error` so manifest can report
`status: "failed"`.

**Verify**: tests command → all pass.

## Test plan

- `tests/test_demo_routes.py`: `/api/demo/manifest` returns `pending` initially;
  two concurrent regenerate calls → second returns `Already generating`. Model
  after `_client()` + `admin_cookie` fixtures in `tests/test_admin_panel.py`.

## Done criteria

- [ ] tests command exits 0, new demo tests pass
- [ ] `uv run ruff check portal/` clean
- [ ] only in-scope files changed; `plans/README.md` updated

## STOP conditions

- Excerpts don't match. Concurrency test can't run without real Supertonic model.

## Maintenance notes

- If demo gen moves to a queue, retire the module-global flag.
