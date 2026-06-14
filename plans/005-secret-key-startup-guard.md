# Plan 005: Add startup guard for weak default secret_key

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat 6972d5b..HEAD -- portal/config.py fastapi_app.py`
> If either file changed, compare the "Current state" excerpts below before
> proceeding.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `6972d5b`, 2026-06-14

## Why this matters

`portal/config.py` sets `secret_key: str = 'change-me'` as the default. This
value is used to sign **all JWTs** (participant tokens, user tokens, admin
tokens) via `effective_jwt_secret`. An operator who deploys without setting
`SECRET_KEY` produces tokens that any attacker knowing the default can forge,
granting arbitrary role access to every booth. The fix adds a fail-fast check
at startup that refuses to run in non-debug mode with the known-weak defaults.

## Current state

`portal/config.py`:

```python
# line 19
debug: bool = Field(default=True)
secret_key: str = 'change-me'
```

```python
# line 60
@property
def effective_jwt_secret(self) -> str:
    return self.jwt_secret or self.secret_key
```

`fastapi_app.py` lifespan (lines 55–60) — the startup hook that runs before
any request is served:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    import portal.transcription as ts
    import httpx
    ts.shared_http_client = httpx.AsyncClient(timeout=10.0)
    yield
```

No validation of `secret_key` exists anywhere in startup code.

Repo convention for startup errors: `fastapi_app.py` uses `raise` inside
lifespan to abort startup (uvicorn propagates the exception and exits). Tests
use `API_KEY_ENCRYPTION_KEY="..."` in env to satisfy a similar existing guard.

## Commands you will need

| Purpose     | Command                                                                                        | Expected on success |
|-------------|------------------------------------------------------------------------------------------------|---------------------|
| Tests       | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` | 385 passed (or more) |
| Targeted    | `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_fastapi_app.py -q` | all pass |

## Scope

**In scope**:
- `portal/config.py` — add a `validate_secrets` method or property
- `fastapi_app.py` — call the validation in `lifespan`
- `tests/test_fastapi_app.py` — add tests that verify the guard fires

**Out of scope** (do NOT touch):
- `portal/auth.py` — no changes to JWT signing
- Template files
- `.env.example` — a separate plan (006) handles env doc drift

## Git workflow

- Branch: `advisor/005-secret-key-startup-guard`
- One commit: `security: fail fast on weak default secret_key in production`

## Steps

### Step 1: Add validation method to Settings

In `portal/config.py`, add a method `validate_production_secrets` to the
`Settings` class. Place it after the `effective_jwt_secret` property
(currently around line 60):

```python
def validate_production_secrets(self) -> None:
    """Raise RuntimeError if running in non-debug mode with known-weak defaults.

    Called once at application startup. Does not raise in debug mode so that
    local development works without a configured SECRET_KEY.
    """
    if self.debug:
        return
    _WEAK_DEFAULTS = {'change-me', '', 'secret', 'your-secret-key'}
    if self.effective_jwt_secret in _WEAK_DEFAULTS:
        raise RuntimeError(
            "SECRET_KEY (or JWT_SECRET) is set to a known-weak default value. "
            "Set a strong random value before running in production. "
            "Generate one with: openssl rand -hex 32"
        )
    if not self.admin_password and self.admin_password == '':
        # Admin login is disabled when admin_password is empty — that is
        # intentional and safe. No guard needed here.
        pass
```

**Verify**: `grep -n 'validate_production_secrets' portal/config.py` → shows the new method.

### Step 2: Call the guard in the lifespan startup hook

In `fastapi_app.py`, update the `lifespan` function:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    import portal.transcription as ts
    import httpx
    settings.validate_production_secrets()
    ts.shared_http_client = httpx.AsyncClient(timeout=10.0)
    yield
```

**Verify**: `grep -A6 'async def lifespan' fastapi_app.py` → shows `settings.validate_production_secrets()` before `yield`.

### Step 3: Run the full test suite

The existing tests run with `SECRET_KEY` unset (falling back to 'change-me')
but CI sets `debug=True` implicitly (default). Confirm the tests still pass:

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q
```

**Verify**: exits 0, 385 passed (or more).

If tests fail because the guard triggers in test mode, it means `debug` is
`False` somewhere in the test fixtures. Check `tests/conftest.py` — if
`DEBUG=` is not set there, add `os.environ.setdefault('DEBUG', 'true')` before
the Settings import in conftest, or set `os.environ['DEBUG'] = 'true'` alongside
the existing `os.environ['API_KEY_ENCRYPTION_KEY']` line. **STOP and report**
if you need to modify conftest and the change is not obvious.

## Test plan

Add tests to `tests/test_fastapi_app.py`. Model after existing tests that
override settings for testing (search for `monkeypatch` or `Settings` in that
file to find the pattern).

New tests (add to a `# --- Secret guard tests ---` section):

1. **`test_weak_secret_raises_in_production`** — patch `settings.debug = False`
   and `settings.secret_key = 'change-me'`, then call
   `settings.validate_production_secrets()`, assert it raises `RuntimeError`
   containing "SECRET_KEY".

2. **`test_weak_secret_allowed_in_debug_mode`** — patch `settings.debug = True`
   and `settings.secret_key = 'change-me'`, then call
   `settings.validate_production_secrets()` and assert it does NOT raise.

3. **`test_strong_secret_passes_production`** — patch `settings.debug = False`
   and `settings.secret_key = 'a-strong-random-64-char-hex-value-here-xxxxx'`,
   call `settings.validate_production_secrets()` and assert it does NOT raise.

```bash
API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/test_fastapi_app.py -k "secret" -q
```

Expected: 3 new tests pass.

## Done criteria

- [ ] `grep 'validate_production_secrets' portal/config.py` → match found
- [ ] `grep 'validate_production_secrets' fastapi_app.py` → match found (inside lifespan)
- [ ] `API_KEY_ENCRYPTION_KEY="ci-test-encryption-key-must-be-32-chars-long" uv run pytest tests/ -q` exits 0
- [ ] 3 new guard tests exist and pass
- [ ] `git diff --name-only HEAD` shows only `portal/config.py`, `fastapi_app.py`, `tests/test_fastapi_app.py`
- [ ] `plans/README.md` status row updated

## STOP conditions

- The `lifespan` function body in `fastapi_app.py` doesn't match the excerpt —
  the codebase has drifted.
- The existing test suite starts failing because `debug=False` is set
  somewhere unexpected and the guard fires. Report which test and the traceback.
- Adding the guard requires touching auth or middleware — it should only live
  in `lifespan` calling `settings.validate_production_secrets()`.

## Maintenance notes

- If additional known-weak values are found in production reports, add them to
  `_WEAK_DEFAULTS` in `validate_production_secrets`.
- The guard intentionally does NOT check `admin_password` — empty admin
  password disables the admin login UI, which is a valid intentional config.
- If `api_key_encryption_key` validation is ever needed (similar guard for
  encryption key), follow the same pattern: method on Settings, called in
  lifespan.
