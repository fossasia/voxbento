# Voxbento

Voxbento is a real-time interpretation platform for live events. It provides a browser-first, zero-install experience for simultaneous interpreters, allowing them to monitor the main floor video via Jitsi and broadcast translated audio to attendees with low latency.

**Official Documentation:** [docs.voxbento.com](https://docs.voxbento.com)

Interpreters stream live audio via WebRTC/WHIP → MediaMTX → WHEP (WebRTC playback).
Booth coordination (who is active, relay handoff, chat) runs over WebSocket.

Rooms can optionally add a listener-side audio synchronization delay for WHEP playback. The default is `0` ms, which keeps the existing low-latency HTML audio path unchanged. Organizers can set values such as `1000`, `2000`, `5000`, or `8000` ms when a room's livestream video, captions, embedded player, or other external media is delayed and translated audio needs to line up with it. The delay is applied in the listener browser only; MediaMTX, WHIP, WHEP, and RTP packets are not changed. Within the organizer workspace, organizers can manage and search rooms by name within each event using server-side, case-insensitive filtering.

## Management dashboards

- Event owners manage event configuration at `/workspace/`.
- Room coordinators use `/mission-control/`, which limits them to their assigned rooms.
- System administrators use `/admin/` for instance-wide administration.
- Existing organizer bookmarks under `/admin/events/...` redirect to the matching `/workspace/events/...` URL. HTTP methods and query strings are preserved, so old forms and filtered room-list bookmarks continue to work.

The workspace and admin surfaces share the same management implementation and authorization checks. This URL separation does not change OAuth authorization, token refresh, reconnect, or disconnect behavior for Eventyay integrations.

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

# Required for Jitsi video: set the IP JVB advertises to browsers
# macOS:  ipconfig getifaddr en0
# Linux:  hostname -I | awk '{print $1}'
echo 'JVB_ADVERTISE_IPS=192.168.1.x' >> .env

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
Authentication routes (`POST /register`, `POST /login`, `POST /admin/login`) are protected with rate limiting via SlowAPI:
- `RATE_LIMIT_ENABLED`: Master toggle for rate limiting (`true` by default; set to `false` for testing or automated benchmarking).
- `RATE_LIMIT_REGISTER`: Limit for registration attempts per IP (default: `5/minute`).
- `RATE_LIMIT_LOGIN`: Limit for user login attempts per IP (default: `10/minute`).
- `RATE_LIMIT_LOGIN_ACCOUNT`: Limit for user login attempts per account/email (default: `5/minute`). Account identifiers are normalized (trimmed, lowercased) and hashed with HMAC-SHA256 using the application secret key so raw email addresses are never stored in rate-limit keys or exposed in memory dumps.
- `RATE_LIMIT_ADMIN_LOGIN`: Limit for admin login attempts per IP (default: `5/minute`).

#### Process-Local Storage and Deployment Notice
The default rate limiter uses an in-process memory backend (`MemoryStorage`). Consequently:
- Rate limits are **process-local**; counters are stored in memory within each running process.
- Multiple Uvicorn worker processes (`--workers > 1`) or multiple container replicas/instances do **not** share counters.
- Under a multi-worker or multi-replica setup, the effective request limit across the fleet is higher than configured (multiplied by the number of active worker processes).
- Production deployments requiring strict global rate enforcement should configure an external shared backend (such as Redis) or operate with a single worker per instance behind an upstream rate-limiting reverse proxy until shared storage is supported.

When deployed behind a reverse proxy (such as Caddy or Nginx):
- The bundled `Caddyfile` proxies requests to the portal and forwards the client IP (`X-Forwarded-For`).
- In the shipped Docker topology, host Caddy connects across the deterministic Docker bridge gateway (`172.28.0.1`).
- The ASGI server (Uvicorn) is configured with `FORWARDED_ALLOW_IPS=127.0.0.1,172.28.0.1` (`--forwarded-allow-ips`), trusting forwarded headers strictly from the Docker bridge gateway and localhost.
- Port 8000 is published on the host, so unrestricted wildcards (`*`) must **never** be used; untrusted direct connections to port 8000 cannot spoof `X-Forwarded-For` because their source IP is rejected by Uvicorn's proxy headers middleware.


---

## VoxBento Local (Desktop App)

VoxBento Local is our sovereign, 100% on-device AI meeting intelligence and interpretation desktop console available for macOS (Apple Silicon & Intel), Windows, and Linux.

- **Download**: Visit [`/local`](https://voxbento.org/local) for platform-detected desktop packages (.dmg, .exe, .deb, .AppImage).
- **Source Code & Releases**: Built at [github.com/ArnavBallinCode/voxa](https://github.com/ArnavBallinCode/voxa).
- **Air-Gapped & Sovereign**: Whisper live transcription and Qwen 3.5 structured meeting minutes run completely on-device via Apple Metal and NVIDIA CUDA acceleration.
