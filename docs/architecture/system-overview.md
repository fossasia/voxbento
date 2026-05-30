# System Overview

## Full system diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         LIVE EVENT                                       │
│                                                                           │
│  Speaker/Presenter                                                        │
│       │                                                                   │
│       ▼                                                                   │
│  Jitsi meeting room  ◄──────────────────────────────────────────────┐   │
│       │  (floor audio + video)                                        │   │
│       │                                                               │   │
│       │  iframe embed (receive-only)                                  │   │
│       ▼                                                               │   │
│  ┌────────────────────────────────────────────┐                      │   │
│  │         Interpreter Console (this repo)     │                      │   │
│  │                                             │                      │   │
│  │  ┌─────────────────┐  ┌──────────────────┐ │                      │   │
│  │  │ Jitsi Monitor   │  │ Mic Ingest Panel │ │                      │   │
│  │  │ Panel           │  │                  │ │                      │   │
│  │  │ (floor audio)   │  │ Preflight        │ │                      │   │
│  │  │                 │  │ Level meter      │ │                      │   │
│  │  │                 │  │ Go Live / Stop   │ │                      │   │
│  │  └─────────────────┘  └────────┬─────────┘ │                      │   │
│  │                                │            │                      │   │
│  │  ┌─────────────────┐  ┌────────┴─────────┐ │                      │   │
│  │  │ Participant Grid│  │ Booth Health     │ │                      │   │
│  │  │ (active/standby)│  │ Panel            │ │                      │   │
│  │  └─────────────────┘  └──────────────────┘ │                      │   │
│  │                                             │                      │   │
│  │  ┌────────────────────────────────────────┐ │                      │   │
│  │  │ Booth Chat Panel (WebSocket)           │ │ ──── booth:join ─────┘   │
│  │  └────────────────────────────────────────┘ │                          │
│  └────────────────────┬────────────────────────┘                          │
│                       │ WHIP (WebRTC)                                     │
│                       ▼                                                   │
│  ┌────────────────────────────────────────────┐                          │
│  │         MediaMTX (bluenviron/mediamtx:1)   │                          │
│  │                                             │                          │
│  │  WHIP endpoint :8889                       │                          │
│  │  HLS  endpoint :8888                       │                          │
│  │  overridePublisher for handoff             │                          │
│  └────────────────────┬────────────────────────┘                          │
│                       │ .m3u8 + .ts segments                              │
│                       ▼                                                   │
│  ┌──────────────────────────────────────────────────┐                    │
│  │  Listener page /listen/{booth_id}                │                    │
│  │                                                  │                    │
│  │  hls.js player with auto-recovery               │                    │
│  └──────────────────────────────────────────────────┘                    │
│                                                                           │
│  ┌──────────────────────────────────────────────────┐                    │
│  │  Eventyay Viewer (stage page)                    │                    │
│  │                                                  │                    │
│  │  YouTube player  +  Hidden HLS audio player      │                    │
│  │  (master clock)      (drift-corrected)           │                    │
│  └──────────────────────────────────────────────────┘                    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Component map

### Frontend (Jinja2 templates + plain ES modules)

| Component | File | Purpose |
|---|---|---|
| Interpreter booth page | `templates/interpreter_booth.html` | Server-rendered booth page with all panels |
| Booth JavaScript | `static/js/interpreter-booth.js` | Central state machine, WebSocket wiring, UI logic |
| Jitsi embed helpers | `static/js/interpreter-booth.js` | Jitsi URL parsing and embed URL construction |
| Listener page | `templates/listen.html` | HLS listener page with hls.js |

### Backend (FastAPI + WebSocket)

| Module | File | Purpose |
|---|---|---|
| FastAPI app | `fastapi_app.py` | Routes, WebSocket handlers, access token checks |
| `BoothRegistry` | `portal/booth_state.py` | Async in-memory booth state, roles, handoff, chat |
| `Auth` | `portal/auth.py` | JWT authentication via PyJWT |
| `Settings` | `portal/config.py` | pydantic-settings environment variable loading and validation |

### Templates

| Template | Purpose |
|---|---|
| `templates/base.html` | Eventyay-style page shell |
| `templates/interpreter_booth.html` | Server-rendered booth page |
| `templates/listen.html` | HLS listener page with hls.js auto-recovery |

### Media infrastructure (Docker Compose)

| Service | Image | Purpose |
|---|---|---|
| MediaMTX | `bluenviron/mediamtx:1` | WHIP ingest (:8889) and HLS delivery (:8888) |
| Jitsi Web | `jitsi/web:stable-9823` | Self-hosted Jitsi frontend (HTTP :8080, HTTPS :8443) |
| Jitsi Prosody | `jitsi/prosody:stable-9823` | XMPP server for Jitsi |
| Jitsi Jicofo | `jitsi/jicofo:stable-9823` | Jitsi conference focus |
| Jitsi JVB | `jitsi/jvb:stable-9823` | Jitsi video bridge |

---

## Data flow summary

### Monitoring path (Jitsi)

```
Speaker → Jitsi meeting → Jitsi iframe in interpreter console → interpreter's ears
```

The Jitsi iframe loads with `startWithAudioMuted=false` (it plays floor audio) but `startWithVideoMuted=true` and `disableInitialGUM=true` so the interpreter's mic/camera is never accidentally published into the Jitsi call. Jitsi is self-hosted via Docker containers (stable-9823), served on HTTP port :8080 (development) or HTTPS :8443 (production).

### Ingest path (WHIP → HLS)

```
Interpreter mic
  → getUserMedia (echoCancellation + noiseSuppression + autoGainControl)
  → RTCPeerConnection (audio track only)
  → WHIP POST to MediaMTX :8889
  → MediaMTX receives WebRTC stream
  → MediaMTX produces HLS segments
  → HLS available at MediaMTX :8888/{channel_id}/playlist.m3u8
```

Python never touches audio. The browser publishes directly to MediaMTX via WHIP. MediaMTX handles all transcoding and HLS segmentation.

### Coordination path (WebSocket)

```
Browser → WebSocket connect → /ws/booth/{booth_id}
       ← booth:joined + booth:state
       ↔ booth:chat / booth:set-active / booth:update-state
       ← booth:state (broadcast to all booth participants on any change)
```

---

## State ownership

| State | Owned by |
|---|---|
| Booth participants and roles | `portal/booth_state.py` (server, async in-memory) |
| Active interpreter assignment | `portal/booth_state.py` (server) |
| Handoff state | `portal/booth_state.py` (server) |
| Chat history (last 500 messages) | `portal/booth_state.py` (server) |
| WHIP ingest session | MediaMTX (external service) |
| Local mic stream | `static/js/interpreter-booth.js` (browser) |
| Local WebRTC peer connection | `static/js/interpreter-booth.js` (browser) |
| UI state (preflight, level, chat display) | `static/js/interpreter-booth.js` (browser) |

---

## Security model

- Booth access is controlled via JWT tokens (PyJWT) managed by `portal/auth.py`. All HTTP API calls and WebSocket messages must include a valid token.
- The WHIP endpoint on MediaMTX accepts publisher connections. The portal's booth state enforces that only the active interpreter for a channel is authorized to publish.
- Non-interpreter roles (coordinator, listener) cannot publish ingest audio.
- Booth state is private to booth participants; there is no public viewer-facing API in this module.
