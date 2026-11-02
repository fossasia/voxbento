<p align="center">
  <img src="https://github.com/user-attachments/assets/4521a23a-f99c-445d-96ee-dd98a4b66d5d" alt="VoxBento" width="420" />
</p>

<p align="center">
  <b>Browser-first, real-time simultaneous interpretation for live events.</b>
</p>

<p align="center">
  <a href="https://github.com/fossasia/voxbento/actions/workflows/tests.yml"><img src="https://github.com/fossasia/voxbento/actions/workflows/tests.yml/badge.svg" alt="CI Status"></a>
  <a href="https://www.python.org/downloads/release/python-3130/"><img src="https://img.shields.io/badge/python-3.13-3776ab.svg?style=flat&logo=python&logoColor=white" alt="Python 3.13"></a>
  <a href="https://voxbento.com"><img src="https://img.shields.io/badge/live-voxbento.com-2ea44f" alt="Live Demo"></a>
  <a href="https://docs.voxbento.com"><img src="https://img.shields.io/badge/docs-docs.voxbento.com-1f6feb" alt="Documentation"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-2ea44f.svg" alt="License: Apache 2.0"></a>
</p>

<p align="center">
  <a href="#-about-voxbento">About</a> •
  <a href="#-key-features">Features</a> •
  <a href="#-architecture">Architecture</a> •
  <a href="#-quick-start">Quick Start</a> •
  <a href="#-testing--code-quality">Testing</a> •
  <a href="#-project-structure">Project Structure</a> •
  <a href="#-contributing">Contributing</a> •
  <a href="https://docs.voxbento.com">Docs</a>
</p>

---

## 🎧 About VoxBento

**VoxBento** is a real-time, browser-first simultaneous interpretation and audio broadcast platform built for live conferences and hybrid events. 

Interpreters monitor the floor session and broadcast high-fidelity translation directly from their browser using WebRTC with sub-second latency. Attendees receive translated audio on any device through lightweight WebRTC playback, while optional AI pipelines provide live transcription, contextual translation, and synthetic speech concurrently.

---

## ✨ Key Features

- **🎙️ Browser-First Interpreter Booth:** Zero software installation. Interpreters join, test microphones, go live, and coordinate shift handoffs smoothly via WebRTC (WHIP).
- **⚡ Sub-Second Listener Playback:** Attendees receive ultra-low latency audio via WebRTC (WHEP) powered by MediaMTX, scaling effortlessly to thousands of listeners.
- **🤖 Live AI Transcription & Translation:** Multi-provider speech-to-text (Faster-Whisper, OpenAI, Deepgram, NVIDIA Parakeet, ElevenLabs) paired with real-time LLM translations guided by custom personas, styles, and domain vocabulary glossaries.
- **🗣️ Direct-Push Text-to-Speech (TTS):** Synthesize live translated speech on the fly (Deepgram Aura or self-hosted Supertonic ONNX), streamed directly to attendees over binary WebSocket frames.
- **📡 Program Stream Ingest & YouTube Relay:** Send conference floor audio from an encoder or browser, with optional RTMPS forwarding to YouTube.
- **🎛️ Mission Control & Coordination:** Real-time booth status monitoring, live audio meters, in-booth team chat, and coordinator controls via FastAPI WebSockets.
- **💻 VoxBento Local:** Sovereign, 100% offline desktop application for private AI meeting intelligence and minutes on macOS, Windows, and Linux.

---

## 🏗️ Architecture

VoxBento isolates the real-time media plane from application coordination. Media streams flow directly through **MediaMTX**, ensuring that Python never processes or relays live interpreter microphone data.

```
                         ┌─────────────────────────────────────────────────────────┐
                         │                    MediaMTX Server                      │
                         │                                                         │
[ Interpreter Mic ] ────►│  /event/room/lang/whip (WHIP Ingest)                    ├────► [ Attendees (WHEP) ]
                         │                                                         │
[ Floor Audio / OBS ] ──►│  /event/room/floor (WHIP / RTMPS Ingest)                ├────► [ Optional YouTube Relay ]
                         └───────────────────────────┬─────────────────────────────┘
                                                     │ RTSP (8554)
                                                     ▼
                                         ┌───────────────────────┐
                                         │  AI Pipeline Subsystem│
                                         │  (STT → LLM → TTS)    │
                                         └───────────┬───────────┘
                                                     │ WebSocket
                                                     ▼
                                         [ Live Captions & Speech ]

───────────────────────────────────────────────────────────────────────────────────────────
   Control Plane: FastAPI + WebSockets + SQLite/PostgreSQL (Booth State, Auth, Coordination)
```

---

## 🚀 Quick Start

Get VoxBento up and running locally in a few simple steps.

### Prerequisites

- [Python 3.13+](https://www.python.org/downloads/)
- [uv](https://github.com/astral-sh/uv) (Extremely fast Python package manager)
- [Docker](https://www.docker.com/) & Docker Compose
- [Node.js](https://nodejs.org/) (for client-side syntax checks)

### 1. Clone the Repository

```bash
git clone https://github.com/fossasia/voxbento.git
cd voxbento
```

### 2. Install Dependencies

```bash
uv sync --all-groups
```

### 3. Configure Environment

For **NEW local setups only**, copy the example configuration and generate separate strong secrets. Do not overwrite an existing `.env` or rotate existing keys with this command; it refuses to replace an existing file. The generated file is owner-readable only.

The encryption key protects stored provider keys and queued email.

```bash
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
```

Defaults keep debug disabled (`DEBUG=false`) and startup secret checks active. Developers may explicitly set `DEBUG=true` in their own `.env` for local-only debugging; never enable it for deployments.

For production, configure `BOOTH_ACCESS_TOKEN` and `MEDIAMTX_AUTH_HOOK_SECRET` with separate strong values, for example from `openssl rand -hex 32`. Startup rejects a missing or weak `BOOTH_ACCESS_TOKEN` and a missing `MEDIAMTX_AUTH_HOOK_SECRET`; it does not measure the hook secret's strength, so keep it strong and private.

For self-hosted domains, set `PUBLIC_BASE_URL` in `.env` to your public URL
for email links and canonical URLs. See the
[deployment guide](website/docs/configuration/deployment.mdx) for production setup
and [registration protection](docs/registration-protection.md) for signup controls.

### 4. Run the Stack

**Option A: Using Docker (Recommended for full stack)**
```bash
docker compose up -d --build
```

**Option B: Native Development (Faster backend iteration)**
```bash
# Run database migrations
uv run alembic upgrade head

# Start the FastAPI application with live reload
uv run uvicorn fastapi_app:app --reload --port 8000
```

Once running, access the application at **`http://localhost:8000`**.

For event and room setup, see the [events and rooms guide](website/docs/admin/events-and-rooms.mdx).
For pipeline details and advanced configuration, see [how it works](website/docs/how-it-works.mdx).

---

## 🧪 Testing & Code Quality

Before submitting changes, make sure all tests and linter checks pass:

```bash
# Run the test suite
uv run pytest tests/ -v

# Run Python linter and formatter checks
uv run ruff check .
uv run ruff format --check .

# Run JavaScript syntax checks
node --check portal/static/js/interpreter-booth.js
node --check portal/static/js/whep-listener.js
node --check portal/static/js/admin.js
```

---

## 📁 Project Structure

Here is a quick overview of how the codebase is organized:

```text
voxbento/
├── fastapi_app.py          # FastAPI application lifespan & router aggregation
├── portal/
│   ├── routers/            # HTTP endpoints (admin, workspace, API, auth)
│   ├── websockets/         # WebSocket handlers & real-time connection managers
│   ├── transcription/      # STT providers (Whisper, Deepgram, NVIDIA, ElevenLabs)
│   ├── translations/       # Real-time LLM translation & vocabulary glossaries
│   ├── tts/                # Text-to-speech providers (Deepgram Aura & Supertonic)
│   ├── models.py           # SQLAlchemy database models
│   ├── database.py         # Async database session & CRUD helpers
│   ├── templates/          # Jinja2 HTML templates
│   └── static/js/          # Vanilla ES module frontend (zero build steps)
├── mediamtx.yml            # MediaMTX WebRTC (WHIP/WHEP) & RTSP streaming config
├── docker-compose.yml      # Container orchestration (portal, mediamtx, jitsi)
└── tests/                  # Comprehensive pytest test suite
```

---

## 💻 VoxBento Local (Desktop App)

Looking for an on-device, sovereign AI meeting intelligence tool? 

**VoxBento Local** runs 100% locally on your computer with zero cloud dependencies:
- **Download**: Visit [`/local`](https://voxbento.com/local) for macOS, Windows, and Linux packages.
- **Air-Gapped & Sovereign**: Live speech transcription and meeting minutes powered by local Whisper and Qwen models accelerated by Apple Metal and NVIDIA CUDA.
- **Source Code**: Developed at [github.com/ArnavBallinCode/voxa](https://github.com/ArnavBallinCode/voxa).

---

## 🤝 Contributing

We love contributions! Whether you are fixing a bug, adding new transcription providers, improving the documentation, or polishing the UI, you are very welcome here.

1. **Find an issue:** Check our [Issue Tracker](https://github.com/fossasia/voxbento/issues) for issues labeled [`good first issue`](https://github.com/fossasia/voxbento/labels/good%20first%20issue) or [`help wanted`](https://github.com/fossasia/voxbento/labels/help%20wanted).
2. **Read the guide:** Check [CONTRIBUTING.md](CONTRIBUTING.md) for full development workflows and conventions.
3. **Branch convention:** Create a branch for your work (`feat/your-feature` or `fix/your-fix`).
4. **Keep it atomic:** Keep commits focused, write tests for new functionality, and open a Pull Request against `dev`.

---

## 📄 License

VoxBento is open-source software licensed under the [Apache License 2.0](LICENSE).
