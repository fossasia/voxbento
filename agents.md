# Eventyay Interpretation Portal — Contributor/Agent Guide

This file defines implementation guardrails for contributors and coding agents working inside `eventyay-interpretation-portal/`.

## Product intent

Build a production-oriented interpretation subsystem that is:

- Eventyay-native in UI and architecture
- browser-first for interpreter operations
- collaborative for booth teams
- extensible for future multilingual/relay workflows

## Core design constraints

1. **Single-tab workflow:** no OBS/RTMP/external encoder requirements.
2. **Separation of concerns:**
   - Jitsi = monitoring/coordination
   - WebRTC ingest = interpreter audio uplink
   - HLS = viewer distribution
3. **One active publisher per language channel.**
4. **No local audio loopback from ingest stream path.**
5. **UI consistency:** match Eventyay video/streaming visual language.

## Role model

Supported booth roles:

- Active Interpreter
- Backup Interpreter
- Coordinator
- Listener

Expected behavior:

- only one active interpreter publishes at a time per channel
- coordinator can override and reassign active role
- booth chat remains internal/private to booth participants

## Runtime architecture expectations

- `app.py` owns Flask routes, Socket.IO events, and ingest endpoint boundaries.
- `portal/booth_state.py` owns in-memory booth state, participant roles, handoff policy, and chat history.
- `portal/ingest.py` owns aiortc peer negotiation and FFmpeg/HLS recorder setup.
- `templates/` owns server-rendered HTML.
- `static/js/interpreter-booth.js` owns browser behavior for joining, chat, Jitsi iframe setup, mic capture, and WebRTC offer creation.
- `static/css/interpreter.css` owns lightweight Eventyay-aligned styling.

Keep the browser layer small. Do not reintroduce a SPA, client-side router, frontend state store, or custom component system unless the Eventyay app integration requires it.

## Flow summary for implementation decisions

- **Interpreter flow:** Jitsi monitor join -> pre-flight -> mic test -> active assignment -> ingest start.
- **Coordinator flow:** monitor participant states -> reassign active interpreter -> supervise booth health.
- **Viewer flow (outside this module):** YouTube video clock + HLS language audio with drift-correction sync loop.

## Media pipeline responsibilities

- **Jitsi role:** monitoring and booth communication context.
- **WebRTC role:** low-latency microphone ingest to Eventyay ingest backend.
- **FFmpeg role:** transcode/segment interpreter audio to HLS assets.
- **HLS role:** scalable viewer delivery path for selected language channels.

## Realtime transport strategy

Current default:

- Flask-SocketIO with in-memory booth state
- booth invite URLs with optional temporary token query parameter

Production path:

- PostgreSQL for persistent booth/session records
- Redis or another Socket.IO message queue when multiple app workers are deployed
- keep viewer playback and ingest/HLS infrastructure outside the booth template layer

## Scaling and deployment assumptions

- medium initial event scale (hundreds of concurrent viewers per language path)
- ingest and FFmpeg/HLS services are deployed outside this frontend module
- websocket sync is deployment-dependent; local fallback keeps development usable

## Validation expectations

At minimum per change:

- `uv sync --python 3.13 --dev`
- `uv run pytest`
- manual browser check with two interpreter tabs and one coordinator tab

Dependency invariants:

- use the `uv` lockfile as the source of truth
- keep local development on Python `3.13.x` unless the media stack is revalidated
- avoid reintroducing `requirements.txt` as the primary bootstrap path
- do not require building `av` from source for normal contributor setup

For integrated environments:

- execute the multi-user and media-path scenario in `README.md`
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
