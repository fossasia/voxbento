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

- `src/services/jitsiEmbed.js` for Jitsi URL/iframe behavior
- `src/services/ingestClient.js` for API negotiation boundaries
- `src/services/micStreamingManager.js` for media + WebRTC lifecycle
- `src/services/boothRealtime.js` for booth state/chat transport abstraction
- `src/composables/useInterpreterBooth.js` for state orchestration and policy enforcement

Do not collapse these boundaries into a single monolithic view component.

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

- browser-local collaboration via `BroadcastChannel` + persisted chat state

Production path:

- optional websocket transport (`VITE_BOOTH_WS_URL`) for cross-device booth sync
- keep transport swappable without rewriting UI components

## Scaling and deployment assumptions

- medium initial event scale (hundreds of concurrent viewers per language path)
- ingest and FFmpeg/HLS services are deployed outside this frontend module
- websocket sync is deployment-dependent; local fallback keeps development usable

## Validation expectations

At minimum per change:

- `npm run lint`
- `npm run build`

For integrated environments:

- execute the 3-browser scenario in `README.md` (speaker/interpreter/viewer)
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
