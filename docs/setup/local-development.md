# Local Development Setup

This guide covers everything needed to run the interpretation portal locally, including troubleshooting for common environment issues.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| `uv` | 0.9+ | Python package manager. [Install guide](https://docs.astral.sh/uv/getting-started/installation/) |
| Python | 3.13.x | Managed by `uv`; do not use system Python or Conda |
| Node.js | 18+ | For the Vue frontend build |
| npm | 9+ | Comes with Node.js |
| A headset | — | Required for testing mic ingest without echo |

A system `ffmpeg` binary is useful for inspecting HLS output manually but is **not required** for dependency installation or running the backend. aiortc uses PyAV (prebuilt wheel) which bundles its own FFmpeg libraries.

---

## Backend setup

### 1. Clone and enter the directory

```bash
git clone <repo-url>
cd eventyay-interpretation-portal
```

### 2. Copy and review the environment file

```bash
cp .env.example .env
```

Review `.env`. The defaults work for local development. Change `SECRET_KEY` if you plan to run tests with real sessions.

### 3. Install Python dependencies

```bash
uv sync --python 3.13 --dev
```

This creates `.venv/` and installs all pinned dependencies from `uv.lock`. It uses a prebuilt `av` wheel and does not require building from source.

### 4. Verify the environment

```bash
uv run python -c "import aiortc, av; print('aiortc', aiortc.__version__, 'av', av.__version__)"
```

Expected output: `aiortc 1.14.0 av 16.1.0`

### 5. Run the server

```bash
uv run python app.py
```

The server starts at `http://127.0.0.1:5000`.

### 6. Verify the health endpoint

```bash
curl http://127.0.0.1:5000/healthz
# Expected: {"aiortc_available": true, "ok": true}
```

---

## Frontend setup

### 1. Install Node dependencies

```bash
npm install
```

### 2. Run the Vite dev server

```bash
npm run dev
```

Vite starts at `http://localhost:5173`. API requests are proxied to Flask at `http://127.0.0.1:5000`.

### 3. Build for production

```bash
npm run build
```

Output goes to `static/dist/`. Flask serves this in production.

---

## Opening a booth

With both servers running:

```
http://127.0.0.1:5000/interpreter/hall-a-fr?language=French
```

Or via the Vite dev server:

```
http://localhost:5173/interpreter/demo-event/hall-a-fr
```

To test multi-user booth behaviour, open multiple tabs:
- Tab 1: `?role=interpreter` (defaults to interpreter, becomes active automatically)
- Tab 2: `?role=interpreter` (joins as backup)
- Tab 3: `?role=coordinator`

---

## Running tests

```bash
uv run pytest
```

Tests are in `tests/`. They cover:

- `test_app.py` — Flask routes and Socket.IO event handlers
- `test_booth_state.py` — `BoothRegistry` state machine

Tests run without a browser. Ingest tests are skipped if aiortc is unavailable.

To run with verbose output:

```bash
uv run pytest -v
```

---

## Environment variable reference

All variables are loaded from `.env` by `portal/config.py` via `python-dotenv`.

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` for LAN access. |
| `PORT` | `5000` | Flask/Socket.IO port. |
| `FLASK_DEBUG` | `1` | Flask debug mode (auto-reload, error pages). |
| `SECRET_KEY` | `change-me` | Flask session signing key. **Change in production.** |
| `BOOTH_ACCESS_TOKEN` | _(empty)_ | If set, all API calls and Socket.IO events must include this token. |
| `BOOTH_WS_CORS_ORIGINS` | `*` | Allowed Socket.IO origins. Set to your origin in production. |
| `DEFAULT_JITSI_ROOM` | `https://meet.jit.si/eventyay-stage-room` | Pre-filled Jitsi URL. |
| `JITSI_DOMAIN` | `meet.jit.si` | Domain validation for Jitsi embed URLs. |
| `INGEST_HLS_ROOT` | `./hls-output` | Directory for FFmpeg HLS output. Created automatically. |
| `HLS_SEGMENT_SECONDS` | `2` | HLS segment duration. |
| `HLS_PLAYLIST_LENGTH` | `8` | Live playlist segment count. |

---

## Troubleshooting

### `uv sync` resolves to Python 3.14

```bash
uv sync --python 3.13 --dev
```

Confirm `.python-version` is present in the repo root and contains `3.13`.

### `av` tries to compile from source

1. Delete `.venv` and retry: `rm -rf .venv && uv sync --python 3.13 --dev`
2. Confirm you are on Python 3.13, not 3.14.
3. Do not modify `uv.lock` without revalidating the media stack.

### `aiortc_available` is `False`

```bash
uv run python -c "import aiortc, av; print(aiortc.__version__, av.__version__)"
```

If this fails with `ImportError`, the virtual environment is broken. Delete `.venv` and re-run `uv sync`.

### Browser mic test shows no level

1. Check that the browser has microphone permissions for `localhost` / `127.0.0.1`.
2. Confirm the correct device is selected in the device dropdown.
3. Check the browser console for `getUserMedia` errors.

### Go Live button is disabled

All four preflight items must be checked:
1. Headphones: manually click the checkbox.
2. Monitoring: the Jitsi iframe must have loaded (click **Join Monitor Room** first).
3. Mic test: click **Test Mic** and confirm level.
4. Ingest reachable: the server must be running at the configured `VITE_INGEST_BASE_URL`.

### WebRTC connection fails

1. On `localhost`, STUN is not required — ICE host candidates should suffice.
2. Inspect `/api/interpreter/status/{channel_id}` for the server-side session state.
3. Check the browser console for `RTCPeerConnection` errors.
4. If on a corporate network with strict firewall rules, a TURN server may be needed. Configure `VITE_STUN_SERVERS` with your TURN server.

### HLS output not appearing

1. Confirm `INGEST_HLS_ROOT` directory exists or is writable.
2. Check that aiortc successfully started the recorder — look for errors in the Flask console.
3. The recorder starts only after a WebRTC connection is established and the first audio track arrives.

### Flask restarts wipe booth state

This is expected. Booth state is in-memory. Restart Flask and all browser tabs will need to rejoin. In production, add PostgreSQL persistence.

---

## macOS notes

- Install `uv` via Homebrew: `brew install uv` or via the official installer.
- If you have multiple Python installations (pyenv, Conda, system), `uv` manages its own Python. Do not use `python3` directly.
- Homebrew `ffmpeg` is useful for inspecting HLS playlists but is not required for development.

## Linux notes

- `uv sync --python 3.13 --dev` should install the locked wheel set on x86_64 and aarch64 Linux.
- If `uv` falls back to building `av` from source, treat this as an environment problem. Confirm you are using the locked Python version.
