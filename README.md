# Eventyay Interpretation Portal

Eventyay Interpretation Portal is the browser-native interpreter operations console for multilingual live events.

It is designed to feel like an Eventyay-first subsystem, not a standalone demo:

- **Jitsi** for monitoring and booth coordination
- **WebRTC** for interpreter microphone ingest
- **FFmpeg + HLS** for scalable viewer delivery
- **Collaborative booth controls** for coordinators, active interpreters, and backups

## System goals

1. Run the interpreter workflow in one browser tab (no OBS/RTMP/external encoder).
2. Keep monitoring and ingest separated (Jitsi is not the ingest transport).
3. Enforce one active publisher per language channel.
4. Support collaborative booth operations (participant state, handoff, internal chat).
5. Stay visually and structurally aligned with Eventyay video UI patterns.

## Architecture summary

- **Interpreter monitor path:** Jitsi iframe, receive-focused by default.
- **Interpreter ingest path:** browser mic `getUserMedia` -> `RTCPeerConnection` offer/answer -> ingest endpoint.
- **Distribution path:** ingest server -> FFmpeg transcode/segment -> HLS playlist -> viewer player.
- **Coordination path:** booth participant state + chat sync (browser-local transport seam; websocket-ready).

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for full diagrams and flow details.

## Interpreter workflow

1. Join monitor room from a Jitsi URL.
2. Run pre-flight checks (headphones, monitoring, mic test, ingest reachability).
3. Select input device and run mic test.
4. Become active interpreter (or receive coordinator assignment).
5. Start interpretation stream to ingest endpoint.
6. Monitor booth health and reconnect states.

## Coordinator workflow

1. Monitor participant states and speaking activity.
2. Switch active interpreter when handoff is required.
3. Use booth chat for terminology/rotation/technical coordination.
4. Confirm only one live publisher per channel.

## Viewer workflow (upstream Eventyay surface)

1. Viewer selects language on stage/video page.
2. Viewer video clock (YouTube) remains master.
3. Hidden audio (HLS) is synchronized via drift correction loop.
4. Fallback to original audio remains available.

## Constraints and assumptions

- Jitsi monitor should start with mic/camera muted (receive-first behavior).
- Ingest audio must never be locally looped back.
- DSP flags are enabled for mic capture:
  - `echoCancellation: true`
  - `noiseSuppression: true`
  - `autoGainControl: true`
- Ingest negotiation endpoint contract:
  - `POST /api/interpreter/connect/{channel}`
- One active interpreter publishes per language channel.
- Initial collaboration transport can run without backend websocket; production deploys should provide websocket/pubsub for cross-device booth sync.

## Scaling assumptions

- Initial target: medium event scale (hundreds of concurrent viewers per language through HLS distribution).
- FFmpeg and HLS packaging are external infrastructure concerns.
- CDN is optional for initial rollout; can be introduced as traffic profile grows.

## Local startup

```bash
npm install
npm run dev
```

Alternative:

```bash
npm start
```

Open:

```text
http://localhost:5174/interpreter/<eventSlug>/<boothId>
```

Example:

```text
http://localhost:5174/interpreter/sample-event/booth-a
```

## Build

```bash
npm run build
```

## Lint

```bash
npm run lint
```

## Environment variables

Copy `.env.example` to `.env` and configure:

- `VITE_INGEST_BASE_URL` - ingest API base URL
- `VITE_INGEST_AUTH_TOKEN` - optional ingest auth token
- `VITE_BOOTH_WS_URL` - websocket/pubsub bridge endpoint for booth state sync
- `VITE_JITSI_DOMAIN` - allowed Jitsi domain for room join validation
- `VITE_JITSI_DEFAULT_URL` - default room URL prefill
- `VITE_STUN_SERVERS` - comma-separated STUN server list
- `VITE_DEFAULT_EVENT_SLUG` - default event identifier
- `VITE_DEFAULT_BOOTH_ID` - default booth identifier
- `VITE_DEFAULT_LANGUAGE_LABEL` - default language label
- `VITE_DEFAULT_CHANNEL_ID` - default ingest channel identifier

## End-to-end validation runbook

### Target scenario (3-browser workflow)

1. **Browser A (speaker/source)**  
   Join Jitsi room with mic/camera enabled and continuously speak.
2. **Browser B (interpreter console)**  
   Join same Jitsi room in monitor panel, run mic test, become active interpreter, start ingest stream.
3. **Browser C (viewer page)**  
   Open Eventyay stage page, switch language channel, verify interpretation audio and sync behavior.

### Pipeline checks

- Jitsi monitor loads and remains receive-focused.
- Mic permission and level meter behave correctly.
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

- lint/build integrity of the portal
- application startup and route serving in local dev mode
- code-path enforcement for active interpreter ownership

Full media-path E2E (Jitsi + ingest backend + FFmpeg/HLS + viewer app) requires external runtime services and must be run in an integrated Eventyay environment.
