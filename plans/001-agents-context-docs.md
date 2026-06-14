# Plan 001: Create the missing .github/.agents context and skills directories

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 6972d5b..HEAD -- agents.md`
> If `agents.md` changed since this plan was written, compare the "Current
> state" excerpts below against the live file before proceeding; on a mismatch,
> treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: docs / dx
- **Planned at**: commit `6972d5b`, 2026-06-14

## Why this matters

`agents.md` is the primary onboarding document for every AI agent and
developer working on this repo. It instructs readers to "Read these files in
order" and lists 7 context files and 12+ skills under `.github/.agents/` —
but that directory does not exist. Any agent following these instructions will
fail to load the required context, producing plans that are inconsistent with
the actual codebase. Creating accurate snapshot files fixes the broken
guidance system without changing any product code.

## Current state

- `agents.md` lines 13–46: references 7 context files under
  `.github/.agents/context/` and 13 skill `SKILL.md` files under
  `.github/.agents/skills/`. None exist — `find .github/.agents -type f`
  returns nothing.
- `agents.md` line 6 says: *"Read these files in order before any task"* and
  lists `REPOSITORY_CONTEXT.md`, `CHANGE_IMPACT_MAP.md`, `ROUTE_MAP.md`,
  `DATABASE_MAP.md`, `TRANSCRIPTION_MAP.md`, `AI_WORKFLOWS.md`,
  `TECHNICAL_DEBT_REPORT.md`.
- Existing good reference for writing style/convention: `agents.md` itself —
  it uses clear section headings, tables, and fenced code blocks. Match that
  style in the new files.

## Commands you will need

| Purpose       | Command                                        | Expected on success |
|---------------|------------------------------------------------|---------------------|
| Tests         | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | `385 passed` (or more) |
| Verify files  | `find .github/.agents -type f \| sort`         | lists all created files |
| Verify no broken links | `grep -o '\.github/\.agents/[^)]*' agents.md \| while read f; do [ -f "$f" ] && echo "OK $f" \|\| echo "MISSING $f"; done` | all lines start with `OK` |

## Scope

**In scope** (the only files you should create):
- `.github/.agents/context/REPOSITORY_CONTEXT.md`
- `.github/.agents/context/CHANGE_IMPACT_MAP.md`
- `.github/.agents/context/ROUTE_MAP.md`
- `.github/.agents/context/DATABASE_MAP.md`
- `.github/.agents/context/TRANSCRIPTION_MAP.md`
- `.github/.agents/context/AI_WORKFLOWS.md`
- `.github/.agents/context/TECHNICAL_DEBT_REPORT.md`
- `.github/.agents/skills/repo-navigation/SKILL.md`
- `.github/.agents/skills/architecture-review/SKILL.md`
- `.github/.agents/skills/route-analysis/SKILL.md`
- `.github/.agents/skills/database-analysis/SKILL.md`
- `.github/.agents/skills/transcription-analysis/SKILL.md`
- `.github/.agents/skills/provider-analysis/SKILL.md`
- `.github/.agents/skills/pr-review/SKILL.md`
- `.github/.agents/skills/security-audit/SKILL.md`
- `.github/.agents/skills/docker-review/SKILL.md`
- `.github/.agents/skills/deployment-review/SKILL.md`
- `.github/.agents/skills/incident-investigation/SKILL.md`
- `.github/.agents/skills/test-generation/SKILL.md`
- `.github/.agents/skills/production-readiness-review/SKILL.md`

**Out of scope** (do NOT touch):
- `agents.md` — the link targets must remain exactly as written there
- Any Python, JS, or template source files
- Any existing files in `.github/instructions/` or `.github/workflows/`

## Git workflow

- Branch: `advisor/001-agents-context-docs`
- One commit per logical group (e.g., all context files together, all skill stubs together)
- Message style: `docs: add missing .github/.agents context and skill files`

## Steps

### Step 1: Create the context directory and REPOSITORY_CONTEXT.md

Read `agents.md`, `fastapi_app.py` (lines 1-60), `portal/config.py`,
`portal/booth_identity.py`, and `portal/roles.py` to gather accurate facts.
Then create `.github/.agents/context/REPOSITORY_CONTEXT.md` covering:

- Stack: FastAPI + MediaMTX + Jitsi, Python 3.13, uv, aiosqlite
- Auth: JWT types (participant/user/admin), cookies, bcrypt, roles hierarchy
- Booth identity: `make_booth_id`, `make_mediamtx_path`, slug/language constraints
- Services and ports table (from `agents.md` — already accurate there)
- Role model table (from `agents.md`)
- Key invariants (copy the 11 points from `agents.md` "Core Invariants" section)

Keep the file under 300 lines. Write concise tables and bullet points — not prose.

**Verify**: `wc -l .github/.agents/context/REPOSITORY_CONTEXT.md` → less than 300

### Step 2: Create CHANGE_IMPACT_MAP.md

Read `fastapi_app.py`, `portal/database.py`, `portal/booth_state.py`,
`portal/auth.py`, `portal/transcription/worker.py`, and
`portal/translations/worker.py` (file headers and key function signatures only).
Create `.github/.agents/context/CHANGE_IMPACT_MAP.md` as a table mapping each
common task type to which files to touch. Rows to include at minimum:

| Task | Files to modify | Files to read first | Tests to run |
|------|----------------|---------------------|--------------|
| Add a new HTTP route | `fastapi_app.py` | `portal/auth.py`, `portal/database.py` | `tests/test_fastapi_app.py` |
| Change booth state logic | `portal/booth_state.py` | `fastapi_app.py` (WS handlers) | `tests/test_booth_state.py` |
| Add a DB column | `portal/models.py`, new `alembic/versions/NNN_*.py` | existing migrations | `tests/test_database.py` |
| Change auth/roles | `portal/auth.py`, `portal/roles.py` | `fastapi_app.py` | `tests/test_roles.py`, `tests/test_user_auth.py` |
| Change transcription | `portal/transcription/worker.py`, provider file | `portal/transcription/providers/base.py` | `tests/test_transcription_concurrency.py` |
| Change invite/token flow | `portal/database.py`, `fastapi_app.py` | `portal/auth.py` | `tests/test_memberships_tokens.py` |

**Verify**: `grep -c '|' .github/.agents/context/CHANGE_IMPACT_MAP.md` → 8 or more

### Step 3: Create ROUTE_MAP.md

Read `fastapi_app.py` and grep for all `@app.get`, `@app.post`, `@app.delete`,
`@app.put`, `@app.patch`, `@app.websocket` decorators:

```bash
grep -n '@app\.\(get\|post\|delete\|put\|patch\|websocket\)' fastapi_app.py | head -80
```

Create `.github/.agents/context/ROUTE_MAP.md` with a complete route table:

| Method | Path | Auth | Handler function |
|--------|------|------|-----------------|

Group by: Pages, Admin, API, WebSocket. Include the auth requirement for each
route (none / require_admin / require_user / session_token cookie).

**Verify**: `grep -c '|' .github/.agents/context/ROUTE_MAP.md` → 50 or more

### Step 4: Create DATABASE_MAP.md

Read `portal/models.py` and `portal/database.py`. Create
`.github/.agents/context/DATABASE_MAP.md` covering:

- Table schemas (one section per table: columns, types, constraints, indexes)
- CRUD helpers table (function name → purpose → module)
- Migration history (list `alembic/versions/` filenames and their description)
- How to run migrations: `uv run alembic upgrade head`

**Verify**: `grep -c '__tablename__' .github/.agents/context/DATABASE_MAP.md` → matches `grep -c '__tablename__' portal/models.py`

### Step 5: Create TRANSCRIPTION_MAP.md

Read `portal/transcription/worker.py`, `portal/transcription/aggregator.py`,
`portal/transcription/providers/base.py`, and each provider file. Create
`.github/.agents/context/TRANSCRIPTION_MAP.md` covering:

- Pipeline diagram (text): MediaMTX RTSP → ffmpeg PCM → TranscriptionProvider → CaptionAggregator → WebSocket broadcast
- Provider table: name, class, file, status (functional/stub), external dependency
- Worker lifecycle: `start_transcription_worker` / `stop_transcription_worker` signatures
- `MAX_TOTAL_WORKERS` = 10
- `CaptionAggregator`: forced finalization at 15 s / 50 words

**Verify**: `grep -c 'provider' .github/.agents/context/TRANSCRIPTION_MAP.md` → 5 or more

### Step 6: Create AI_WORKFLOWS.md

Create `.github/.agents/context/AI_WORKFLOWS.md` with step-by-step playbooks
for the most common agent tasks:

1. **Add a new API endpoint** — read route map, add to `fastapi_app.py`, add
   test in `tests/test_fastapi_app.py`, run `uv run pytest tests/ -q`.
2. **Add a new DB column** — create alembic migration, update `portal/models.py`,
   run `uv run alembic upgrade head`, add test in `tests/test_database.py`.
3. **Add a new transcription provider** — create
   `portal/transcription/providers/{name}.py` inheriting `TranscriptionProvider`,
   register in `portal/transcription/worker.py` PROVIDERS dict, add to
   `ProviderEnum` in `portal/transcription/constants.py`.
4. **Change booth state / handoff logic** — edit `portal/booth_state.py`, update
   `tests/test_booth_state.py`, verify one-active-publisher invariant holds.

**Verify**: `grep -c 'Step\|step' .github/.agents/context/AI_WORKFLOWS.md` → 8 or more

### Step 7: Create TECHNICAL_DEBT_REPORT.md

Create `.github/.agents/context/TECHNICAL_DEBT_REPORT.md` as a summary of
known issues with status. Source: the findings from this advisor run (commit
`6972d5b`, 2026-06-14). Include:

- Table: Finding ID | Summary | Severity | Plan | Status
- Row for each finding #1–#11 from the advisor run
- Note which have plans written under `plans/` (001–009)

**Verify**: `grep -c '|' .github/.agents/context/TECHNICAL_DEBT_REPORT.md` → 12 or more

### Step 8: Create skill stub files

For each skill referenced in `agents.md` under `.github/.agents/skills/`, create
a `SKILL.md` stub that tells an agent:
- What this skill is for (1 sentence)
- Which files to read first (specific paths)
- Which tests to run to verify changes
- A pointer back to `agents.md` for full context

All 13 skill files follow the same 4-section template. Keep each under 60 lines.

Skills to create (directory name → short description):
- `repo-navigation` → Navigate and explore the VoxBento codebase efficiently
- `architecture-review` → Review system architecture and module design
- `route-analysis` → Analyze FastAPI routes, auth requirements, and data flow
- `database-analysis` → Analyze SQLAlchemy models, migrations, and CRUD helpers
- `transcription-analysis` → Analyze the transcription pipeline and providers
- `provider-analysis` → Analyze and compare transcription/translation providers
- `pr-review` → Review pull requests against codebase conventions
- `security-audit` → Audit security posture against OWASP Top 10
- `docker-review` → Review Docker Compose and container configuration
- `deployment-review` → Review deployment readiness and Caddy/Dockerfile setup
- `incident-investigation` → Investigate production incidents and root causes
- `test-generation` → Generate tests following existing test patterns
- `production-readiness-review` → Audit for production readiness

**Verify**: `find .github/.agents/skills -name 'SKILL.md' | wc -l` → 13

## Test plan

No product code is changed; no new tests are needed. Run the existing suite to
confirm nothing is broken:

```
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q
```

Expected: same pass count as before this plan (currently 385 passed).

## Done criteria

- [ ] `find .github/.agents -type f | sort` lists 20 files (7 context + 13 skills)
- [ ] `grep -o '\.github/\.agents/[^)]*' agents.md | while read f; do [ -f "$f" ] && echo "OK $f" || echo "MISSING $f"; done` prints only `OK` lines
- [ ] `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` exits 0 with same pass count
- [ ] No files outside the in-scope list are modified (`git diff --name-only HEAD | grep -v '^\.github/\.agents'` is empty)
- [ ] `plans/README.md` status row updated

## STOP conditions

- The link paths in `agents.md` don't match what you're about to create (drift check failed)
- Any step's verification fails twice after a reasonable attempt
- You find that a referenced file path in `agents.md` contains characters that can't be used as a filesystem path

## Maintenance notes

- When routes, DB schema, or transcription providers change, update the
  corresponding context file in the same commit (required by `agents.md`
  documentation policy).
- `TECHNICAL_DEBT_REPORT.md` should be updated when a plan from `plans/` is
  marked DONE or REJECTED.
- If `agents.md` itself is edited to add new context file references, this
  plan must be re-run to create the new files.
