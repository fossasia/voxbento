# Project Context

## What is the Eventyay Interpretation Portal?

The Eventyay Interpretation Portal is a browser-based subsystem of the Eventyay live event platform that enables human simultaneous interpreters to broadcast their translations to viewers in real time.

It is modelled on the real-world interpretation workflow at multilingual conferences (UN, EU Parliament, large international events) but eliminates the hardware and specialist knowledge requirements. An interpreter needs only:

- a laptop or desktop with a modern browser
- a headset with microphone
- the tokenized booth URL from the event organizer

There is no OBS, no RTMP, no hardware encoder, no sound card routing. Everything runs in one browser tab.

---

## Background and motivation

Traditional conference interpretation requires:

- Physical glass booths with relay consoles
- Professional hardware (Bosch, Televic, or similar interpreter desks)
- Trained audio engineers for mix-minus routing
- Proprietary software for remote interpretation (Interprefy, KUDO, etc.)

For community-run, volunteer-driven, and open-source events (such as those hosted on Eventyay), these options are either cost-prohibitive or logistically impossible. The Interpretation Portal aims to provide a viable, self-hosted alternative that integrates natively with Eventyay's existing video infrastructure.

---

## Design philosophy

**Interpreter-centric.** The portal is designed from the interpreter's point of view. The entire workflow — monitoring the session, coordinating with colleagues, testing the mic, and going live — happens in one browser tab.

**Separation of monitoring and ingest.** Jitsi handles the monitoring path (what the interpreter hears and sees). The WebRTC ingest handles the audio uplink. These two paths are deliberately kept separate so that Jitsi is never the bottleneck for broadcast quality.

**Operational safety over features.** The portal enforces a preflight checklist before allowing an interpreter to go live. Headphone use is a hard operational requirement to prevent acoustic echo.

**No single point of failure for viewers.** If the ingest connection drops, the viewer side falls back to the original floor audio while the interpreter reconnects.

---

## Current phase: Interpreter Console MVP

The current implementation covers:

- Server-rendered interpreter console (Jinja2 + plain ES module, single browser tab)
- Self-hosted Jitsi Meet embedded monitoring panel (receive-only iframe, Docker)
- Mic capture with DSP flags (echo cancellation, noise suppression, AGC)
- Level meter for mic test
- Preflight checklist gating Go Live
- WebRTC/WHIP audio publishing to MediaMTX
- Booth participant grid (active, backup, coordinator, listener)
- Internal booth chat (WebSocket)
- Coordinator handoff controls
- Async in-memory booth state (server-side, FastAPI + native WebSocket)
- HLS listener page with hls.js auto-recovery

**Not yet in scope (future phases):**

- PostgreSQL persistence for booth/session records
- Redis pub/sub for multi-worker WebSocket broadcasting
- CDN delivery of HLS segments
- Relay interpretation (interpreter-to-interpreter language chain)
- Sign language video channel support
- Organizer UI for managing interpretation channels

---

## How it fits into the Eventyay ecosystem

```
Eventyay core (Django app)
  │
  ├── Event stage page
  │     ├── YouTube video embed (master clock)
  │     └── HLS language audio player (drift-corrected)
  │
  └── Interpretation Portal (this repo)
        ├── FastAPI + WebSocket server (booth coordination)
        ├── Jinja2 templates + plain ES module (interpreter console)
        ├── Self-hosted Jitsi Meet (floor monitoring, Docker)
        └── MediaMTX (WHIP ingest → HLS output, Docker)
```

The portal feeds language-specific HLS streams to the Eventyay viewer. It does not modify or replace the YouTube video path. Viewer-side synchronization is handled by a drift-correction loop in the Eventyay video module.

---

## Key stakeholders

| Stakeholder | Interaction with this system |
|---|---|
| **Interpreter** | Opens booth URL; monitors floor via Jitsi; broadcasts translation via WebRTC |
| **Coordinator** | Supervises booth; manages handoffs; uses internal chat |
| **Event organizer** | Generates tokenized booth URLs; configures language channels |
| **Viewer** | Selects language on the Eventyay stage page; receives HLS audio |
| **Eventyay infrastructure** | Receives HLS segments from this portal's FFmpeg output |

---

## Non-goals

- The portal does not host or manage the Jitsi server. It embeds a Jitsi room via iframe.
- The portal does not deliver video to viewers. It delivers audio-only HLS.
- The portal does not replace Eventyay's main event management or ticketing surfaces.
- The portal does not support browser-based OBS or canvas mixing.
