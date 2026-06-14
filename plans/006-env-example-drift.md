# Plan 006: Fix .env.example drift (remove dead variable, add missing ones)

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6972d5b..HEAD -- .env.example portal/config.py`
> If either file changed, compare the "Current state" excerpts below before
> proceeding.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx / docs
- **Planned at**: commit `6972d5b`, 2026-06-14

## Why this matters

`.env.example` is the operator's first reference for what environment
variables to configure. It has two concrete problems:

1. **Dead variable**: `JITSI_PUBLIC_URL` is in `.env.example` but does not
   exist in `portal/config.py` `Settings`. Operators copy it into `.env` for
   no effect, causing confusion.
2. **Missing variables**: `DEBUG` (defaults to `True` in config — dangerous
   in production) and `NVIDIA_FUNCTION_ID` (required for NVIDIA Riva
   transcription) are absent from `.env.example`, so operators deploying
   those features get no guidance.

There is also a latent bug: `portal/config.py`'s `effective_mediamtx_internal_base`
property reads `self.mediamtx_internal_base`, but that field is not declared in
`Settings`. This plan fixes that as well.

## Current state

`.env.example` (relevant excerpt, ~line 23):
```
JITSI_PUBLIC_URL=http://localhost:8080
```
This variable does **not** appear in `portal/config.py`.

`portal/config.py` (full Settings class up to line ~75):
```python
class Settings(BaseSettings):
    ...
    debug: bool = Field(default=True)
    secret_key: str = 'change-me'
    ...
    mediamtx_rtsp_base: str = 'rtsp://mediamtx:8554'
    floor_bot_base: str = 'http://floor-bot:8080'

    @property
    def effective_mediamtx_internal_base(self) -> str:
        return self.mediamtx_internal_base or self.mediamtx_api_base
    ...
    nvidia_function_id: str = ''
```

`mediamtx_internal_base` is referenced in the property but **never declared**
as a field — accessing `settings.effective_mediamtx_internal_base` raises
`AttributeError`.

## Commands you will need

| Purpose      | Command                                                                                        | Expected on success |
|--------------|------------------------------------------------------------------------------------------------|---------------------|
| Tests        | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | 385 passed (or more) |
| Dead var check | `python3 -c "from portal.config import Settings; s = Settings(); print(s.effective_mediamtx_internal_base)"` | prints a URL, no AttributeError |
| Verify removed | `grep 'JITSI_PUBLIC_URL' .env.example`                                                       | no match            |
| Verify added   | `grep 'DEBUG\|NVIDIA_FUNCTION_ID' .env.example`                                              | both present        |

## Scope

**In scope**:
- `.env.example` — remove `JITSI_PUBLIC_URL`, add `DEBUG` and `NVIDIA_FUNCTION_ID`
- `portal/config.py` — declare `mediamtx_internal_base` as a proper field to
  fix the latent `AttributeError`

**Out of scope** (do NOT touch):
- `fastapi_app.py`
- Any templates or JS files
- `docker-compose.yml` (has its own env var references)

## Git workflow

- Branch: `advisor/006-env-example-drift`
- One commit: `docs: fix .env.example drift and add missing mediamtx_internal_base field`

## Steps

### Step 1: Add missing `mediamtx_internal_base` field to Settings

In `portal/config.py`, add `mediamtx_internal_base` as a declared field.
Place it immediately before the `effective_mediamtx_internal_base` property.
The current block looks like:

```python
    mediamtx_rtsp_base: str = 'rtsp://mediamtx:8554'
    floor_bot_base: str = 'http://floor-bot:8080'


    @property
    def effective_mediamtx_internal_base(self) -> str:
        return self.mediamtx_internal_base or self.mediamtx_api_base
```

Change to:

```python
    mediamtx_rtsp_base: str = 'rtsp://mediamtx:8554'
    # Optional internal MediaMTX base URL. When empty, falls back to
    # mediamtx_api_base. Set in Docker to http://mediamtx:8888.
    mediamtx_internal_base: str = ''
    floor_bot_base: str = 'http://floor-bot:8080'


    @property
    def effective_mediamtx_internal_base(self) -> str:
        return self.mediamtx_internal_base or self.mediamtx_api_base
```

**Verify**: `python3 -c "from portal.config import Settings; s = Settings(); print(s.effective_mediamtx_internal_base)"` → prints a URL without `AttributeError`.

### Step 2: Remove JITSI_PUBLIC_URL from .env.example

Find the line:
```
JITSI_PUBLIC_URL=http://localhost:8080
```
and delete it (the entire line). It is around line 23 of `.env.example`,
inside the `# ── Jitsi Meet ───` section.

**Verify**: `grep 'JITSI_PUBLIC_URL' .env.example` → no matches.

### Step 3: Add DEBUG to .env.example

After the `SECRET_KEY=change-me` line (line 3 of `.env.example`), add:

```
# Set to false in production. Default is true (enables debug error pages).
DEBUG=false
```

Place it so the resulting block is:
```
SECRET_KEY=change-me
# Set to false in production. Default is true (enables debug error pages).
DEBUG=false
```

**Verify**: `grep -n 'DEBUG' .env.example` → shows the new line.

### Step 4: Add NVIDIA_FUNCTION_ID to .env.example

The NVIDIA transcription section is not currently present in `.env.example`.
Add a new section at the end of the file (before or after the DATABASE_URL line):

```
# ── NVIDIA Riva (optional transcription provider) ────────────────────────────
# Required only when using the 'nvidia' transcription provider.
# NVIDIA_FUNCTION_ID=<your-nvidia-cloud-function-id>
```

Keep it commented out (with `#`) so it is clearly optional.

**Verify**: `grep 'NVIDIA_FUNCTION_ID' .env.example` → shows the commented-out line.

### Step 5: Run the full test suite

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q
```

**Verify**: exits 0, 385 passed (or more).

## Test plan

Add one test to `tests/test_fastapi_app.py` or a new `tests/test_config.py`:

1. **`test_effective_mediamtx_internal_base_fallback`** — instantiate Settings
   with `mediamtx_internal_base=''` and `mediamtx_api_base='http://localhost:9997'`,
   assert `settings.effective_mediamtx_internal_base == 'http://localhost:9997'`.
2. **`test_effective_mediamtx_internal_base_override`** — instantiate Settings
   with `mediamtx_internal_base='http://mediamtx:8888'`, assert
   `settings.effective_mediamtx_internal_base == 'http://mediamtx:8888'`.

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -k "mediamtx_internal" -q
```

Expected: 2 new tests pass.

## Done criteria

- [ ] `grep 'JITSI_PUBLIC_URL' .env.example` → no matches
- [ ] `grep 'DEBUG' .env.example` → match found (uncommented)
- [ ] `grep 'NVIDIA_FUNCTION_ID' .env.example` → match found
- [ ] `grep 'mediamtx_internal_base' portal/config.py` → match found (as field declaration, not just in property body)
- [ ] `python3 -c "from portal.config import Settings; s = Settings(); s.effective_mediamtx_internal_base"` → no exception
- [ ] `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` exits 0
- [ ] `git diff --name-only HEAD` shows only `.env.example` and `portal/config.py` (plus test file)
- [ ] `plans/README.md` status row updated

## STOP conditions

- `portal/config.py` content around `effective_mediamtx_internal_base` doesn't
  match the excerpt (codebase drifted).
- Removing `JITSI_PUBLIC_URL` would break a CI step that validates `.env.example`
  — check `.github/workflows/` for any such check before deleting.

## Maintenance notes

- When new Settings fields are added to `portal/config.py`, update
  `.env.example` in the same commit (required by project documentation policy
  in `agents.md`).
- `DEBUG=false` in `.env.example` documents the production default. The
  guard added in plan 005 checks `settings.debug` to determine whether to
  enforce the secret_key check.
