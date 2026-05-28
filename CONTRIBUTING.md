# Contributing to Eventyay Interpretation Portal

## Setup

```bash
git clone https://github.com/fossasia/eventyay-interpretation-portal
cd eventyay-interpretation-portal
uv sync --all-groups     # installs runtime + dev dependencies
```

You also need [MediaMTX](https://github.com/bluenviron/mediamtx/releases) to test
audio ingest end-to-end, but it is not required to run the Python test suite.

## Running tests

```bash
uv run pytest tests/ -v
node --check static/js/interpreter-booth.js   # JS syntax check
```

All 27 tests must pass before opening a PR. The same checks run in CI
(`.github/workflows/tests.yml`).

## Branch and PR workflow

- Branch off `main`: `git checkout -b feat/your-feature`
- Keep commits focused and atomic (one concern per commit)
- Open PRs against `main`
- All CI checks must be green before merge

## Code conventions

### Python

- Python 3.13+; run with `uv`
- `asyncio.Lock` for all shared mutable state in `BoothRegistry`
- Use specific exception types (`ValueError`, `PermissionError`) — do not catch bare `Exception`
- `portal.*` namespace for new imports; no `pretix.*`, `pretalx.*`, or `venueless.*`

### JavaScript

- Plain browser ES modules — no jQuery, no Alpine, no bundler required for `interpreter-booth.js`
- No inline scripts in HTML templates
- Use `element.textContent` / `element.setAttribute` / `element.dataset` — never assign user-controlled data to `innerHTML`

### Django templates / Jinja2

- All user-controlled values must be escaped via the template engine or `escapeHtml()` in JS

## Architecture constraints

- **Python is never in the audio path.** Audio flows: browser mic → WHIP → MediaMTX → HLS → attendee. Do not add aiortc or similar.
- **No Flask, no Socket.IO.** FastAPI + native WebSocket is the sole backend.
- **In-memory state only.** `BoothRegistry` lives in process memory. Do not add a database dependency without a design discussion.
- **Booth fields are immutable after creation.** `language` and `channel_id` on a `Booth` object are set on first join and not overwritten.

## Audio handoff

The seamless interpreter-switch relies on the silence-mode handoff in
`static/js/interpreter-booth.js → applyBoothState`. If you change timing
constants (`700 ms` outgoing silence window, `400 ms` retry interval), test with
a real MediaMTX instance and VLC to verify HLS continuity.

## Dependency management

```bash
# Add a runtime dep
uv add <package>

# Add a dev dep
uv add --dev <package>

# Always commit the updated uv.lock
uv sync --all-groups --python 3.13   # regenerate lock
git add pyproject.toml uv.lock
```

## Security

- Never pass user-controlled data to `innerHTML` / `outerHTML` / `document.write`
- Validate and escape all inputs at system boundaries
- Keep `SECRET_KEY` and `BOOTH_ACCESS_TOKEN` out of version control (use `.env`)
- Sourcery runs security checks on every PR — resolve any blocking findings before merge
