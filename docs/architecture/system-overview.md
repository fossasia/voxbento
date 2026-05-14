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
│  │  │ Booth Chat Panel (Socket.IO)           │ │ ──── booth:join ─────┘   │
│  │  └────────────────────────────────────────┘ │                          │
│  └────────────────────┬────────────────────────┘                          │
│                       │ WebRTC SDP offer/answer                           │
│                       ▼                                                   │
│  ┌────────────────────────────────────────────┐                          │
│  │         Flask + Socket.IO server (app.py)  │                          │
│  │                                             │                          │
│  │  POST /api/interpreter/connect/{channel}   │                          │
│  │  portal/ingest.py  (aiortc)                │                          │
│  └────────────────────┬────────────────────────┘                          │
│                       │ PCM / Opus tracks                                 │
│                       ▼                                                   │
│  ┌─────────────────────────────┐                                          │
│  │  FFmpeg transcode + segment │                                          │
│  └──────────────┬──────────────┘                                          │
│                 │ .m3u8 + .ts segments                                    │
│                 ▼                                                         │
│  hls-output/ ──► CDN / HLS origin server                                 │
│                 │                                                         │
│                 ▼                                                         │
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

### Frontend (Vue 3 SPA)

| Component | File | Purpose |
|---|---|---|
| `InterpreterConsoleView` | `src/views/InterpreterConsoleView.vue` | Top-level view; wires all panels together |
| `JitsiMonitorPanel` | `src/components/JitsiMonitorPanel.vue` | Jitsi iframe embed and join controls |
| `MicIngestPanel` | `src/components/MicIngestPanel.vue` | Device selector, level meter, Go Live / Stop |
| `PreflightChecklist` | `src/components/PreflightChecklist.vue` | Checklist gating the Go Live button |
| `ParticipantGrid` | `src/components/ParticipantGrid.vue` | Booth participant list with role/status badges |
| `BoothChatPanel` | `src/components/BoothChatPanel.vue` | Internal booth chat |
| `BoothHealthPanel` | `src/components/BoothHealthPanel.vue` | Live ingest health indicators |
| `useInterpreterBooth` | `src/composables/useInterpreterBooth.js` | Central state machine and event wiring |
| `IngestClient` | `src/services/ingestClient.js` | WebRTC SDP negotiation with the ingest API |
| `MicStreamingManager` | `src/services/micStreamingManager.js` | getUserMedia, level meter, peer connection |
| `buildJitsiEmbedUrl` | `src/services/jitsiEmbed.js` | Jitsi URL parsing and embed URL construction |
| `BoothRealtimeClient` | `src/services/boothRealtime.js` | Socket.IO + BroadcastChannel realtime transport |

### Backend (Flask + Socket.IO)

| Module | File | Purpose |
|---|---|---|
| Flask app | `app.py` | Routes, Socket.IO handlers, access token checks |
| `BoothRegistry` | `portal/booth_state.py` | In-memory booth state, roles, handoff, chat |
| `IngestService` | `portal/ingest.py` | aiortc peer connection management, async runtime |
| `Settings` | `portal/config.py` | Environment variable loading and validation |

### Templates

| Template | Purpose |
|---|---|
| `templates/base.html` | Eventyay-style page shell |
| `templates/interpreter_booth.html` | Server-rendered booth page (passes config to Vue) |

---

## Data flow summary

### Monitoring path (Jitsi)

```
Speaker → Jitsi meeting → Jitsi iframe in interpreter console → interpreter's ears
```

The Jitsi iframe loads with `startWithAudioMuted=false` (it plays floor audio) but `startWithVideoMuted=true` and `disableInitialGUM=true` so the interpreter's mic/camera is never accidentally published into the Jitsi call.

### Ingest path (WebRTC → HLS)

```
Interpreter mic
  → getUserMedia (echoCancellation + noiseSuppression + autoGainControl)
  → RTCPeerConnection (audio track only)
  → SDP offer → POST /api/interpreter/connect/{channel_id}
  → aiortc RTCPeerConnection (server-side)
  → SDP answer returned
  → RTP Opus stream received server-side
  → aiortc MediaRecorder → FFmpeg command or HLS recorder
  → hls-output/{channel_id}/playlist.m3u8 + segments
```

### Coordination path (Socket.IO)

```
Browser → socket.io connect → booth:join
       ← booth:joined + booth:state
       ↔ booth:chat / booth:set-active / booth:update-state
       ← booth:state (broadcast to all booth participants on any change)
```

---

## State ownership

| State | Owned by |
|---|---|
| Booth participants and roles | `portal/booth_state.py` (server, in-memory) |
| Active interpreter assignment | `portal/booth_state.py` (server) |
| Handoff state | `portal/booth_state.py` (server) |
| Chat history (last 500 messages) | `portal/booth_state.py` (server) |
| Ingest session (peer connection) | `portal/ingest.py` (server, async runtime) |
| Local mic stream | `MicStreamingManager` (browser) |
| Local WebRTC peer connection | `MicStreamingManager` (browser) |
| UI state (preflight, level, chat display) | `useInterpreterBooth` composable (browser) |
| Persisted chat (localStorage) | `BoothRealtimeClient` (browser) |

---

## Security model

- Booth URLs carry an optional `BOOTH_ACCESS_TOKEN`. When set, all HTTP API calls and Socket.IO events must include the matching token.
- The ingest endpoint (`POST /api/interpreter/connect/{channel}`) additionally checks that the requesting participant is the active interpreter for the channel before accepting the SDP offer.
- Non-interpreter roles (coordinator, listener) cannot publish ingest audio.
- Booth state is private to booth participants; there is no public viewer-facing API in this module.
