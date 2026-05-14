# Development Environment

This project uses `uv` for Python installation, dependency resolution, locking, and virtual environment management.

## Tested baseline

The current lockfile and instructions were validated on:

- macOS 15 / Apple Silicon
- `uv 0.9.24`
- CPython `3.13.5`
- `aiortc 1.14.0`
- `av 16.1.0`
- Homebrew `ffmpeg@7 7.1.4` present on the machine

Important:

- The Python dependency stack no longer builds PyAV against your system FFmpeg during normal setup.
- `uv sync` installs a compatible prebuilt `av` wheel on Apple Silicon, which avoids the older FFmpeg 8 API breakage that affected `av 11`.
- A system `ffmpeg` binary is still useful for inspecting generated HLS playlists manually, but it is not required for dependency installation.

## Bootstrap from scratch

```bash
uv sync --python 3.13 --dev
```

Run the app:

```bash
uv run python app.py
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
uv run python -c "import app; print(app.AIORTC_AVAILABLE)"
uv run python app.py
```

Then open `http://127.0.0.1:5000/healthz`.

## Runtime notes

- Default bind host is `127.0.0.1`.
- Set `HOST=0.0.0.0` only when you intentionally need LAN access.
- Booth state remains in memory in the current prototype.
- HLS output is written under `INGEST_HLS_ROOT`.

## macOS notes

- If `uv` is missing, install it with the official installer or Homebrew.
- If you have multiple Python installations, prefer the interpreter selected by `uv`, not the system or Conda default.
- If you previously created a broken `.venv` with Python 3.14 or older `aiortc` / `av` pins, remove it before syncing:

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

`aiortc_available` is `False`:

- Run `uv run python -c "import aiortc, av; print(aiortc.__version__, av.__version__)"`.
- If imports fail, recreate the environment with `uv sync --python 3.13 --dev`.

The app starts but browser ingest fails:

- Confirm microphone permissions are granted.
- Confirm the active interpreter owns the booth before clicking `Go Live`.
- Inspect `/healthz` and `/api/interpreter/status/<channel>`.
