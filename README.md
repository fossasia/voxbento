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

Floor source (one per room, chosen in the room settings)
  ├─ Jitsi floor bot: headless Chromium joins the Jitsi meeting → ffmpeg → RTSP
  └─ Program Stream Ingest: organizer encoder (OBS…) → authenticated WHIP
        ▼
  MediaMTX {event_slug}/{room_id}/floor → Transcription → Translation → TTS
```

### Program Stream Ingest

Produced events can send the **same program feed they stream to YouTube** to Voxbento as a second destination, instead of relying on the Jitsi floor bot. Voxbento uses its copy for floor captions, translated captions, and translated TTS. It never touches the YouTube player or needs the YouTube stream key, and YouTube keeps broadcasting if Voxbento stops.

1. In the room page, configure Floor Audio Transcription (plus translation/TTS if wanted).
2. Under **Floor Source & Program Stream Ingest**, choose **Program Stream Ingest**, then **Generate secret** (shown once).
3. In OBS 30+, set **Settings → Stream → Service: WHIP**, with the shown ingest server URL and the secret as the **Bearer Token**. Keep YouTube as a second output, for example with the Multiple RTMP outputs plugin.
4. The room card shows waiting → processing, codecs, connect/disconnect times and warnings. Rotate or revoke the secret at any time; the encoder is disconnected immediately.

MediaMTX now authorizes every publish through the portal (`/internal/mediamtx/auth`). `MEDIAMTX_AUTH_HOOK_SECRET` is required in `.env` when `DEBUG=false`; the portal refuses to start without it. Room floor paths accept only their configured source. The MediaMTX Control API (9997) and RTSP (8554) ports are bound to `127.0.0.1`. See the [Program Stream Ingest guide](website/docs/admin/program-stream-ingest.mdx) for encoder setup, sync-offset calibration, capacity, and troubleshooting.


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

# Required (outside DEBUG mode): shared secret so only MediaMTX can call the portal's publish auth hook
echo "MEDIAMTX_AUTH_HOOK_SECRET=$(openssl rand -hex 32)" >> .env

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

---

## VoxBento Local (Desktop App)

VoxBento Local is our sovereign, 100% on-device AI meeting intelligence and interpretation desktop console available for macOS (Apple Silicon & Intel), Windows, and Linux.

- **Download**: Visit [`/local`](https://voxbento.org/local) for platform-detected desktop packages (.dmg, .exe, .deb, .AppImage).
- **Source Code & Releases**: Built at [github.com/ArnavBallinCode/voxa](https://github.com/ArnavBallinCode/voxa).
- **Air-Gapped & Sovereign**: Whisper live transcription and Qwen 3.5 structured meeting minutes run completely on-device via Apple Metal and NVIDIA CUDA acceleration.
