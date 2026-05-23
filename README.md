# Eventyay Interpretation Portal

The Eventyay Interpretation Portal is a browser-based, interpreter-centric console that lets human interpreters broadcast simultaneous interpretation at live events — no OBS, no hardware encoder, no RTMP knowledge required.

It is built as an Eventyay-native subsystem. The portal feeds the Eventyay viewer playback surface with language-specific audio; it does not replace it.

---

## What this repository is

This repository contains:

- **The interpreter console** — a single-tab Vue 3 application where an interpreter monitors the floor session (via an embedded Jitsi frame) and broadcasts their spoken translation (via browser WebRTC mic capture → ingest API).
- **The booth coordination layer** — a Flask + Socket.IO server that tracks participant roles (active interpreter, backup, coordinator, listener), enforces single-publisher-per-channel rules, handles handoffs, and runs internal booth chat.
- **The ingest bridge** — an `aiortc`-powered endpoint that accepts WebRTC SDP offers from the interpreter's browser, terminates the audio connection server-side, and hands audio to FFmpeg for HLS segmentation.

The viewer-side playback (YouTube video clock + drift-corrected HLS audio) lives in the upstream Eventyay video module and is not part of this repository.

---

## What interpreter booths are

An **interpreter booth** is the virtual equivalent of a soundproofed glass booth at a UN-style conference. Each booth is scoped to one language channel (e.g., `hall-a-fr` for French interpretation of hall A).

Inside a booth:

| Role | What they do |
|---|---|
| **Active Interpreter** | The only person currently broadcasting live audio to viewers. |
| **Backup Interpreter** | Ready to take over; can request or receive a handoff. |
| **Coordinator** | Supervises the booth, can reassign the active role at any time. |
| **Listener** | Observes the booth; no publishing rights. |

The organizer generates a private, tokenized URL per language per session and shares it with the booth team. The interpreter opens the URL, goes through a preflight checklist (headset check, mic test, level meter), and clicks **Go Live** when ready. No other configuration is needed.

---

## System architecture at a glance

```
Speaker/Presenter
       │
       ▼
  Jitsi meeting ──────────────────────────────────────────────┐
       │                                                       │
       │  (interpreter monitors floor audio via Jitsi iframe) │
       ▼                                                       ▼
Interpreter Console (this repo)                     Eventyay Viewer
  ┌─────────────────────────────┐                  ┌──────────────────────────┐
  │  Jitsi iframe (receive-only)│                  │  YouTube video (master   │
  │  Mic capture (WebRTC)       │                  │  clock)                  │
  │  Booth chat / participant   │                  │  HLS language audio      │
  │  grid / health panel        │                  │  (drift-corrected)       │
  └────────────┬────────────────┘                  └──────────────────────────┘
               │ WebRTC SDP offer/answer
               ▼
         Ingest API  (app.py / portal/ingest.py)
               │ aiortc terminates RTP
               ▼
           FFmpeg  →  HLS segments  →  hls-output/
```

Full diagrams: [docs/architecture/system-overview.md](docs/architecture/system-overview.md)

---

## Quick start

### Prerequisites

- Python 3.13 (managed by `uv`)
- Node.js 18+ and npm (for the Vue frontend build)
- `uv` — the Python package manager ([install guide](https://docs.astral.sh/uv/getting-started/installation/))

### Backend

```bash
cd eventyay-interpretation-portal
cp .env.example .env          # review and adjust if needed
uv sync --python 3.13 --dev
uv run python app.py
```

## MediaMTX (local media server)

MediaMTX handles WebRTC audio ingest (WHIP) and HLS output. It is the planned
production replacement for the prototype `aiortc` ingest pipeline (`portal/ingest.py`).
Both run side-by-side during migration — see the Phase 1 migration plan in
`docs/detailedplan.md` for the full removal schedule.

### Quick start (Docker Compose)

```bash
docker compose -f docker-compose.interpretation.yml up -d
```

This starts MediaMTX with:
- **WHIP ingest** on `http://localhost:8889/whip/{path}` — interpreters publish audio here
- **HLS output** on `http://localhost:8888/{path}/index.m3u8` — audience fetches streams here

### Quick start (standalone Docker)

```bash
docker run --rm -d --name mediamtx \
  -v $(pwd)/mediamtx.yml:/mediamtx.yml:ro \
  -p 8888:8888 \
  -p 8889:8889 \
  -p 8189:8189/udp \
  bluenviron/mediamtx:1
```

### Verify MediaMTX is running

```bash
# HLS endpoint — 404 is expected here (no stream path specified).
# A 404 from MediaMTX confirms the server is up and listening.
curl -s -o /dev/null -w "%{http_code}" http://localhost:8888/

# WHIP endpoint — 404 is also expected (no stream path specified).
curl -s -o /dev/null -w "%{http_code}" http://localhost:8889/

# Container logs
docker logs mediamtx
```

Both endpoints return **404 until a stream is published** — this is expected.
To confirm a live stream, check `http://localhost:8888/{path}/index.m3u8` after
a publisher connects to `http://localhost:8889/whip/{path}`.

### Configuration

The `mediamtx.yml` file in the repository root configures MediaMTX for local
development. Key settings:

| Setting | Value | Purpose |
|---|---|---|
| `webrtcAddress` | `:8889` | WHIP ingest port |
| `hlsAddress` | `:8888` | HLS output port |
| `hlsSegmentDuration` | `2s` | HLS segment length |
| `webrtcEncryption` | `no` | Disable WebRTC DTLS-SRTP media encryption for local dev. **Do not use in production.** (Distinct from HTTPS/TLS transport — this controls per-packet media encryption.) |

See [MediaMTX docs](https://github.com/bluenviron/mediamtx) for full configuration reference.

Open:

```text
http://127.0.0.1:5000/interpreter/hall-a-fr?token=dev-token&language=French
```

If `BOOTH_ACCESS_TOKEN` is empty in `.env`, the `token` parameter is optional. When a token is set, every booth URL and API call must include the matching token.

---

## Environment variables

Copy `.env.example` to `.env`. All variables have safe development defaults.

| Variable | Default | Description |
|---|---|---|
| `HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` only for LAN testing. |
| `PORT` | `5000` | Flask/Socket.IO port. |
| `FLASK_DEBUG` | `1` | Enable Flask debug mode locally. |
| `SECRET_KEY` | `change-me` | Flask session secret. Change in production. |
| `BOOTH_ACCESS_TOKEN` | _(empty)_ | Optional invite token for booth URLs. |
| `BOOTH_WS_CORS_ORIGINS` | `*` | Allowed Socket.IO origins (comma-separated or `*`). |
| `DEFAULT_JITSI_ROOM` | `https://meet.jit.si/eventyay-stage-room` | Pre-filled Jitsi monitoring URL. |
| `JITSI_DOMAIN` | `meet.jit.si` | Jitsi server domain used for embed URL validation. |
| `INGEST_HLS_ROOT` | `./hls-output` | Directory where FFmpeg writes HLS playlists and segments. |
| `HLS_SEGMENT_SECONDS` | `2` | Target segment duration in seconds. |
| `HLS_PLAYLIST_LENGTH` | `8` | Number of segments retained in the live playlist. |

---

## Workflows

### Interpreter

1. Open the tokenized booth URL.
2. Complete the preflight checklist (headphones, mic test, level meter).
3. Monitor the floor session via the embedded Jitsi frame.
4. When assigned as active interpreter, click **Go Live**.
5. Speak. Your audio is captured, sent via WebRTC, and segmented into HLS.
6. Use booth chat for coordination, terminology, or handoff signalling.
7. Click **Stop** or accept a coordinator handoff to yield the active role.

### Coordinator

1. Open the same booth URL with `role=coordinator`.
2. Watch the participant grid for active/standby/connection status.
3. Assign a different interpreter active via the participant grid.
4. Use booth chat for rotation planning and terminology.

### Viewer (upstream Eventyay surface)

1. Open an Eventyay event stage page.
2. Select a language from the audio track picker.
3. The page loads HLS language audio alongside the YouTube video clock.
4. A drift-correction loop keeps the language audio in sync with video.

---

## Running tests

```bash
uv run pytest
```

Tests cover booth state, participant role enforcement, ingest API boundaries, and Socket.IO event handlers. See [docs/testing/e2e-testing.md](docs/testing/e2e-testing.md).

---

## Documentation index

| Document | Purpose |
|---|---|
| [docs/context/project-context.md](docs/context/project-context.md) | Product background and design intent |
| [docs/architecture/system-overview.md](docs/architecture/system-overview.md) | Full system diagram and component map |
| [docs/architecture/interpreter-flow.md](docs/architecture/interpreter-flow.md) | End-to-end interpreter audio pipeline |
| [docs/architecture/jitsi-integration.md](docs/architecture/jitsi-integration.md) | How Jitsi is used for monitoring |
| [docs/architecture/webrtc-ingest.md](docs/architecture/webrtc-ingest.md) | WebRTC ingest design and SDP flow |
| [docs/frontend/frontend-architecture.md](docs/frontend/frontend-architecture.md) | Vue 3 component and service layer |
| [docs/backend/backend-architecture.md](docs/backend/backend-architecture.md) | Flask routes, booth state, and ingest service |
| [docs/specifications/interpreter-portal-spec.md](docs/specifications/interpreter-portal-spec.md) | Interpreter console feature specification |
| [docs/specifications/booth-collaboration-spec.md](docs/specifications/booth-collaboration-spec.md) | Booth roles, handoff, and chat rules |
| [docs/specifications/jitsi-room-management-spec.md](docs/specifications/jitsi-room-management-spec.md) | Jitsi room and embed policy |
| [docs/setup/local-development.md](docs/setup/local-development.md) | Detailed local setup and troubleshooting |
| [docs/testing/e2e-testing.md](docs/testing/e2e-testing.md) | Test suite overview and manual scenarios |
| [docs/phases/implementation-roadmap.md](docs/phases/implementation-roadmap.md) | Current phase and future roadmap |

---

## Key constraints

- Jitsi iframe starts muted (receive-only). Interpreters must use headphones to prevent echo.
- Ingest audio is never looped back to the interpreter's local speakers.
- Browser mic capture enables `echoCancellation`, `noiseSuppression`, and `autoGainControl`.
- Exactly one active interpreter publishes per language channel at any time.
- Booth state is currently in-memory. Production deployments require PostgreSQL and Redis.
- Python runtime is pinned to `3.13.x`; `aiortc 1.14.0` and `av 16.1.0` are the validated media stack.

---

## Contributing

See [AGENTS.md](AGENTS.md) for implementation guardrails and agent-specific instructions.
See [docs/setup/local-development.md](docs/setup/local-development.md) for the full environment setup.

- `JITSI_DOMAIN` - allowed Jitsi hostname for iframe validation.
- `INGEST_HLS_ROOT` - local HLS output directory.
- `HLS_SEGMENT_SECONDS` - FFmpeg HLS segment length.
- `HLS_PLAYLIST_LENGTH` - HLS playlist window.

## Tests

```bash
uv run pytest
```

## Verified setup

The current dependency set was validated from scratch on macOS Apple Silicon with:

- `uv 0.9.24`
- CPython `3.13.5`
- `aiortc 1.14.0`
- `av 16.1.0`

That combination installs from wheels and avoids the older `av 11` source-build failure against newer FFmpeg headers.

## Basic runtime verification

```bash
uv run python -c "import app; print(app.AIORTC_AVAILABLE)"
uv run python app.py
```

Then open `http://127.0.0.1:5000/healthz` and confirm the JSON response reports `"ok": true`.

## Multi-user booth testing

1. Start the Flask app.
2. Open two interpreter tabs using the same booth URL.
3. Join both tabs with different display names and role `Interpreter`.
4. Open a third tab and join as `Coordinator`.
5. Verify the first interpreter is active by default.
6. Send booth chat messages from each tab.
7. Use `Pass Relay` from the active interpreter, or `Set Active` from the coordinator.
8. Confirm only the active interpreter can use `Go Live`.

## Streaming validation

1. Sync dependencies with `uv sync --python 3.13 --dev`.
2. Join a booth as the active interpreter.
3. Click `Go Live` and allow browser microphone access.
4. Verify the ingest endpoint returns an SDP answer.
5. Confirm HLS output appears under `INGEST_HLS_ROOT/<channel>/playlist.m3u8`.
6. Open the generated playlist in a local HLS-capable player or the Eventyay viewer integration.

## Failure testing

- Disconnect or refresh the active interpreter tab and verify another interpreter can become active.
- Switch active interpreter while ingest is live and verify the previous publisher is stopped.
- Join with an invalid token when `BOOTH_ACCESS_TOKEN` is set and verify access is rejected.
- Break the Python environment or remove `aiortc` and verify the UI shows ingest unavailable instead of fake telemetry.

## End-to-end validation runbook

### Target scenario

1. **Browser A (speaker/source)**  
   Join Jitsi room with mic/camera enabled and continuously speak.
2. **Browser B (active interpreter booth tab)**  
   Join same Jitsi room in monitor panel, become active interpreter, start ingest stream.
3. **Browser C (standby interpreter or coordinator booth tab)**  
   Join the same booth, send chat, and perform handoff.
4. **Browser D (viewer page)**  
   Open Eventyay stage page, switch language channel, verify interpretation audio and sync behavior.

### Pipeline checks

- Jitsi monitor loads and remains receive-focused.
- Mic permission behaves correctly.
- WebRTC SDP negotiation succeeds.
- Reconnect logic handles transport drops.
- Inactive interpreters cannot remain live when ownership switches.
- HLS playback resumes after ingest reconnect.
- Viewer fallback to original audio works.

### Edge cases to execute

- Interpreter browser refresh mid-stream
- Temporary network interruption
- Mic device switch
- Coordinator active-interpreter override
- Jitsi disconnect while ingest is live
- stale HLS playlist recovery

## Validation status in this repository

This repository currently validates:

- booth state ownership and handoff behavior with `uv run pytest`
- application startup and route serving in a `uv`-managed Python 3.13 environment
- code-path enforcement for active interpreter ownership

Full media-path E2E (Jitsi + ingest backend + FFmpeg/HLS + viewer app) requires external runtime services and must be run in an integrated Eventyay environment.
