# Eventyay Interpretation Portal

Eventyay Interpretation Portal is a lightweight collaborative interpretation booth for multilingual live events.

It is designed to feel like an Eventyay-first subsystem, not a standalone demo:

- **Jitsi** for monitoring and booth coordination
- **WebRTC** for interpreter microphone ingest
- **FFmpeg + HLS** for scalable viewer delivery
- **Collaborative booth controls** for coordinators, active interpreters, and standby interpreters

The development environment is managed with `uv` and pinned to a working Python/media stack for Apple Silicon. See [`docs/development.md`](./docs/development.md) for the full bootstrap and troubleshooting guide.

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
- **Coordination path:** Flask-SocketIO booth participant state and internal chat.

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for full diagrams and flow details.

## Interpreter workflow

1. Join monitor room from a Jitsi URL.
2. Join the booth as interpreter, coordinator, or listener.
3. Confirm active/standby ownership in the participant list.
4. Become active interpreter or receive coordinator assignment.
5. Start interpretation stream to the ingest endpoint.
6. Use booth chat and pass relay during handoff.

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
- Initial collaboration state is in memory; production deploys should add PostgreSQL persistence and Redis-backed Socket.IO when multiple workers are used.
- Local setup is pinned to Python `3.13.x`, `aiortc 1.14.0`, and `av 16.1.0`.
- The repo no longer relies on building `av` against the system FFmpeg installation during normal development setup.

## Scaling assumptions

- Initial target: medium event scale (hundreds of concurrent viewers per language through HLS distribution).
- FFmpeg and HLS packaging are external infrastructure concerns.
- CDN is optional for initial rollout; can be introduced as traffic profile grows.

## Local setup

```bash
uv sync --python 3.13 --dev
uv run python app.py
```

Open:

```text
http://127.0.0.1:5000/interpreter/hall-a-fr?token=dev-token&language=French
```

If `BOOTH_ACCESS_TOKEN` is empty, the `token` query parameter is optional. If a token is configured, every booth URL and API call must use the matching temporary token.

## Local environment

Copy `.env.example` to `.env` and configure:

- `HOST` - bind address for local development. Defaults to `127.0.0.1`.
- `PORT` - Flask/Socket.IO server port.
- `FLASK_DEBUG` - `1` to enable debug mode locally.
- `SECRET_KEY` - Flask session secret.
- `BOOTH_ACCESS_TOKEN` - optional temporary booth invite token.
- `BOOTH_WS_CORS_ORIGINS` - allowed Socket.IO origins.
- `DEFAULT_JITSI_ROOM` - monitoring room prefill.
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
