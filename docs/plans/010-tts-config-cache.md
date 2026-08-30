# Plan 010: Cache per-room TTS config so caption segments stop re-querying the DB and decrypting keys

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat b4a92d7..HEAD -- portal/tts/worker.py`
> If `portal/tts/worker.py` changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: perf
- **Planned at**: commit `b4a92d7`, 2026-06-29
- **Issue**: https://github.com/fossasia/voxbento/issues/212

## Why this matters

Every finalized caption segment for a floor-TTS room triggers a fresh DB round
trip plus a Fernet decrypt of the translation/Deepgram API keys. During live
events captions arrive many times per minute per room, so the config — which
almost never changes mid-session — is reloaded and re-decrypted continuously.
Caching it per room removes the steady-state DB and crypto load while keeping a
short TTL so rotated keys/settings still take effect.

## Current state

- `portal/tts/worker.py` — TTS routing. `enqueue_tts()` (line 35) spawns a task
  per segment that calls `_route()` (line 59), which builds a `TTSWorker` and
  calls `await worker._load_config(room_id)` every time.
- `_load_config` (line 133) opens a session, runs a `selectinload` over `Room`,
  loads the `Event`, and decrypts keys via `decrypt_val`. Excerpt:

```python
# portal/tts/worker.py:59
async def _route(room_id: int, text: str) -> None:
    worker = TTSWorker(tts_manager.broadcast_audio)
    cfg = await worker._load_config(room_id)
    if not cfg:
        return
```

```python
# portal/tts/worker.py:133
async def _load_config(self, room_id: int) -> dict | None:
    async with get_session() as session:
        room = await session.scalar(select(Room).options(...).where(Room.id == room_id))
        ...
        dg_api_key = decrypt_val(event.encrypted_deepgram_api_key) ...
```

Convention: module-level mutable singletons are used in this file already
(`_room_pipelines`, `_inflight` at lines 30/33). `from __future__ import
annotations` is at the top of every Python file. Match both.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | all pass (≥439) |
| Lint | `uv run ruff check portal/` | All checks passed! |

## Scope

**In scope:**
- `portal/tts/worker.py`
- `tests/test_tts_providers.py` (add cache tests)

**Out of scope:**
- `portal/translations/worker.py` — separate config path; do not touch.
- The `_load_config` query shape — keep it; only add caching around it.

## Git workflow

- Branch: `advisor/010-tts-config-cache`
- Commit message style (match repo): short imperative, e.g. `Cache TTS room config to cut per-segment DB load`.
- Do NOT push or open a PR.

## Steps

### Step 1: Add a small TTL cache for config

Add a module-level cache `{room_id: (expires_at, cfg)}` with a TTL of 300s.
Wrap `_load_config` with `_load_config_cached(room_id)` that returns the cached
dict if not expired, else calls `_load_config` and stores it. A `None` result
(TTS disabled) should also be cached for the TTL to avoid hammering on disabled
rooms. Add `def invalidate_room_config(room_id: int)` that pops the entry.

**Verify**: `uv run ruff check portal/` → All checks passed!

### Step 2: Route through the cache

Change `_route` and `_RoomTTSPipeline.submit` paths to call the cached loader.
Keep the per-segment translate/synthesize behavior identical.

**Verify**: tests command → all pass.

## Test plan

- Add `tests/test_tts_providers.py` cases: cache hit returns same dict without a
  second DB call (patch `_load_config` with a counter), TTL expiry forces reload,
  `invalidate_room_config` clears entry. Model after the class structure in
  `tests/test_tts_providers.py`.
- Verify: tests command → all pass, including 3 new tests.

## Done criteria

- [ ] tests command exits 0, ≥3 new cache tests pass
- [ ] `uv run ruff check portal/` → All checks passed!
- [ ] `git status` shows only in-scope files
- [ ] `plans/README.md` row updated

## STOP conditions

- `portal/tts/worker.py` differs from the excerpts above.
- Caching breaks ordering for Supertonic rooms (a test regresses).
- Adding the cache requires touching `translations/worker.py`.

## Maintenance notes

- When room TTS settings or keys are edited in `portal/routers/admin.py`, call
  `invalidate_room_config(room_id)` so changes apply before TTL expiry. Note this
  in PR review.
