# Eventyay Interpretation Portal — Agent Guide

This file defines implementation guardrails for contributors and coding agents working inside `eventyay-interpretation-portal/`.

Read this file in full before making changes. The constraints here are non-negotiable.

---
Follow Eventyay backend patterns wherever applicable. Reuse naming conventions, validation style, dataclass/model structure, route organization, API response patterns, testing conventions, and documentation style. Do not directly couple the interpretation portal to Eventyay yet. Only mirror architectural patterns to maintain consistency for future integration.
---

## Product intent

Build a production-oriented interpretation subsystem that is:

- **Eventyay-native** in UI and architecture
- **Browser-first** for interpreter operations — no OBS, no RTMP, no external encoder
- **Collaborative** for booth teams (active interpreter, backup, coordinator, listener)
- **Extensible** for future multilingual, relay, and sign-language workflows

**Current stack:** FastAPI (ASGI/uvicorn) + MediaMTX (WHIP/WHEP/HLS) + self-hosted Jitsi Meet.
No Flask, no Socket.IO, no aiortc.

---

## Core design constraints

1. **Single-tab workflow:** no OBS/RTMP/external encoder requirements.
2. **Separation of concerns:**
   - Jitsi = monitoring the floor session (self-hosted, receive-only)
   - WebRTC/WHIP = interpreter audio uplink directly to MediaMTX
   - WHEP = primary attendee audio delivery via MediaMTX (sub-second latency)
   - HLS = fallback attendee audio delivery via MediaMTX
   - FastAPI = coordination only (booth state, roles, chat, auth, dynamic MediaMTX path management)
3. **One active publisher per language channel at all times.**
4. **No local audio loopback.** Interpreter mic audio never routes to `AudioContext.destination`.
5. **UI consistency:** match Eventyay visual language (CSS variables, card-based layout).

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

- Only the active interpreter can go live (WHIP publish to MediaMTX).
- Coordinator or active interpreter may call `booth:set-active` via WebSocket.
- Non-interpreter roles cannot be promoted to active.
- MediaMTX enforces single-publisher per path (`overridePublisher: yes` for handoff).

---

## Module ownership

| File / directory | Owns |
|---|---|
| `fastapi_app.py` | FastAPI routes, WebSocket handler, JWT auth, Jinja2 templates |
| `portal/booth_state.py` | In-memory booth registry, participant roles, handoff policy, chat |
| `portal/auth.py` | JWT token creation and validation, participant token issuance with role claims |
| `portal/config.py` | pydantic-settings (env vars / .env) |
| `templates/` | Server-rendered HTML (base shell, booth page, WHEP listener page, HLS listener page) |
| `static/js/interpreter-booth.js` | Plain browser JS — WebRTC/WHIP, WebSocket, mic controls, Jitsi embed |
| `static/js/whep-listener.js` | WHEP WebRTC listener client |
| `static/css/interpreter.css` | Booth UI styles |
| `mediamtx.yml` | MediaMTX config (WHIP ingest, WHEP playback, HLS fallback, Control API, overridePublisher) |
| `docker-compose.yml` | Portal + MediaMTX + Jitsi services |

---

## Flow summary

### Interpreter flow

```
Open booth URL → preflight → mic test → active assignment → Go Live
     │                                                    │
     ▼                                                    ▼
Jitsi iframe loads (receive-only)            WHIP POST → MediaMTX
Monitoring floor audio/video                 MediaMTX → HLS segments
```

### Coordinator flow

```
Open booth URL → monitor participant grid → assign active interpreter
```

### Attendee flow

```
Open /listener-webrtc/{booth_id} → WHEP WebRTC connects → sub-second audio (primary)
Open /listen/{booth_id} → hls.js loads HLS stream → auto-recovers on handoff (fallback)
```

### Invite-token join flow

```
Receive invite link → GET /join/{token} → validate & redeem → JWT cookie → redirect to booth
     │                                         │
     ▼                                         ▼
Token invalid/expired/used → 4xx error    /interpreter/{event_slug}/{language_code}
```

---

## Media pipeline responsibilities

| Component | Responsibility |
|---|---|
| Self-hosted Jitsi Meet | Monitor floor audio/video; booth communication context |
| Browser `getUserMedia` | Capture interpreter mic (echoCancellation, noiseSuppression, autoGainControl) |
| `RTCPeerConnection` + WHIP | Send audio track as Opus/RTP directly to MediaMTX |
| MediaMTX | Terminate WebRTC, serve WHEP for low-latency playback, remux Opus → AAC for HLS |
| WHEP (listener-webrtc page) | Play WebRTC stream with sub-second latency and automatic handoff recovery |
| hls.js (listen page) | Play HLS fallback stream with auto-recovery during interpreter handoff |

---

## Realtime transport

- FastAPI native WebSocket with in-memory booth state (`asyncio.Lock`)
- JWT auth (PyJWT) with configurable expiry
- All booth coordination messages flow over `/ws/booth/{booth_id}`
- Audio never touches the Python process — browser talks directly to MediaMTX

---

## What agents must not do

- Do not introduce jQuery or inline `<script>` blocks.
- Do not add a second client-side router or a new state management store.
- Do not replace `uv` with `pip` or `requirements.txt`.
- Do not import `pretix.*`, `pretalx.*`, or `venueless.*` namespaces.
- Do not modify `uv.lock` without re-running `uv sync --python 3.13 --dev` and confirming tests pass.
- Do not add any code that routes interpreter mic audio back to `AudioContext.destination`.
- Do not add Vue, React, or any frontend framework. The frontend is plain browser JS.
- Do not add Flask, Socket.IO, or aiortc — these have been removed.

---

## Validation before submitting changes

```bash
uv sync --python 3.13 --dev
uv run pytest tests/ -v
node --check static/js/interpreter-booth.js
```

Manual browser check:

1. Open two interpreter tabs (active + backup).
2. Open one coordinator tab.
3. Confirm only one tab shows the active publisher state.
4. Confirm mic test and level meter work on the active tab.
5. Confirm coordinator can reassign active role.
6. Open `/listener-webrtc/demo-booth` and verify WHEP playback + handoff recovery.
7. Open `/listen/demo-booth` and verify HLS fallback playback.

---

## Dependency invariants

- Python runtime: `3.13.x`
- `uv.lock` is the source of truth for all Python dependencies.
- Docker images: `bluenviron/mediamtx:1`, `jitsi/*:stable-9823`
- Dependabot monitors Python, GitHub Actions, and Docker image updates.

---

## Documentation requirements

When changing architecture or behavior, update:

- `README.md` for operational usage and setup
- `ARCHITECTURE.md` for system design
- `agents.md` (this file) for guardrails

**Every PR that adds, removes, or changes a feature must update the relevant documentation files above as part of the same commit.** Do not defer documentation to a follow-up PR.
