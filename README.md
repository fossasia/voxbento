# Voxbento

Voxbento is a real-time interpretation platform for live events. It provides a browser-first, zero-install experience for simultaneous interpreters, allowing them to monitor the main floor video via Jitsi and broadcast translated audio to attendees with low latency.

**Official Documentation:** [docs.voxbento.com](https://docs.voxbento.com)

Interpreters stream live audio via WebRTC/WHIP → MediaMTX → WHEP (WebRTC playback).
Booth coordination (who is active, relay handoff, chat) runs over WebSocket.

Rooms can optionally add a listener-side audio synchronization delay for WHEP playback. The default is `0` ms, which keeps the existing low-latency HTML audio path unchanged. Organizers can set values such as `1000`, `2000`, `5000`, or `8000` ms when a room's livestream video, captions, embedded player, or other external media is delayed and translated audio needs to line up with it. The delay is applied in the listener browser only; MediaMTX, WHIP, WHEP, and RTP packets are not changed. Within the admin console, organizers can manage and search rooms by name within each event using server-side, case-insensitive filtering.

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
  │
  ├──► Background Transcription (ffmpeg → Deepgram/OpenAI/Local)
  └──► Background Translation (Groq/Anthropic/Gemini)

Floor Audio Bot (floor-bot)
  │  Headless Chromium → Joins Jitsi Meeting
  └──► ffmpeg → RTSP → MediaMTX → Transcription/Translation
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

# Required: set your admin password (or generate a secure random password)
echo "ADMIN_PASSWORD=$(openssl rand -hex 16)" >> .env

# Required for API key encryption: set your encryption key (must be 32 characters or longer)
echo "API_KEY_ENCRYPTION_KEY=$(openssl rand -hex 32)" >> .env

# Required for Jitsi video: set your machine's LAN IP
# macOS:  ipconfig getifaddr en0
# Linux:  hostname -I | awk '{print $1}'
echo 'DOCKER_HOST_ADDRESS=192.168.1.x' >> .env

docker compose up --build
```

Open http://localhost:8000 — all services are running.

For detailed API documentation, environment variables, and configuration, visit [docs.voxbento.com](https://docs.voxbento.com). Native development setup details can also be found in [CONTRIBUTING.md](CONTRIBUTING.md).

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

### Authentication Rate Limiting
Authentication routes (`POST /register`, `POST /login`, `POST /admin/login`) are protected with IP-based rate limiting via SlowAPI:
- `RATE_LIMIT_ENABLED`: Master toggle for rate limiting (`true` by default; set to `false` for testing or automated benchmarking).
- `RATE_LIMIT_REGISTER`: Limit for registration attempts per IP (default: `5/minute`).
- `RATE_LIMIT_LOGIN`: Limit for user login attempts per IP (default: `10/minute`).
- `RATE_LIMIT_ADMIN_LOGIN`: Limit for admin login attempts per IP (default: `5/minute`).

When deployed behind a reverse proxy (such as Caddy or Nginx):
- The bundled `Caddyfile` sets `header_up X-Forwarded-For {remote_host}` to forward the client IP.
- The ASGI server (Uvicorn) must be configured at the deployment boundary to trust ONLY the specific reverse-proxy IP(s) (e.g., via `--forwarded-allow-ips 127.0.0.1` or the specific reverse proxy container/host IP) so `request.client.host` is safely populated. Do not use an unrestricted wildcard (`*`) to prevent forwarded-header spoofing.


