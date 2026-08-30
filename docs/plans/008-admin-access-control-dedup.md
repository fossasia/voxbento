# Plan 008: Extract duplicated admin access-control logic into a shared helper

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6972d5b..HEAD -- fastapi_app.py portal/auth.py portal/database.py`
> If any of these changed, compare the "Current state" excerpts below against
> live code before proceeding.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: plans/007-transcription-characterization-tests.md
- **Category**: tech-debt
- **Planned at**: commit `6972d5b`, 2026-06-14

## Why this matters

The pattern "decode both admin_token and user_token cookies → determine
is_super_admin → fetch memberships → compute accessible_event_ids" is
copy-pasted across at least 4 route handlers in `fastapi_app.py`:

- `mission_control_list` (lines ~1248–1282)
- `mission_control_grid` (lines ~1296–1332)
- `admin_dashboard` (lines ~1414–1420)
- `admin_event_list` (lines ~1456–1472)

A bug fixed in one location will not propagate to the others. Extracting this
into a shared `portal/auth.py` helper unifies the logic and makes the access
control surface easier to audit and test.

## Current state

Relevant files:
- `fastapi_app.py` — all four routes listed above; duplicate pattern described below.
- `portal/auth.py` — already has `get_admin_flags`, `get_current_user`,
  `require_admin`, `require_user`, `resolve_booth_role`. The new helper
  belongs here.
- `portal/database.py` — `list_memberships_for_user`, `list_room_memberships_for_user`.

The duplicate pattern (mission_control_list, ~lines 1248–1282):

```python
is_super_admin = False
admin_cookie = request.cookies.get('admin_token', '')
if admin_cookie:
    try:
        payload = decode_token(admin_cookie)
        if payload.get('admin'):
            is_super_admin = True
    except jwt.InvalidTokenError:
        pass

user_cookie = request.cookies.get('user_token', '')
if user_cookie:
    try:
        payload = decode_token(user_cookie)
        if payload.get('is_admin'):
            is_super_admin = True
    except jwt.InvalidTokenError:
        pass

async with get_session() as session:
    all_events = await list_events(session)
    if is_super_admin:
        accessible_events = all_events
    else:
        memberships = await list_memberships_for_user(session, int(user['sub']))
        room_memberships = await list_room_memberships_for_user(session, int(user['sub']))
        accessible_event_ids = {m.event_id for m in memberships if m.role == 'event_owner'}
        accessible_event_ids.update({rm.room.event_id for rm in room_memberships if rm.role == 'room_coordinator'})
        accessible_events = [e for e in all_events if e.id in accessible_event_ids]
```

The same block (sometimes with minor variation) appears in all 4 routes.

Repo convention: `portal/auth.py` uses `from __future__ import annotations`
at the top. All public functions in `portal/auth.py` are async and accept a
`Request` parameter. Existing example pattern:

```python
async def get_admin_flags(request: Request) -> dict:
    """Returns is_super_admin / is_event_admin flags from JWT cookies."""
    ...
```

## Commands you will need

| Purpose   | Command                                                                                        | Expected on success |
|-----------|------------------------------------------------------------------------------------------------|---------------------|
| Tests     | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | 385+ passed         |
| Targeted  | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_fastapi_app.py tests/test_user_auth.py -q` | all pass            |
| Grep      | `grep -n 'list_memberships_for_user' fastapi_app.py \| wc -l`                                 | fewer than before   |

## Scope

**In scope**:
- `portal/auth.py` — add `get_accessible_events` helper function
- `fastapi_app.py` — replace the 4 duplicate blocks with calls to the new helper
- `tests/test_user_auth.py` or a new `tests/test_access_control.py` — tests
  for the new helper

**Out of scope** (do NOT touch):
- `portal/database.py` — no changes to CRUD helpers
- `portal/models.py`
- `portal/roles.py`
- Templates
- JS files
- Any route that is NOT one of the 4 named above (admin_dashboard,
  admin_event_list, mission_control_list, mission_control_grid)

## Git workflow

- Branch: `advisor/008-admin-access-control-dedup`
- Commit 1: `refactor(auth): add get_accessible_events helper`
- Commit 2: `refactor(routes): use get_accessible_events in 4 admin routes`

## Steps

### Step 1: Add `get_accessible_events` to portal/auth.py

After the `get_current_user` function in `portal/auth.py`, add:

```python
async def get_accessible_events(
    request: Request,
    session: AsyncSession,
    *,
    user_id: int | None,
) -> tuple[bool, list]:
    """Return (is_super_admin, accessible_events_list) for the current request.

    Checks admin_token and user_token cookies to determine super-admin status.
    For non-super-admins with a user_id, returns only events the user owns
    (as event_owner) or coordinates (as room_coordinator).
    Returns all events for super-admins.

    Args:
        request: The FastAPI Request (used to read cookies).
        session: An open AsyncSession — the caller is responsible for the
            session lifecycle.
        user_id: The authenticated user's ID, or None for anonymous callers.
    """
    from portal.database import list_events, list_memberships_for_user, list_room_memberships_for_user
    import jwt as pyjwt

    is_super_admin = False

    admin_cookie = request.cookies.get('admin_token', '')
    if admin_cookie:
        try:
            payload = decode_token(admin_cookie)
            if payload.get('admin'):
                is_super_admin = True
        except pyjwt.InvalidTokenError:
            pass

    user_cookie = request.cookies.get('user_token', '')
    if user_cookie:
        try:
            payload = decode_token(user_cookie)
            if payload.get('is_admin'):
                is_super_admin = True
        except pyjwt.InvalidTokenError:
            pass

    all_events = await list_events(session)

    if is_super_admin or user_id is None:
        return is_super_admin, all_events

    memberships = await list_memberships_for_user(session, user_id)
    room_memberships = await list_room_memberships_for_user(session, user_id)

    accessible_event_ids = {m.event_id for m in memberships if m.role == 'event_owner'}
    accessible_event_ids.update(
        {rm.room.event_id for rm in room_memberships if rm.role == 'room_coordinator'}
    )

    accessible_events = [e for e in all_events if e.id in accessible_event_ids]
    return is_super_admin, accessible_events
```

Also add `AsyncSession` to the imports from sqlalchemy at the top of
`portal/auth.py` if not already present. Check: `grep 'AsyncSession'
portal/auth.py`.

**Verify**: `grep -n 'get_accessible_events' portal/auth.py` → shows the new function.

### Step 2: Update `mission_control_list` to use the helper

Replace the duplicate block in `mission_control_list` (fastapi_app.py, ~lines
1248–1285). The route currently:
1. Decodes cookies to determine `is_super_admin`
2. Calls `list_events` + `list_memberships_for_user` + `list_room_memberships_for_user`
3. Filters to `accessible_events`

Replace with:

```python
@app.get('/mission-control/')
async def mission_control_list(request: Request, user=Depends(require_user)):
    from portal.database import get_session
    from portal.auth import get_accessible_events

    async with get_session() as session:
        is_super_admin, accessible_events = await get_accessible_events(
            request, session, user_id=int(user['sub'])
        )

    return templates.TemplateResponse(
        request,
        'mission_control/event_list.html',
        {
            'events': accessible_events,
            'is_super_admin': is_super_admin,
            'active_nav': 'mission-control',
        }
    )
```

**Verify**: `grep -c 'list_memberships_for_user' fastapi_app.py` decreases by at least 1 after this step.

### Step 3: Update `admin_event_list` to use the helper

Replace the duplicate block in `admin_event_list` (~lines 1456–1475) with:

```python
@app.get('/admin/events/', dependencies=[Depends(require_admin)])
async def admin_event_list(request: Request):
    from portal.database import get_session
    from portal.auth import get_admin_flags, get_current_user, get_accessible_events

    admin_flags = await get_admin_flags(request)
    user = await get_current_user(request)
    user_id = int(user['sub']) if user and user.get('sub') else None

    async with get_session() as session:
        _, events = await get_accessible_events(request, session, user_id=user_id)

    return templates.TemplateResponse(request, 'admin/event_list.html', {
        'events': events,
        **admin_flags,
    })
```

**Verify**: `grep -n 'admin_event_list' fastapi_app.py` — the function body no longer contains inline membership queries.

### Step 4: Update `admin_dashboard` to use the helper

`admin_dashboard` (~line 1404) also queries memberships inline to filter
events, but it additionally does a single-event redirect check. Update to use
`get_accessible_events` for the membership resolution while keeping the
redirect logic:

```python
@app.get('/admin/', dependencies=[Depends(require_admin)])
async def admin_dashboard(request: Request):
    from portal.database import get_session, list_booths_for_event
    from portal.auth import get_admin_flags, get_current_user, get_accessible_events

    admin_flags = await get_admin_flags(request)
    user = await get_current_user(request)
    user_id = int(user['sub']) if user and user.get('sub') else None

    async with get_session() as session:
        _, events = await get_accessible_events(request, session, user_id=user_id)

        if not admin_flags.get('is_super_admin') and len(events) == 1:
            return safe_redirect(url=f'/admin/events/{events[0].id}/', status_code=status.HTTP_303_SEE_OTHER)

        event_data = []
        for ev in events:
            db_booths = await list_booths_for_event(session, ev.id)
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
    ...  # remainder of template response unchanged
```

**Verify**: `grep -c 'list_memberships_for_user' fastapi_app.py` is further reduced.

### Step 5: Update `mission_control_grid`

`mission_control_grid` also has a duplicate `is_super_admin` detection block
(~lines 1296–1332), but it additionally uses `allowed_room_ids` for room-level
filtering. `get_accessible_events` doesn't expose room IDs — that part must
stay inline. Update ONLY the `is_super_admin` detection to use the helper,
keeping the room-level filtering as-is:

```python
# Replace only the is_super_admin detection block (~lines 1296-1316):
from portal.auth import get_accessible_events
async with get_session() as session:
    event = await get_event_by_slug(session, event_slug)
    if not event:
        raise HTTPException(status_code=404, detail='Event not found')

    is_super_admin, _ = await get_accessible_events(
        request, session, user_id=int(user['sub'])
    )
    # keep the existing allowed_room_ids block unchanged below this line
```

**STOP condition**: if the room-level access check in `mission_control_grid`
also uses `accessible_event_ids` in a way that would be broken by this
replacement, STOP and report rather than guessing.

**Verify**: `grep -n 'is_super_admin = False' fastapi_app.py | wc -l` → fewer than before this plan.

### Step 6: Run the full test suite

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q
```

**Verify**: exits 0, same pass count (385+).

## Test plan

Add to `tests/test_user_auth.py` or create `tests/test_access_control.py`.
Model after existing async tests in `test_user_auth.py` (use `anyio_backend`
fixture, `AsyncClient`, in-memory SQLite DB).

Tests:

1. **`test_get_accessible_events_super_admin_sees_all`** — mock request with
   valid `admin_token` cookie containing `admin=True`. Create 3 events. Call
   `get_accessible_events(request, session, user_id=1)`. Assert
   `is_super_admin == True` and `len(events) == 3`.

2. **`test_get_accessible_events_event_owner_filtered`** — mock request with
   no admin cookie. Create 2 events. Create a user + `EventMembership` for
   event 1 with `role='event_owner'`. Call helper with `user_id=user.id`. Assert
   `is_super_admin == False` and `events` contains only event 1.

3. **`test_get_accessible_events_no_membership_returns_empty`** — mock request
   with no admin cookie. Create 2 events. Create a user with no memberships.
   Call helper. Assert `events == []`.

4. **`test_get_accessible_events_user_id_none_returns_all_as_non_admin`** — mock
   request with no cookies. Create 2 events. Call helper with `user_id=None`.
   Assert `is_super_admin == False` and `len(events) == 2`.

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -k "accessible_events" -v
```

Expected: 4 new tests pass.

## Done criteria

- [ ] `grep -n 'get_accessible_events' portal/auth.py` → function defined
- [ ] `grep -c 'list_memberships_for_user.*int(user' fastapi_app.py` → 0 (no inline membership queries for the 4 routes)
- [ ] `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` exits 0
- [ ] 4 new access-control tests pass
- [ ] `git diff --name-only HEAD` shows only `portal/auth.py`, `fastapi_app.py`, and the test file
- [ ] `plans/README.md` status row updated

## STOP conditions

- Live code in the 4 routes doesn't match the excerpts (drifted).
- The `mission_control_grid` room-level check is entangled with the
  `is_super_admin` detection in a way that can't be cleanly split — stop and
  describe what you found.
- Any of the 4 routes serve different user roles than assumed (e.g., if
  `event_owner` is not the only relevant role — check by reading the handler
  to EOF before extracting).

## Maintenance notes

- If room-level (coordinator) access control is ever needed in additional
  routes, consider extending `get_accessible_events` to also return
  `accessible_room_ids: set[int]`.
- The `get_accessible_events` function calls `list_events` on every invocation.
  Once plan 002 (pagination) lands, the default `limit=100` applies here too;
  for super-admins with >100 events, the `_` events return in this helper may
  be truncated. Review when pagination UI is added.
- Future reviewer: check that the `admin_dashboard` redirect for single-event
  admins still works correctly after this change.
