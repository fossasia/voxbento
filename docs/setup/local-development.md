# Local Development Setup

This guide covers everything needed to run the interpretation portal locally, including troubleshooting for common environment issues.

---

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| `uv` | 0.9+ | Python package manager. [Install guide](https://docs.astral.sh/uv/getting-started/installation/) |
| Python | 3.12.x | Managed by `uv`; do not use system Python or Conda |
| Docker | 24+ | Required for MediaMTX and Jitsi services |
| Docker Compose | v2+ | Comes with Docker Desktop |
| A headset | — | Required for testing mic ingest without echo |

No Node.js or npm is required. The frontend uses plain ES modules served by FastAPI.

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

Review `.env`. The defaults work for local development.

### 3. Install Python dependencies

```bash
uv sync --python 3.12 --dev
```

This creates `.venv/` and installs all pinned dependencies from `uv.lock`.

### 4. Run the FastAPI server

```bash
uv run uvicorn fastapi_app:app --reload --host 127.0.0.1 --port 8000
```

The server starts at `http://127.0.0.1:8000`.

### 5. Verify the health endpoint

```bash
curl http://127.0.0.1:8000/healthz
```

---

## Docker services setup

### Start MediaMTX and Jitsi

```bash
docker compose up -d
```

This starts all required services:

| Service | Port | Purpose |
|---|---|---|
| `portal` | :8000 | FastAPI application |
| `mediamtx` | :8889 (WHIP), :8888 (HLS) | Audio ingest and delivery |
| `jitsi-web` | :8443 | Self-hosted Jitsi frontend |
| `jitsi-prosody` | Internal | XMPP server |
| `jitsi-jicofo` | Internal | Conference focus |
| `jitsi-jvb` | :10000/udp | Video bridge |

### Verify MediaMTX

```bash
curl http://localhost:8888
```

### Verify Jitsi

Open `https://localhost:8443` in a browser (accept the self-signed certificate for local development).

---

## Opening a booth

With the server running:

```
http://127.0.0.1:8000/interpreter/hall-a-fr?language=French
```

To test multi-user booth behaviour, open multiple tabs:
- Tab 1: `?role=interpreter` (defaults to interpreter, becomes active automatically)
- Tab 2: `?role=interpreter` (joins as backup)
- Tab 3: `?role=coordinator`

### Listener page

```
http://127.0.0.1:8000/listen/hall-a-fr
```

This page plays the HLS stream from MediaMTX using hls.js with auto-recovery.

---

## Running tests

```bash
uv run pytest
```

Tests are in `tests/`. There are 27 tests covering:

- FastAPI routes and WebSocket handlers
- `BoothRegistry` state machine
- JWT authentication

Tests use `httpx` and `anyio`. They run without a browser or Docker services.

To run with verbose output:

```bash
uv run pytest -v
```

---

## Environment variable reference

All variables are loaded from `.env` by `portal/config.py` via pydantic-settings.

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` for LAN access. |
| `PORT` | `8000` | FastAPI/uvicorn port. |
| `DEBUG` | `true` | Debug mode (auto-reload, error pages). |
| `JWT_SECRET` | `change-me` | JWT signing secret. **Change in production.** |
| `BOOTH_ACCESS_TOKEN` | _(empty)_ | If set, all API calls and WebSocket connections must include this token. |
| `DEFAULT_JITSI_ROOM` | — | Pre-filled Jitsi URL (points to self-hosted instance). |
| `JITSI_DOMAIN` | — | Domain validation for Jitsi embed URLs. |
| `MEDIAMTX_WHIP_URL` | `http://localhost:8889` | MediaMTX WHIP endpoint for browser ingest. |
| `MEDIAMTX_HLS_URL` | `http://localhost:8888` | MediaMTX HLS endpoint for listener playback. |

---

## Troubleshooting

### `uv sync` resolves to wrong Python version

```bash
uv sync --python 3.12 --dev
```

Confirm `.python-version` is present in the repo root.

### Browser mic test shows no level

1. Check that the browser has microphone permissions for `localhost` / `127.0.0.1`.
2. Confirm the correct device is selected in the device dropdown.
3. Check the browser console for `getUserMedia` errors.

### Go Live button is disabled

All preflight items must be checked:
1. Headphones: manually click the checkbox.
2. Monitoring: the Jitsi iframe must have loaded (click **Join Monitor Room** first).
3. Mic test: click **Test Mic** and confirm level.

### WHIP connection fails

1. Confirm MediaMTX is running: `docker compose ps mediamtx`.
2. Confirm the WHIP endpoint is reachable: `curl -v http://localhost:8889`.
3. On `localhost`, STUN is not required — ICE host candidates should suffice.
4. Check the browser console for `RTCPeerConnection` errors.

### HLS output not appearing on listener page

1. Confirm MediaMTX is running and healthy.
2. Confirm a WHIP session is active (an interpreter has clicked Go Live).
3. Check `http://localhost:8888/{channel_id}/playlist.m3u8` directly.

### Jitsi not loading

1. Confirm Jitsi containers are running: `docker compose ps`.
2. Open `https://localhost:8443` directly and accept the self-signed certificate.
3. Check that `JITSI_DOMAIN` in `.env` matches the Jitsi hostname.

### Server restarts wipe booth state

This is expected. Booth state is in-memory. Restart the server and all browser tabs will need to rejoin. In production, add PostgreSQL persistence.

---

## macOS notes

- Install `uv` via Homebrew: `brew install uv` or via the official installer.
- If you have multiple Python installations (pyenv, Conda, system), `uv` manages its own Python. Do not use `python3` directly.
- Docker Desktop for Mac is required for running MediaMTX and Jitsi containers.

## Linux notes

- `uv sync --python 3.12 --dev` should install the locked wheel set on x86_64 and aarch64 Linux.
- Docker and Docker Compose v2 are required for the media and Jitsi services.
