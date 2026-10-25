# Contributing to VoxBento

Welcome! VoxBento is a real-time browser interpretation platform (part of Eventyay). It allows interpreters to speak into their browser (via WebRTC/WHIP) and broadcasts that audio seamlessly to attendees (via WHEP), all coordinated by FastAPI and MediaMTX.

This guide will help you set up your local development environment, understand the basics, and open your first Pull Request.

---

## 1. Quick Start: Local Development Setup

We use `uv` for lightning-fast Python dependency management and Docker for our media infrastructure.

### Prerequisites
- [uv](https://github.com/astral-sh/uv) (Python package manager)
- [Node.js](https://nodejs.org/) (for JS typechecking/linting)
- [Docker](https://www.docker.com/) (for MediaMTX and Jitsi)

### Setup Commands

Run the environment-generation step below only for **NEW local setups**. Do not overwrite an existing `.env` or rotate existing keys with it; the command refuses to replace an existing file. The generated file is owner-readable only and is installed atomically after the complete configuration has been written.

```bash
# 1. Clone the repository
git clone https://github.com/fossasia/voxbento.git
cd voxbento

# 2. Install local Python dependencies (required for IDE linting & running tests)
uv sync --all-groups

# 3. Set up your environment variables and generate required secrets
uv run python -c "
import secrets, re
import os, tempfile
from pathlib import Path

target = Path('.env')
if target.exists():
    raise SystemExit('.env already exists; refusing to replace it')
env = open('.env.example').read()
for name in (
    'SECRET_KEY', 'API_KEY_ENCRYPTION_KEY',
    'MEDIAMTX_AUTH_HOOK_SECRET', 'BOOTH_ACCESS_TOKEN',
):
    env = re.sub(rf'^{name}=.*$', f'{name}={secrets.token_hex(32)}', env, flags=re.MULTILINE)
with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir='.', prefix='.env.', delete=False) as f:
    os.chmod(f.name, 0o600)
    f.write(env)
    f.flush()
    os.fsync(f.fileno())
temporary = Path(f.name)
try:
    os.link(temporary, target)
except FileExistsError:
    raise SystemExit('.env already exists; refusing to replace it')
finally:
    temporary.unlink(missing_ok=True)
"

# 4. Start the entire stack (FastAPI, MediaMTX, and Jitsi)
docker compose up -d --build
```

Defaults keep debug disabled (`DEBUG=false`) and startup secret checks active. Developers may explicitly set `DEBUG=true` in their own `.env` for local-only debugging; never enable it for deployments.

You can now visit the app at `http://localhost:8000`. 
To access the admin panel, sign in with an account having admin privileges at `http://localhost:8000/login?next=/admin/`.

---

## 2. Testing Your Changes

Before opening a PR, ensure that the linter and test suite pass.

```bash
# Run Python linter and formatter
uv run ruff check .
uv run ruff format .

# Run Javascript syntax checks
node --check portal/static/js/interpreter-booth.js
node --check portal/static/js/whep-listener.js
node --check portal/static/js/admin.js

# Run the Pytest suite
uv run pytest tests/ -v
```

CI runs the checks above (except `uv run ruff format .`), and also runs a separate Docker build and full-stack smoke test. Your PR will be blocked if any of these CI checks fail.

---

## 3. How the Platform Works

- **Audio Flow**: Interpreters speak into their browser mic -> pushed via WebRTC (WHIP) to MediaMTX -> broadcast via WebRTC (WHEP) to listeners. Python is **never** in the audio path; it only coordinates state.
- **Backend**: FastAPI serves the frontend Jinja2 templates, handles REST API calls, and manages WebSocket connections that coordinate the live state of the booths (e.g., who is the active interpreter).
- **Frontend**: Plain Vanilla ES modules in `portal/static/js/`. No React, no Vue, and no build steps. 
- **Database**: We use SQLAlchemy 2.0 (async) and Alembic for migrations.
- **Roles and permissions**: VoxBento has four roles (Admin, Event owner, Room coordinator, Interpreter), defined once in `portal/roles.py`. See the [roles table](website/docs/admin/members-and-roles.mdx) for what each role can do and where it can be given. Import role names and permissions from `portal.roles`; never type a role name elsewhere (`tests/test_roles.py` checks this). "Admin" means only the platform role.

If you need more deep-dive technical context, check out our internal documentation in the `.agents/context/` directory (e.g., `ROUTE_MAP.md`, `DATABASE_MAP.md`, `AI_WORKFLOWS.md`).

---

## 4. Making Database Changes

If your feature requires changes to the database schema:

```bash
# 1. Edit your SQLAlchemy models in portal/models.py
# 2. Generate a new migration file locally
uv run alembic revision --autogenerate -m "describe your change"
# Note: Please rename the generated revision ID to be sequential (e.g., 009, 010) to match our existing migrations. Do not use Alembic's default random hex IDs.
# 3. Apply it to the running Docker container
docker compose exec portal uv run alembic upgrade head
```

Always commit the generated migration files in `alembic/versions/`. Do **not** commit your local `.db` files.

---

## 5. Development Workflow

1. Fork the repository on GitHub, then clone your fork.
2. Create a branch for your feature: `git checkout -b feat/your-feature-name`
3. Keep your commits atomic and focused.
4. Make sure you don't use inline scripts in HTML templates (strict CSP is enforced).
5. Do not use jQuery or external UI frameworks.
6. Push your branch and open a PR against `dev`.
7. **Keep no more than 3 PRs open at a time when possible.** Limiting the number of concurrent PRs helps maintainers and reviewers focus on each change and provide thorough, timely reviews. Before opening another PR, try to get one of your existing PRs merged or otherwise closed.

Thank you for contributing!
