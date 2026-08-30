# Plan 009: Fix N+1 queries in admin and home page route handlers

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6972d5b..HEAD -- fastapi_app.py portal/database.py portal/models.py`
> If any of these changed, compare the "Current state" excerpts below against
> live code before proceeding.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: plans/007-transcription-characterization-tests.md
- **Category**: perf
- **Planned at**: commit `6972d5b`, 2026-06-14

## Why this matters

Three route handlers call `list_booths_for_event` once per event in a loop:
`admin_dashboard` (lines ~1424–1426), the home page (lines ~401–404), and
`admin_event_list` doesn't have this loop but the pattern is pervasive. With
10 events each having 5 booths, the admin dashboard issues 11 DB queries
instead of 2. With 50 events this becomes 51 queries. Adding a
`list_all_booths_for_events` bulk helper eliminates these per-event queries.

## Current state

Relevant files:
- `fastapi_app.py` — `admin_dashboard` (~line 1404), home page handler (~line
  395), and related handlers that loop over events and call
  `list_booths_for_event` per event.
- `portal/database.py` — `list_booths_for_event(session, event_id)` returns
  booths for a single event. No bulk equivalent exists today.
- `portal/models.py` — `DBBooth` has `event_id: Mapped[int]` foreign key.
  `Event.booths` relationship is already defined
  (`relationship(back_populates='booths', cascade='all, delete-orphan')`).

The N+1 loop in `admin_dashboard` (fastapi_app.py, ~lines 1424–1436):

```python
event_data = []
for ev in events:
    db_booths = await list_booths_for_event(session, ev.id)  # 1 query PER event
    booth_statuses = []
    for b in db_booths:
        booth_id = make_booth_id(ev.slug, b.language_code)
        mem_booth = booths.get_booth_sync(booth_id)
        is_live = mem_booth is not None and mem_booth.ingest_status == 'connected'
        booth_statuses.append({'db': b, 'booth_id': booth_id, 'is_live': is_live})
    event_data.append({
        'event': ev,
        'booths': booth_statuses,
        'live_count': sum(1 for bs in booth_statuses if bs['is_live']),
        'total_booths': len(booth_statuses),
    })
```

Home page handler (~lines 401–415) has the same structure.

Existing pattern for bulk loading with selectinload (from `portal/database.py`,
`list_rooms_for_event` ~line 163):

```python
async def list_rooms_for_event(session: AsyncSession, event_id: int, ...) -> list[Room]:
    from sqlalchemy.orm import selectinload
    result = await session.execute(
        select(Room).options(selectinload(Room.translation_languages))
        .where(Room.event_id == event_id).order_by(Room.created_at)
        ...
    )
    return list(result.scalars().all())
```

Repo convention: new CRUD helpers in `portal/database.py` follow the same
`async def` + `AsyncSession` + `select()` pattern, grouped by entity. All
use `from __future__ import annotations` at the file top.

## Commands you will need

| Purpose      | Command                                                                                        | Expected on success |
|--------------|------------------------------------------------------------------------------------------------|---------------------|
| Tests        | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | 385+ passed         |
| Targeted     | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_database.py tests/test_fastapi_app.py -q` | all pass            |
| N+1 check    | `grep -n 'list_booths_for_event' fastapi_app.py`                                               | result shows only single-event contexts |

## Scope

**In scope**:
- `portal/database.py` — add `list_all_booths_for_events` bulk helper
- `fastapi_app.py` — update `admin_dashboard` and home page handler to use
  the bulk helper; remove per-event `list_booths_for_event` calls inside loops
- `tests/test_database.py` — test for the new helper

**Out of scope** (do NOT touch):
- `portal/models.py` — no schema changes
- `alembic/versions/` — no migration needed
- Any route that calls `list_booths_for_event` for a **single** event (detail
  pages, edit pages) — those are correct single-lookups, not N+1
- JS files and templates

## Git workflow

- Branch: `advisor/009-n1-booth-queries`
- Commit 1: `perf(db): add list_all_booths_for_events bulk helper`
- Commit 2: `perf(routes): eliminate N+1 booth queries in admin dashboard and home page`

## Steps

### Step 1: Add `list_all_booths_for_events` to portal/database.py

Place this new function immediately after `list_booths_for_event` in
`portal/database.py`:

```python
async def list_all_booths_for_events(
    session: AsyncSession,
    event_ids: list[int],
) -> dict[int, list[DBBooth]]:
    """Return a mapping of event_id → list[DBBooth] for the given event IDs.

    Executes a single query instead of one-per-event. Returns an empty list
    for any event_id that has no booths.
    """
    if not event_ids:
        return {}
    from sqlalchemy.orm import selectinload
    result = await session.execute(
        select(DBBooth)
        .options(joinedload(DBBooth.event), selectinload(DBBooth.translation_languages))
        .where(DBBooth.event_id.in_(event_ids))
        .order_by(DBBooth.event_id, DBBooth.language_code),
    )
    booths_by_event: dict[int, list[DBBooth]] = {eid: [] for eid in event_ids}
    for booth in result.scalars().all():
        booths_by_event[booth.event_id].append(booth)
    return booths_by_event
```

**Verify**: `grep -n 'list_all_booths_for_events' portal/database.py` → shows the new function.

### Step 2: Update admin_dashboard to use the bulk helper

In `fastapi_app.py`, locate `admin_dashboard`. Change the import block and the
per-event loop.

**Before** (current, ~line 1424):
```python
from portal.database import get_session, list_events, list_booths_for_event, list_memberships_for_user, list_room_memberships_for_user
...
event_data = []
for ev in events:
    db_booths = await list_booths_for_event(session, ev.id)
```

**After**:
```python
from portal.database import get_session, list_events, list_all_booths_for_events, list_memberships_for_user, list_room_memberships_for_user
...
event_ids = [ev.id for ev in events]
booths_by_event = await list_all_booths_for_events(session, event_ids)

event_data = []
for ev in events:
    db_booths = booths_by_event.get(ev.id, [])
```

The rest of the inner loop (building `booth_statuses`, `is_live`, etc.)
stays exactly as-is — only the per-event query call is replaced.

**Verify**: `grep -n 'list_booths_for_event(session, ev.id)' fastapi_app.py` → no match in `admin_dashboard`.

### Step 3: Update the home page handler to use the bulk helper

Locate the home page handler in `fastapi_app.py` (look for `@app.get('/')`
and find the `for ev in events: db_booths = await list_booths_for_event(session, ev.id)`
loop, ~line 401).

Apply the same pattern as Step 2:

```python
event_ids = [ev.id for ev in events]
booths_by_event = await list_all_booths_for_events(session, event_ids)

for ev in events:
    db_booths = booths_by_event.get(ev.id, [])
    ...  # rest of loop unchanged
```

Add `list_all_booths_for_events` to the import from `portal.database` in
this handler if it has a local import block.

**Verify**: `grep -n 'list_booths_for_event(session, ev.id)' fastapi_app.py` → 0 matches (all per-event calls inside loops eliminated).

### Step 4: Verify no other N+1 loops remain

```bash
grep -n 'for.*events.*list_booths_for_event\|list_booths_for_event.*for.*ev' fastapi_app.py
```

If any matches remain, they are additional N+1 sites not covered by this plan.
STOP and report them — do not fix them speculatively.

### Step 5: Run full test suite

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q
```

**Verify**: exits 0, 385+ passed.

## Test plan

Add tests to `tests/test_database.py`. Model after existing async tests there.

New tests:

1. **`test_list_all_booths_for_events_empty_input`**
   - Call `list_all_booths_for_events(session, [])`.
   - Assert result `== {}`.

2. **`test_list_all_booths_for_events_single_event`**
   - Create 1 event + 1 room + 2 booths (language codes 'en', 'fr').
   - Call `list_all_booths_for_events(session, [event.id])`.
   - Assert result is `{event.id: [<en_booth>, <fr_booth>]}`.
   - Assert booths are ordered by language_code (alphabetical: 'en' before 'fr').

3. **`test_list_all_booths_for_events_multiple_events`**
   - Create 2 events, each with 1 room and 2 booths.
   - Call `list_all_booths_for_events(session, [event1.id, event2.id])`.
   - Assert result has both keys and correct booth lists.

4. **`test_list_all_booths_for_events_event_with_no_booths`**
   - Create 2 events: event1 has 2 booths, event2 has 0 booths.
   - Call `list_all_booths_for_events(session, [event1.id, event2.id])`.
   - Assert `result[event2.id] == []`.

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_database.py -k "all_booths_for_events" -v
```

Expected: 4 new tests pass.

## Done criteria

- [ ] `grep -n 'list_all_booths_for_events' portal/database.py` → function defined
- [ ] `grep -n 'list_booths_for_event(session, ev.id)' fastapi_app.py` → 0 matches
- [ ] 4 new database tests pass
- [ ] `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` exits 0
- [ ] `git diff --name-only HEAD` shows only `portal/database.py`, `fastapi_app.py`, `tests/test_database.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

- The loop patterns in `fastapi_app.py` don't match the "Current state"
  excerpts (code has drifted).
- A route other than `admin_dashboard` or the home page also has a per-event
  booth loop that is not accounted for — stop and list all sites instead of
  fixing them silently.
- `list_all_booths_for_events` requires changes to `portal/models.py` to add
  a relationship — it should not; the query is a plain `WHERE event_id IN (...)`.
  If you find you need to change `models.py`, stop and report.
- After the change, a home page or admin page template fails to render because
  it accesses a relationship attribute (e.g. `b.event.slug`) that is not
  eagerly loaded. The `joinedload(DBBooth.event)` in the new helper should
  cover this — but STOP if you get a greenlet/lazy-load error in tests.

## Maintenance notes

- `list_all_booths_for_events` uses `.in_()` which generates a single SQL
  `IN (...)` clause. For very large event lists (>500), consider using
  `selectinload` on the `Event.booths` relationship via `list_events` with
  eager loading instead. At current scale (admin pages default to 100 events
  from plan 002), `IN` is correct.
- If the admin dashboard is ever paginated, pass the paginated `event_ids`
  (the subset for that page) to `list_all_booths_for_events`.
- The N+1 for rooms-within-events (if it exists) is a separate finding not
  covered by this plan.
