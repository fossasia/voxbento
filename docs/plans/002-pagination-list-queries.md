# Plan 002: Add pagination to unbounded list query helpers

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6972d5b..HEAD -- portal/database.py fastapi_app.py`
> If either file changed since this plan was written, compare the "Current
> state" excerpts below against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: perf
- **Planned at**: commit `6972d5b`, 2026-06-14

## Why this matters

Four `list_*` helpers in `portal/database.py` issue unbounded `SELECT *`
queries with no `LIMIT`. As the database grows, admin and home pages load
every event, room, and user into memory on every request. With 1000+ events
this makes the admin dashboard unusably slow and risks an OOM. Adding
`limit`/`offset` with a safe default (100) caps the worst case immediately
and enables pagination UI later.

## Current state

Relevant files:
- `portal/database.py` — all CRUD helpers; the four unbounded functions are
  `list_events`, `list_rooms_for_event`, `list_booths_for_event`, and
  `list_users`. No LIMIT/OFFSET anywhere in these functions today.
- `fastapi_app.py` — all callers of these functions; currently passes no
  pagination args.
- `tests/test_database.py` — existing database tests; the structural pattern
  to follow.

Current code of the four functions (`portal/database.py`):

```python
# line ~122
async def list_events(session: AsyncSession) -> list[Event]:
    result = await session.execute(select(Event).order_by(Event.created_at))
    return list(result.scalars().all())

# line ~163
async def list_rooms_for_event(session: AsyncSession, event_id: int) -> list[Room]:
    from sqlalchemy.orm import selectinload
    result = await session.execute(
        select(Room).options(selectinload(Room.translation_languages)).where(Room.event_id == event_id).order_by(Room.created_at),
    )
    return list(result.scalars().all())

# line ~215
async def list_booths_for_event(session: AsyncSession, event_id: int) -> list[DBBooth]:
    from sqlalchemy.orm import selectinload
    result = await session.execute(
        select(DBBooth)
        .options(joinedload(DBBooth.event), selectinload(DBBooth.translation_languages))
        .where(DBBooth.event_id == event_id)
        .order_by(DBBooth.language_code),
    )
    return list(result.scalars().all())

# line ~338
async def list_users(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User).order_by(User.created_at))
    return list(result.scalars().all())
```

Repo convention: function signatures use keyword-only args (`*`) for optional
parameters. See `create_booth` at `portal/database.py` lines ~195–210 as an
exemplar. Type hints use `from __future__ import annotations` at the top of
each file (already present).

## Commands you will need

| Purpose   | Command                                                                                     | Expected on success |
|-----------|---------------------------------------------------------------------------------------------|---------------------|
| Tests     | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | 385 passed (or more) |
| Targeted  | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_database.py -q` | all pass |
| Grep check | `grep -n 'def list_events\|def list_rooms_for_event\|def list_booths_for_event\|def list_users' portal/database.py` | shows updated signatures |

## Scope

**In scope**:
- `portal/database.py` — add `limit`/`offset` params to the four functions
- `tests/test_database.py` — add pagination boundary tests (see Test plan)

**Out of scope** (do NOT touch):
- `fastapi_app.py` — all existing callers omit `limit`/`offset`; the default
  of `limit=100, offset=0` makes existing call sites correct without change.
  Only touch this file if you discover a call site that explicitly needs a
  different default — and if you do, treat it as a STOP condition and report.
- `portal/models.py` — no schema changes needed
- `alembic/versions/` — no migration needed
- Any templates

## Git workflow

- Branch: `advisor/002-pagination-list-queries`
- One commit: `perf: add limit/offset pagination to unbounded list queries`

## Steps

### Step 1: Add `limit` and `offset` params to `list_events`

In `portal/database.py`, change `list_events` to:

```python
async def list_events(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Event]:
    result = await session.execute(
        select(Event).order_by(Event.created_at).limit(limit).offset(offset)
    )
    return list(result.scalars().all())
```

**Verify**: `grep -A6 'async def list_events' portal/database.py` shows the new signature with `.limit(limit).offset(offset)`.

### Step 2: Add `limit` and `offset` params to `list_rooms_for_event`

Change `list_rooms_for_event`:

```python
async def list_rooms_for_event(
    session: AsyncSession,
    event_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[Room]:
    from sqlalchemy.orm import selectinload
    result = await session.execute(
        select(Room)
        .options(selectinload(Room.translation_languages))
        .where(Room.event_id == event_id)
        .order_by(Room.created_at)
        .limit(limit)
        .offset(offset),
    )
    return list(result.scalars().all())
```

**Verify**: `grep -A10 'async def list_rooms_for_event' portal/database.py` shows new signature.

### Step 3: Add `limit` and `offset` params to `list_booths_for_event`

Change `list_booths_for_event`:

```python
async def list_booths_for_event(
    session: AsyncSession,
    event_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[DBBooth]:
    from sqlalchemy.orm import selectinload
    result = await session.execute(
        select(DBBooth)
        .options(joinedload(DBBooth.event), selectinload(DBBooth.translation_languages))
        .where(DBBooth.event_id == event_id)
        .order_by(DBBooth.language_code)
        .limit(limit)
        .offset(offset),
    )
    return list(result.scalars().all())
```

**Verify**: `grep -A12 'async def list_booths_for_event' portal/database.py` shows new signature.

### Step 4: Add `limit` and `offset` params to `list_users`

Change `list_users`:

```python
async def list_users(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[User]:
    result = await session.execute(
        select(User).order_by(User.created_at).limit(limit).offset(offset)
    )
    return list(result.scalars().all())
```

**Verify**: `grep -A6 'async def list_users' portal/database.py` shows new signature.

### Step 5: Run the full test suite

```
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q
```

Expected: same pass count (385 or more). All existing call sites use the
default params so no caller should change behaviour.

**Verify**: exit 0, no failures.

## Test plan

Add tests to `tests/test_database.py`. Use the existing async test pattern in
that file — tests use `@pytest.mark.anyio`, create an in-memory DB session
via the test fixtures, and call CRUD helpers directly.

New tests to add (create a section `# --- Pagination tests ---`):

1. **`test_list_events_limit`** — create 5 events, call `list_events(session, limit=2)`, assert `len(result) == 2`.
2. **`test_list_events_offset`** — create 5 events, call `list_events(session, limit=2, offset=3)`, assert `len(result) == 2` and first result is the 4th created event.
3. **`test_list_rooms_pagination`** — create 1 event + 5 rooms, call `list_rooms_for_event(session, event.id, limit=3)`, assert `len(result) == 3`.
4. **`test_list_booths_pagination`** — create 1 event + 1 room + 5 booths (different language codes), call `list_booths_for_event(session, event.id, limit=2)`, assert `len(result) == 2`.
5. **`test_list_users_pagination`** — create 5 users, call `list_users(session, limit=3, offset=1)`, assert `len(result) == 3`.
6. **`test_list_events_default_limit_does_not_break`** — create 3 events, call `list_events(session)` (no args), assert `len(result) == 3` (default limit=100 returns all when count < 100).

Model after `tests/test_database.py` existing async CRUD tests.

```
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_database.py -q
```

Expected: all pass including the 6 new tests.

## Done criteria

- [ ] `grep -c '\.limit(limit)' portal/database.py` → 4 (one per function)
- [ ] `grep -c '\.offset(offset)' portal/database.py` → 4
- [ ] `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` exits 0
- [ ] 6 new pagination tests exist and pass
- [ ] `git diff --name-only HEAD` shows only `portal/database.py` and `tests/test_database.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

- The function bodies in `portal/database.py` don't match the "Current state"
  excerpts (the codebase has drifted since this plan was written).
- Any call site in `fastapi_app.py` passes explicit positional args to these
  functions in a way that breaks with the new signature — report which call
  site and what it passes.
- A verification step fails twice after a reasonable fix attempt.

## Maintenance notes

- The `limit=100` default is intentionally conservative. When the admin UI
  adds pagination controls, the route handlers should pass explicit `limit=`
  and `offset=` values derived from query parameters.
- `list_booths_for_room` (a separate function in `portal/database.py`) also
  has no pagination. It is out of scope for this plan because it is only
  called in single-booth contexts, but it should be updated if rooms grow to
  100+ booths.
- Future reviewer: verify the admin dashboard route at `fastapi_app.py` still
  renders correctly after this change. The default of 100 events is large
  enough for current usage but the UI should eventually support pagination
  controls for larger deployments.
