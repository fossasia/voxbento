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

```bash
# 1. Clone the repository
git clone https://github.com/fossasia/voxbento.git
cd voxbento

# 2. Install local Python dependencies (required for IDE linting & running tests)
uv sync --all-groups

# 3. Set up your environment variables
cp .env.example .env

# 4. Start the entire stack (FastAPI, MediaMTX, and Jitsi)
docker compose up -d --build
```

You can now visit the app at `http://localhost:8000`. 
To access the admin panel, set `ADMIN_PASSWORD` explicitly in `.env` during setup, then go to `http://localhost:8000/admin/login`. There is no usable default password.

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

If you need more deep-dive technical context, check out our internal documentation in the `.agents/context/` directory (e.g., `ROUTE_MAP.md`, `DATABASE_MAP.md`, `AI_WORKFLOWS.md`).

---

## 4. Making Database Changes

If your feature requires changes to the database schema:

```bash
# 1. Edit your SQLAlchemy models in portal/models.py
# 2. Generate a new migration file locally
uv run alembic revision --autogenerate -m "describe your change"
# 3. Apply it to the running Docker container
docker compose exec portal uv run alembic upgrade head
```
Always commit the generated migration files in `alembic/versions/`. Do **not** commit your local `.db` files.

---

## 5. Development Workflow

1. Create a branch for your feature: `git checkout -b feat/your-feature-name`
2. Keep your commits atomic and focused.
3. Make sure you don't use inline scripts in HTML templates (strict CSP is enforced).
4. Do not use jQuery or external UI frameworks.
5. Push your branch and open a PR against `main`.

Thank you for contributing!
