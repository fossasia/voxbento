# Plan 004: Fix js.instructions.md applyTo glob to match actual JS file locations

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6972d5b..HEAD -- .github/instructions/js.instructions.md`
> If this file changed, compare the "Current state" excerpt below against the
> live file before proceeding.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `6972d5b`, 2026-06-14

## Why this matters

`.github/instructions/js.instructions.md` has `applyTo: 'app/**/*.{js,ts,vue}'`
in its YAML frontmatter. Actual JavaScript source files are in `static/js/`
(not `app/`), so VS Code and agent tooling that honour the `applyTo` filter
will never load these instructions when editing or reviewing JS files. The fix
is a one-line frontmatter change.

## Current state

`.github/instructions/js.instructions.md` lines 1–4:

```
---
description: 'JavaScript development standards'
applyTo: 'app/**/*.{js,ts,vue}'
---
```

Actual JS files (confirmed by `ls static/js/`):
- `static/js/interpreter-booth.js`
- `static/js/whep-listener.js`
- `static/js/admin.js`
- `static/js/interpreter-landing.js`
- `static/js/mission-control.js`

There are no `.ts` or `.vue` files in this repo (vanilla ES modules only, no
build step). There are no JS files under `app/`.

Exemplar: `.github/instructions/python.instructions.md` uses
`applyTo: '**/*.py'` — a correct glob that matches all Python files. Match
that style.

## Commands you will need

| Purpose      | Command                                                                                        | Expected on success |
|--------------|------------------------------------------------------------------------------------------------|---------------------|
| Verify glob  | `grep 'applyTo' .github/instructions/js.instructions.md`                                      | shows new value     |
| Tests        | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | 385 passed (or more) |

## Scope

**In scope**:
- `.github/instructions/js.instructions.md` — change only the `applyTo` line

**Out of scope** (do NOT touch):
- The body of `js.instructions.md` (content is correct, only the glob is wrong)
- Any other instruction file
- Any source files

## Git workflow

- Branch: `advisor/004-js-instructions-applyto`
- One commit: `dx: fix js.instructions.md applyTo glob to match static/js/`

## Steps

### Step 1: Update the applyTo glob

In `.github/instructions/js.instructions.md`, change the frontmatter from:

```yaml
---
description: 'JavaScript development standards'
applyTo: 'app/**/*.{js,ts,vue}'
---
```

To:

```yaml
---
description: 'JavaScript development standards'
applyTo: 'static/**/*.js'
---
```

The glob `static/**/*.js` matches all `.js` files in `static/js/` and any
future subdirectories, which is exactly where all JavaScript lives in this
repo.

**Verify**: `grep 'applyTo' .github/instructions/js.instructions.md` → `applyTo: 'static/**/*.js'`

### Step 2: Confirm no other instruction files reference the old path

```bash
grep -r 'app/\*\*' .github/instructions/
```

Expected: no matches (or only matches in files other than `js.instructions.md`
that have their own correct paths).

### Step 3: Run full test suite to confirm nothing is broken

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q
```

**Verify**: exits 0, same pass count (385 or more).

## Test plan

No new tests needed — this is a one-line documentation fix with no effect on
runtime code.

## Done criteria

- [ ] `grep 'applyTo' .github/instructions/js.instructions.md` → `applyTo: 'static/**/*.js'`
- [ ] `grep -r 'app/\*\*' .github/instructions/` → no matches
- [ ] `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` exits 0
- [ ] `git diff --name-only HEAD` shows only `.github/instructions/js.instructions.md`
- [ ] `plans/README.md` status row updated

## STOP conditions

- The frontmatter format in `.github/instructions/js.instructions.md` doesn't
  match the excerpt above (it may have been updated since this plan was written).

## Maintenance notes

- If JS files are ever moved out of `static/js/` or a `src/` directory is
  re-introduced with a build step, update the glob accordingly.
- If TypeScript is ever introduced, change the glob to
  `static/**/*.{js,ts}` or the build-output directory.
