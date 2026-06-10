# Voxbento

Voxbento is a real-time interpretation platform for live events. It provides a browser-first, zero-install experience for simultaneous interpreters, allowing them to monitor the main floor video via Jitsi and broadcast translated audio to attendees with low latency.

Interpreters stream live audio via WebRTC/WHIP → MediaMTX → WHEP (WebRTC playback).
Booth coordination (who is active, relay handoff, chat) runs over WebSocket.

---

## How it works

```
Interpreter browser
  │  iframe → Jitsi Meet (Monitor conference floor video/audio)
  │  mic → RTCPeerConnection → WHIP POST
  ▼
MediaMTX :8889 (WHIP ingest + WHEP)   Python is never in the audio path
  │  WebRTC termination + remux
  └──► WHEP :8889 ←── attendees connect via WebRTC (sub-second latency)

Interpreter / Coordinator browser
  │  WebSocket /ws/booth/{booth_id}
  ▼
FastAPI portal :8000 (coordination, state, JWT, REST)
```


---

## Setup

### Prerequisites

| Dependency | Version | Purpose |
|-----------|---------|---------|
| Python | 3.13+ | FastAPI portal |
| [uv](https://github.com/astral-sh/uv) | latest | Python package manager |
| [MediaMTX](https://github.com/bluenviron/mediamtx/releases) | 1.x | WebRTC/HLS audio server |
| Docker & Docker Compose | latest | Jitsi stack (or full Docker setup) |

### Option 1 — Docker Compose (everything in containers)

All services (portal, MediaMTX, Jitsi) start with one command:

```bash
git clone https://github.com/fossasia/voxbento.git
cd voxbento

# Configure environment
cp .env.example .env

# Required: set your admin password
echo 'ADMIN_PASSWORD=my-secure-admin-pass' >> .env

# Required for Jitsi video: set your machine's LAN IP
# macOS:  ipconfig getifaddr en0
# Linux:  hostname -I | awk '{print $1}'
echo 'DOCKER_HOST_ADDRESS=192.168.1.x' >> .env

docker compose up --build
```

Open http://localhost:8000 — all services are running.

For detailed API documentation, environment variables, port mappings, and native development setup, please see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## Upgrade Notes

### API Key Encryption & Rotation
A mandatory environment variable `API_KEY_ENCRYPTION_KEY` securely encrypts third-party API keys in the database. 
- You must generate a secure key (e.g., using `openssl rand -hex 32`) and add it to your `.env` file before starting the application. 
- **Rotation**: To rotate keys without breaking existing database entries, provide a comma-separated list of keys. Voxbento will encrypt new tokens using the *first* key, but will use *all* keys to attempt decryption.

### Optional NVIDIA Transcription
NVIDIA Riva support is now an optional dependency to reduce the default installation footprint. If you intend to run NVIDIA transcription models, you must explicitly install the optional package:
```bash
uv pip install -e .[nvidia]
```
