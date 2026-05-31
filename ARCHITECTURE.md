# Eventyay Interpretation Portal Architecture

## 1. Scope and intent

The interpreter portal is a collaborative interpretation booth console integrated with Eventyay live workflows.

It covers:

- interpreter monitoring (self-hosted Jitsi Meet)
- interpreter audio ingest (WebRTC/WHIP → MediaMTX → WHEP/HLS)
- booth operations (participants, roles, handoff, internal chat, health state)

It does **not** replace Eventyay viewer playback surfaces; it feeds them.

## 2. Full system architecture

```mermaid
flowchart LR
  Speaker[Speaker/Presenter] -->|AV + floor audio| Jitsi[Self-hosted Jitsi Meet]
  Interpreter[Interpreter Portal] -->|Jitsi iframe monitor| Jitsi
  Interpreter -->|Mic → WHIP POST| MediaMTX[MediaMTX :8889]
  MediaMTX -->|WHEP WebRTC| WHEPListener[/listener-webrtc page - primary]
  MediaMTX -->|HLS segments| HLS[MediaMTX :8888]
  HLS -->|index.m3u8| HLSListener[/listen page - hls.js fallback]
  HLS -->|index.m3u8| Viewer[Eventyay stage page]
  Viewer -->|sync loop| YouTube[YouTube player - master clock]
```

**Key principle:** Python is never in the audio path. The browser publishes directly to MediaMTX via WHIP. MediaMTX handles WebRTC termination and serves listeners via WHEP (sub-second latency) or HLS (fallback, ~2–3 s latency).

## 3. Interpreter audio pipeline

```mermaid
flowchart TD
  Mic[Interpreter microphone] --> GUM[navigator.mediaDevices.getUserMedia]
  GUM --> DSP[Browser DSP: echoCancellation, noiseSuppression, autoGainControl]
  DSP --> Track[MediaStreamTrack - audio only]
  Track --> PC[RTCPeerConnection]
  PC --> WHIP[WHIP POST to MediaMTX :8889]
  WHIP --> MTX[MediaMTX terminates WebRTC]
  MTX --> WHEP[WHEP at :8889/channel-id/whep - primary, sub-second]
  MTX --> HLS[HLS segments at :8888/channel-id/index.m3u8 - fallback]
```

No server-side audio processing. MediaMTX does everything.

### Seamless interpreter handoff

MediaMTX runs with `overridePublisher: yes`. Paths are created with `alwaysAvailable: true` via the MediaMTX Control API (:9997) so readers stay connected even when no publisher is active. When a coordinator switches the active interpreter:

1. The incoming interpreter's WHIP POST succeeds immediately (MediaMTX kicks the outgoing publisher)
2. WHEP listeners receive the new publisher's audio within ~1.5–3 s (the `RTCPeerConnection` stays open; MediaMTX routes the new track automatically)
3. HLS fallback listeners experience a longer gap (~10–15 s) as hls.js recovers from the muxer reset

## 4. Viewer synchronization flow

```mermaid
sequenceDiagram
  participant V as Eventyay Viewer
  participant Y as YouTube Player (master clock)
  participant A as Hidden HLS Audio

  loop every ~500ms
    V->>Y: read currentTime
    V->>A: read currentTime
    V->>V: compute drift = Y - A
    alt small drift
      V->>A: playbackRate smoothing
    else large drift
      V->>A: hard resync seek
    end
  end
```

## 5. Multi-user booth architecture

```mermaid
flowchart LR
  Coordinator[Coordinator] <--> Chat[Booth internal chat]
  Active[Active interpreter] <--> Chat
  Backup[Backup interpreter] <--> Chat
  Listener[Listener/observer] <--> Chat

  Coordinator -->|assign live role| ActiveState[Active interpreter state]
  Backup -->|request handoff| Coordinator
  ActiveState -->|single publisher per channel| WHIP[WHIP → MediaMTX]

  Coordinator --> Jitsi[Self-hosted Jitsi]
  Active --> Jitsi
  Backup --> Jitsi
  Listener --> Jitsi
```

## 6. Booth identity scheme

A booth is identified by three coordinates:

| Coordinate      | Format                          | Example         |
|-----------------|---------------------------------|-----------------|
| `event_slug`    | Lowercase alphanumeric + hyphens, no consecutive hyphens, max 64 chars | `pycon2026` |
| `language_code` | ISO 639-1 two-letter code       | `en`            |
| `instance`      | `primary` or `backup`           | `primary`       |

**Booth ID:** `{event_slug}-{language_code}` → `pycon2026-en`

**MediaMTX path:** `{event_slug}/{language_code}` → `pycon2026/en` (one active stream per language per event)

**Channel ID:** defaults to the MediaMTX path (`{event_slug}/{language_code}`) when created via `create_booth()`. Can be overridden with an explicit value.

**Room ID:** optional integer FK to an Eventyay Room (`room_id: int | None`). Nullable — has no effect on booth identity. Exists to support future Eventyay integration for mapping booths to event rooms.

### Validation rules

- **Event slug:** `^[a-z0-9]+(?:-[a-z0-9]+)*$`, 1–64 characters. Must start and end with alphanumeric. No consecutive hyphens, no underscores, no spaces.
- **Language code:** Exactly two lowercase ASCII letters matching a recognised ISO 639-1 code.
- **Instance:** Either `primary` or `backup`. Only one instance publishes at a time.

### Bidirectional conversion

```
booth_id_to_mediamtx_path("pycon2026-en")  →  "pycon2026/en"
mediamtx_path_to_booth_id("pycon2026/en")  →  "pycon2026-en"
parse_booth_id("my-great-event-fr")        →  ("my-great-event", "fr")
```

The language code is always the last two characters after the final hyphen.

### Legacy compatibility

Existing free-form booth IDs (e.g. `hall-a-fr`) that happen to end with a valid ISO 639-1 code are parsed automatically. IDs that cannot be parsed are accepted with empty identity fields during the migration window.

## 7. Runtime components in this repository

- `fastapi_app.py`
  - FastAPI routes, WebSocket event handlers, JWT auth, Jinja2 templates, health checks
- `portal/booth_identity.py`
  - booth identity scheme: validation (event slug, ISO 639-1 language code, instance), booth ID generation, MediaMTX path mapping, bidirectional conversion
- `portal/booth_state.py`
  - async in-memory booth registry, participant role policy, active interpreter ownership, handoff state, chat history
- `portal/auth.py`
  - JWT token creation and validation (PyJWT)
- `portal/config.py`
  - pydantic-settings configuration loaded from environment variables / `.env`
- `templates/base.html`
  - Eventyay-style header and page shell
- `templates/interpreter_booth.html`
  - server-rendered interpreter booth page
- `templates/listener.html`
  - attendee HLS listener page with hls.js auto-recovery (fallback)
- `templates/listener-webrtc.html`
  - attendee WHEP WebRTC listener page (primary, sub-second latency)
- `static/js/interpreter-booth.js`
  - browser mic capture, WHIP WebRTC publishing, Jitsi iframe embed, WebSocket coordination, DOM updates
- `static/js/whep-listener.js`
  - WHEP WebRTC listener client — connects to MediaMTX WHEP endpoint for low-latency playback
- `static/css/interpreter.css`
  - lightweight Eventyay-aligned styles
- `mediamtx.yml`
  - MediaMTX configuration (WHIP ingest, WHEP playback, HLS fallback, Control API, overridePublisher for handoff)
- `docker-compose.yml`
  - all services: portal, mediamtx, jitsi-web, jitsi-prosody, jitsi-jicofo, jitsi-jvb

## 8. State model and ownership

`BoothRegistry` tracks:

- booth identity (`booth_id`, `event_slug`, `language_code`, `instance`, `mediamtx_path`)
- booth metadata (`language`, `channel_id`, `room_id`)
- active interpreter id
- participant roster and roles
- per-participant connection, mic, and ingest state
- handoff state (`idle`, `active`)
- internal booth chat timeline (last 500 messages)
- ingest status

The browser keeps only local UI/session state: joined participant id, mic stream, peer connection, current booth snapshot, and current chat messages. Server state remains the source of truth for who is active.

## 9. Active interpreter enforcement

Enforcement rules:

1. Start WHIP ingest only when local participant is active for the channel.
2. Only the active interpreter can click "Go Live" to publish audio.
3. Active interpreter handoff via `booth:set-active` clears the previous publisher's mic and ingest state.
4. MediaMTX enforces single-publisher per path (`overridePublisher: yes`).
5. Coordinator role can override active ownership.
6. Non-interpreter roles cannot become active publishers.

## 10. Reconnect and teardown behavior

Reconnect:

- browser peer connection state is surfaced as connected/reconnecting/disconnected
- stale live publishers are stopped when active ownership changes
- hls.js listener auto-recovers from HLS muxer reset during handoff (fallback path)
- WHEP listeners stay connected during handoff via `alwaysAvailable` paths; new audio arrives within ~1.5–3 s

Teardown:

- close WebRTC peer connection (stops WHIP session in MediaMTX)
- update booth state over WebSocket
- remove participant from in-memory booth on WebSocket disconnect

## 11. Jitsi role vs ingest role

Jitsi responsibilities:

- monitor floor audio/video (receive-only)
- booth coordination context (interpreters hear the speaker)
- self-hosted via Docker (jitsi-web, jitsi-prosody, jitsi-jicofo, jitsi-jvb)

Jitsi non-goals:

- not the interpreter ingest transport
- not viewer delivery pipeline

Ingest responsibilities:

- receive interpreter mic audio uplink via WHIP → MediaMTX
- MediaMTX produces WHEP for low-latency WebRTC playback and HLS as fallback for viewer consumption

## 12. Deployment assumptions

- interpreter portal is served as an ASGI application (FastAPI + uvicorn)
- WHIP endpoint (MediaMTX) is reachable from interpreter browsers
- WHEP endpoint (MediaMTX) is reachable from listener browsers
- WebSocket is available for cross-client booth state
- self-hosted Jitsi Meet provides floor monitoring (4 Docker containers)
- viewer stage page consumes language channels from MediaMTX (WHEP or HLS)
- PostgreSQL and Redis can be added later for persistence and multi-worker scale
- in Docker, `DOCKER_HOST_ADDRESS` must be set to the host's LAN IP for JVB ICE to work

## 13. Reliability and operational constraints

- recommend headphones-first operation to reduce feedback risk
- prevent local audio loopback in mic capture path
- preserve clear state indicators for ingest, reconnecting, and live ownership
- keep service boundaries explicit: FastAPI = coordination only, MediaMTX = audio pipeline, Jitsi = floor monitoring
