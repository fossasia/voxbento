# Development Environment

This project uses `uv` for Python installation, dependency resolution, locking, and virtual environment management.

## Tested baseline

The current lockfile and instructions were validated on:

- macOS 15 / Apple Silicon
- `uv 0.9.24`
- CPython `3.13.5`
- Docker Desktop for MediaMTX and Jitsi services

Important:

- The project no longer uses aiortc or PyAV. Audio ingest is handled entirely by MediaMTX via WHIP.
- A system `ffmpeg` binary is not required.

## Bootstrap from scratch

```bash
uv sync --python 3.13 --dev
```

Run the app:

```bash
uv run uvicorn fastapi_app:app --reload
```

Or with Docker Compose (recommended — starts all services including MediaMTX and Jitsi):

```bash
docker compose up
```

Run tests:

```bash
uv run pytest
```

## Fresh clone verification flow

From a clean checkout:

```bash
uv sync --python 3.13 --dev
uv run pytest
uv run uvicorn fastapi_app:app --reload
```

Then open `http://127.0.0.1:8000/healthz`.

## Runtime notes

- Default bind host is `127.0.0.1`, port `8000`.
- Set `HOST=0.0.0.0` only when you intentionally need LAN access.
- Booth state remains in memory in the current prototype.
- HLS output is served by MediaMTX at `:8888` (not by the portal).

## macOS notes

- If `uv` is missing, install it with the official installer or Homebrew.
- If you have multiple Python installations, prefer the interpreter selected by `uv`, not the system or Conda default.
- If you previously created a broken `.venv` with Python 3.14, remove it before syncing:

```bash
rm -rf .venv
uv sync --python 3.13 --dev
```

## Linux notes

- `uv sync --python 3.13 --dev` should install the locked wheel set on mainstream x86_64 and aarch64 Linux environments.
- If `uv` falls back to building `av` from source, treat that as an environment problem. Do not patch compiler flags locally; first confirm you are using the locked Python version and current `uv.lock`.

## Troubleshooting

`uv sync` resolves to Python 3.14:

- Run `uv sync --python 3.13 --dev`.
- Confirm `.python-version` is present.

`av` tries to compile from source:

- Delete `.venv` and retry with the locked interpreter.
- Confirm you are on Python 3.13, not 3.14.
- Confirm `uv lock` has not been modified without revalidation.

WHIP/HLS ingest not working:

- Ensure MediaMTX is running (`docker compose up mediamtx`).
- Confirm WHIP endpoint is reachable at `:8889`.
- Confirm HLS endpoint is reachable at `:8888`.

The app starts but browser ingest fails:

- Confirm microphone permissions are granted.
- Confirm the active interpreter owns the booth before clicking `Go Live`.
- Inspect `/healthz`.
