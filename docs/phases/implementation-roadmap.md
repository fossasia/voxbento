# Implementation Roadmap

This document tracks what has been built, what is in progress, and what is planned for future phases.

---

## Current phase: Interpreter Console MVP

**Status:** In progress

The goal of the MVP is a fully functional single-tab interpreter console that allows an interpreter to:

- Monitor the floor session via Jitsi
- Test their microphone locally (level meter, device selection)
- Complete a preflight checklist before going live
- Go live with one click (WebRTC → ingest API)
- Coordinate with booth colleagues (participant grid, internal chat, handoff)

### What is done

- [x] Server-rendered interpreter console (Jinja2 + plain ES module, single-tab layout)
- [x] Jitsi iframe monitoring panel (receive-only embed, self-hosted Jitsi Meet via Docker)
- [x] Mic capture via `getUserMedia` with DSP flags
- [x] Level meter (Web Audio API analyser, no loopback)
- [x] Device selector for audio input
- [x] Preflight checklist (4 items gating Go Live)
- [x] WebRTC/WHIP audio publishing to MediaMTX
- [x] Booth participant grid (active/backup/coordinator/listener with state badges)
- [x] Internal booth chat (WebSocket)
- [x] Coordinator handoff controls (Set Live button in participant grid)
- [x] FastAPI + native WebSocket server with async in-memory booth state
- [x] `BoothRegistry` with role enforcement and handoff policy
- [x] JWT-based booth access token security (PyJWT)
- [x] Auto-reconnect on WebRTC disconnection
- [x] `GET /healthz` health check endpoint
- [x] Unit tests for booth state and FastAPI routes
- [x] HLS listener page (`/listen/{booth_id}`) with hls.js auto-recovery
- [x] Self-hosted Jitsi Meet (4 Docker containers: web, prosody, jicofo, jvb)
- [x] MediaMTX WHIP ingest + HLS delivery (Docker)
- [x] Seamless interpreter handoff via `overridePublisher: yes`

### What is remaining for MVP completion

- [ ] End-to-end test with real mic in CI
- [ ] Production deployment instructions (HTTPS, TURN server)
- [ ] PostgreSQL persistence for booth state

---

## Phase 2: Production infrastructure hardening

**Status:** Not started

MediaMTX handles all audio ingest and HLS delivery. Phase 2 focuses on scaling, reliability, and HTTPS deployment.

### Goals

- Handle multiple simultaneous interpreters across multiple language channels
- Reliable HLS delivery at medium event scale (hundreds of concurrent viewers per language)
- HTTPS for production (required for `getUserMedia` in non-localhost environments)

### Planned work

- [ ] Configure TURN server for NAT traversal (required for interpreters behind restrictive firewalls)
- [ ] Deploy behind nginx reverse proxy with TLS termination
- [ ] Benchmark MediaMTX with 4–8 simultaneous interpreter channels
- [ ] CDN integration for HLS delivery (S3 + CloudFront, or equivalent)
- [ ] HLS latency tuning (Low-Latency HLS if required)
- [ ] MediaMTX monitoring and alerting (stream health, connection count)

---

## Phase 3: Persistent booth state

**Status:** Not started

### Goals

- Booth state survives server restarts
- Multi-worker deployments work correctly
- Session history is auditable

### Planned work

- [ ] PostgreSQL models for `Booth`, `Participant`, `ChatMessage`, `IngestSession`
- [ ] SQLAlchemy or Django ORM integration (if merging into main Eventyay Django app)
- [ ] Redis pub/sub for cross-worker WebSocket broadcasting
- [ ] Migration from in-memory `BoothRegistry` to DB-backed registry
- [ ] Session replay / chat history API for organizer review

---

## Phase 4: Eventyay core integration

**Status:** Not started

### Goals

- Booth URLs are generated from within the Eventyay event management UI
- Language channels are configured per event/session in Eventyay
- Viewer language selection is managed by the Eventyay stage page
- Authentication uses Eventyay user accounts rather than one-time tokens

### Planned work

- [ ] Eventyay Django app: `InterpretationChannel` model (language, channel_id, event, session)
- [ ] Eventyay organizer UI: configure interpretation channels per session
- [ ] Eventyay organizer UI: generate and revoke booth invite tokens
- [ ] Eventyay stage page: language audio selector wired to interpretation HLS streams
- [ ] Drift-correction HLS player integrated into the Eventyay video module
- [ ] SSO: interpreter login with Eventyay account (remove separate token auth)

---

## Phase 5: Advanced booth features

**Status:** Planned

### Relay interpretation

Relay interpretation is used when no interpreter can translate directly from the source language. A relay chain is: source language → relay language → target language.

Example: Turkish speaker → English relay interpreter → French final interpreter.

Planned:
- [ ] Booth can subscribe to another channel's HLS stream as their "floor" audio instead of Jitsi
- [ ] Relay chain configuration in the organizer UI
- [ ] Latency management for relay chains (relay adds ~15–30 s)

### Sign language video channels

- [ ] Video ingest path (not just audio)
- [ ] Interpreter camera capture via `getUserMedia` video track
- [ ] Video-over-WebRTC to ingest endpoint
- [ ] HLS video stream for sign language channels

### Interpreter analytics

- [ ] Speaking time per interpreter per session
- [ ] Handoff event log
- [ ] Ingest quality metrics (bitrate, packet loss, jitter)
- [ ] Post-event report for organizers

---

## Design principles that must not change across phases

1. **Single-tab interpreter workflow.** No matter how complex the backend becomes, the interpreter console must remain a single browser tab.
2. **Separation of monitoring and ingest.** Jitsi is monitoring. WebRTC/HLS is broadcast. They must never be the same path.
3. **One active publisher per language channel.** This rule is enforced server-side and must never be relaxed.
4. **No local audio loopback.** The interpreter must never hear their own interpretation audio through the browser.
5. **Graceful degradation.** If ingest is unavailable, monitoring and coordination still work.
