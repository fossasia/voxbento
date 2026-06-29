# Plan 011: Stop leaking `_RoomTTSPipeline` consumer tasks for dead rooms

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving on. If a
> STOP condition occurs, stop and report. When done, update the status row in
> `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat b4a92d7..HEAD -- portal/tts/worker.py`
> On any change, compare against the excerpts below; mismatch = STOP.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug
- **Planned at**: commit `b4a92d7`, 2026-06-29
- **Issue**: https://github.com/fossasia/voxbento/issues/213

## Why this matters

Each Supertonic room creates a `_RoomTTSPipeline` whose consumer runs an
infinite `while True` and is stored in `_room_pipelines[room_id]`. Nothing ever
removes it, so a long-running process accumulates one orphaned task and queue
per room that is ever configured for Supertonic, even after the room is deleted
or TTS is disabled. This is a slow resource leak in exactly the long-uptime
deployments the product targets.

## Current state

- `portal/tts/worker.py:30` — `_room_pipelines: dict[int, "_RoomTTSPipeline"] = {}`.
- `portal/tts/worker.py:78-100` — pipeline:

```python
class _RoomTTSPipeline:
    def __init__(self, room_id: int):
        ...
        self._consumer = asyncio.create_task(self._consume())
    async def _consume(self) -> None:
        while True:
            cfg, prep = await self.queue.get()
            ...
```

There is no `shutdown()` and no removal from `_room_pipelines`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | all pass |
| Lint | `uv run ruff check portal/` | All checks passed! |

## Scope

**In scope:** `portal/tts/worker.py`, `tests/test_tts_providers.py`.
**Out of scope:** Deepgram streaming path, `_inflight` set, DB models.

## Git workflow

- Branch: `advisor/011-tts-pipeline-cleanup`
- Commit: `Add cleanup for stale TTS room pipelines`. Do NOT push.

## Steps

### Step 1: Add `shutdown()` to `_RoomTTSPipeline`

`async def shutdown(self)`: cancel `self._consumer`, await it suppressing
`CancelledError`. Add `def remove_room_pipeline(room_id)` at module scope that
pops the pipeline and schedules its shutdown.

**Verify**: `uv run ruff check portal/` → All checks passed!

### Step 2: Drop pipelines for disabled/dead rooms

In `_route`, after config load returns `None` for a room that has a pipeline,
call `remove_room_pipeline(room_id)`. Keep behavior for active rooms unchanged.

**Verify**: tests command → all pass.

## Test plan

- New tests: pipeline created then `remove_room_pipeline` cancels consumer
  (`pipeline._consumer.cancelled()` true after await); removing a missing room
  is a no-op. Model after `tests/test_tts_providers.py`.

## Done criteria

- [ ] tests command exits 0, ≥2 new tests pass
- [ ] `uv run ruff check portal/` clean
- [ ] only in-scope files changed
- [ ] `plans/README.md` updated

## STOP conditions

- Excerpts don't match. Cancelling consumer breaks existing TTS tests.

## Maintenance notes

- If room deletion gets an explicit handler, call `remove_room_pipeline(room_id)` there too.
