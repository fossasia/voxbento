# Eventyay Interpretation Portal — Agent Guide

This file defines implementation guardrails for contributors and coding agents working inside `eventyay-interpretation-portal/`.

Read this file in full before making changes. The constraints here are non-negotiable.

---

## Product intent

Build a production-oriented interpretation subsystem that is:

- **Eventyay-native** in UI and architecture
- **Browser-first** for interpreter operations — no OBS, no RTMP, no external encoder
- **Collaborative** for booth teams (active interpreter, backup, coordinator, listener)
- **Extensible** for future multilingual, relay, and sign-language workflows

The current phase is the **Interpreter Console MVP**: mic tests, preflight checklist, and WebRTC ingest wiring. The ingest server infrastructure (aiortc endpoint, FFmpeg HLS segmenter) is being developed in parallel and will be connected in a future phase. Agents should not assume the ingest server is production-ready.

---

## Core design constraints

1. **Single-tab workflow:** no OBS/RTMP/external encoder requirements.
2. **Separation of concerns:**
   - Jitsi = monitoring and booth coordination context only
   - WebRTC ingest = interpreter audio uplink to the ingest API
   - HLS = viewer distribution (outside this repo)
3. **One active publisher per language channel at all times.**
4. **No local audio loopback from ingest stream path.** The `MicStreamingManager` intentionally never connects its analyser to `AudioContext.destination`.
5. **UI consistency:** match Eventyay video/streaming visual language (CSS variables, card-based layout).

---

## Role model

Supported booth roles:

| Role | Publishing rights | Assignment authority |
|---|---|---|
| Active Interpreter | Yes — only one per channel | Coordinator or self-assign on join |
| Backup Interpreter | No — standby only | Coordinator or current active |
| Coordinator | No — supervisory role | Fixed on join |
| Listener | No | Fixed on join |

Enforcement rules:

- Only the active interpreter may call `POST /api/interpreter/connect/{channel}`.
- Coordinator or active interpreter may call `booth:set-active` via Socket.IO.
- Non-interpreter roles cannot be promoted to active.

---

## Module ownership

| File / directory | Owns |
|---|---|
| `app.py` | Flask routes, Socket.IO event handlers, access token checks, ingest API boundaries |
| `portal/booth_state.py` | In-memory booth registry, participant roles, handoff policy, chat history |
| `portal/ingest.py` | aiortc peer negotiation, FFmpeg/HLS recorder setup, async runtime |
| `portal/config.py` | Settings loaded from environment variables |
| `templates/` | Server-rendered HTML (base shell + booth page) |
| `src/` | Vue 3 SPA (bundled by Vite, served by Flask in production) |
| `src/composables/useInterpreterBooth.js` | Central booth state machine, event wiring |
| `src/services/ingestClient.js` | WebRTC SDP negotiation with the ingest API |
| `src/services/micStreamingManager.js` | getUserMedia, level meter, peer connection lifecycle |
| `src/services/jitsiEmbed.js` | Jitsi URL parsing and embed URL construction |
| `src/services/boothRealtime.js` | Socket.IO + BroadcastChannel realtime transport |

---

## Flow summary

### Interpreter flow

```
Open booth URL → preflight checklist → mic test → active assignment → Go Live
     │                                                    │
     ▼                                                    ▼
Jitsi iframe loads (receive-only)            WebRTC offer → ingest API
Monitoring floor audio/video                 aiortc terminates RTP
                                             FFmpeg → HLS segments → hls-output/
```

### Coordinator flow

```
Open booth URL → monitor participant grid → assign active interpreter
```

### Viewer flow (outside this repo)

```
Eventyay stage page → select language → HLS audio loads
YouTube video clock (master) + HLS drift-correction loop
```

---

## Media pipeline responsibilities

| Component | Responsibility |
|---|---|
| Jitsi | Monitor floor audio/video; booth communication context |
| Browser `getUserMedia` | Capture interpreter microphone with echoCancellation, noiseSuppression, autoGainControl |
| `RTCPeerConnection` | Send audio track as Opus/RTP to the ingest endpoint |
| `portal/ingest.py` + aiortc | Terminate the WebRTC connection server-side; receive audio tracks |
| FFmpeg | Transcode/segment interpreter audio to HLS assets |
| HLS | Scalable viewer delivery; served from `hls-output/` |

---

## Realtime transport

Current (development):

- Flask-SocketIO with in-memory booth state
- `BroadcastChannel` for cross-tab coordination in the same browser
- Optional WebSocket URL for multi-machine setups

Production requirements:

- PostgreSQL for persistent booth and session records
- Redis-backed Socket.IO adapter when multiple Flask workers are deployed
- Keep viewer playback and ingest/HLS infrastructure outside this module

---

## What agents must not do

- Do not introduce jQuery or inline `<script>` blocks.
- Do not add a second client-side router or a new state management store.
- Do not replace `uv` with `pip` or `requirements.txt`.
- Do not import `pretix.*`, `pretalx.*`, or `venueless.*` namespaces.
- Do not modify `uv.lock` without re-running `uv sync --python 3.13 --dev` and confirming tests pass.
- Do not add any code that routes interpreter microphone audio back to `AudioContext.destination`.

---

## Validation before submitting changes

```bash
uv sync --python 3.13 --dev
uv run pytest
```

Manual browser check:

1. Open two interpreter tabs (active + backup).
2. Open one coordinator tab.
3. Confirm only one tab shows the active publisher state.
4. Confirm mic test and level meter work on the active tab.
5. Confirm coordinator can reassign active role.

---

## Dependency invariants

- Python runtime: `3.13.x` — do not change without revalidating the `aiortc`/`av` media stack.
- `aiortc 1.14.0` and `av 16.1.0` are the validated ingest dependency versions.
- `uv.lock` is the source of truth for all Python dependencies.
- Do not require building `av` from source for normal contributor setup.

- verify reconnect and handoff edge cases

## Documentation requirements

When changing architecture or behavior, update:

- `README.md` for operational usage and setup
- `ARCHITECTURE.md` for design and flow changes
- this `agents.md` when contributor guardrails or invariants change

## Non-goals for initial scope

- native desktop apps
- SIP integrations
- direct RTMP publishing from browser
- unrelated design systems or framework rewrites
