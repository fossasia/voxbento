# Eventyay Interpretation Portal — Audio Streaming Plan

> **Document status:** Working plan · Last updated: May 2026  
> **Scope:** End-to-end design from mic capture to audience consumption, from local dev to single Docker Compose production deployment.

> **Platform independence:** The interpretation portal is a self-contained product. Its primary output is a set of plain **HLS stream URLs** — one per language per event. These URLs can be consumed by Eventyay (Phase 6), embedded in any third-party event platform, pasted into a player widget, or played directly in a browser. Eventyay integration is optional and additive; it does not affect how the portal operates.

---

## Table of Contents

1. [What We Currently Have](#1-what-we-currently-have)
2. [Gap Analysis — What Is Missing or Broken](#2-gap-analysis)
3. [Architecture Decisions](#3-architecture-decisions)
4. [Phase 1 — Mic Controls + End-to-End Ingest Working](#4-phase-1)
5. [Phase 2 — Production Audio Server (MediaMTX)](#5-phase-2)
6. [Phase 3 — Multi-Booth, Multi-Event Data Model](#6-phase-3)
7. [Phase 4 — Authentication, RBAC, and Admin Panel](#7-phase-4-auth)
8. [Phase 5 — Interpreter Console UX (Google Meet-style)](#8-phase-5-ux)
9. [Phase 6 — Eventyay Integration (Audio Links)](#9-phase-6)
10. [Phase 7 — Docker Compose Deployment](#10-phase-7)
11. [Full System Architecture (Final State)](#11-full-system-architecture)
12. [Open Questions and Deferred Decisions](#12-open-questions)

---

## 1. What We Currently Have

### 1.1 Interpretation Portal Repository (`eventyay-interpretation-portal/`)

The portal is a **separate, standalone** application from the main Eventyay platform. It consists of two layers:

#### Backend (Python/Flask)

| File | What it does | Status |
|---|---|---|
| `app.py` | Flask 3 + Flask-SocketIO 5 server. Routes for booth state API, interpreter connect/disconnect, and Socket.IO realtime coordination. | Working |
| `portal/booth_state.py` | In-memory `BoothRegistry` with thread-safe `RLock`. Manages participants, roles (Active/Backup/Coordinator/Listener), chat, active-interpreter enforcement, and handoff state. | Working |
| `portal/ingest.py` | `aiortc` WebRTC peer connection handler. Receives browser SDP offer, returns answer. On RTP track arrival starts `aiortc.MediaRecorder` writing HLS (AAC 128kbps, 2s segments, 8-segment sliding window) to `hls-output/<channel_id>/`. | Working in isolation, not yet reliable behind NAT |
| `portal/config.py` | Typed `Settings` dataclass from env vars. | Working |

#### Frontend Stack: Active UI vs. Prototype SPA

The portal has two frontends in the repository, but only one is the **active production path**:

| Path | Stack | Status |
|---|---|---|
| `templates/` + `static/js/` | Jinja2 + vanilla ES modules, Socket.IO | **Active — all new work goes here** |
| `src/` | Vue 3 SPA (Vite build) | Prototype only — not wired to Flask, not used in production |

All further UI development uses **server-rendered Jinja2 templates and plain ES module JavaScript** — no build step, no bundler, no frontend framework. This keeps the portal deployable as a single Python process without a Node.js build stage.

**Why vanilla JS?** The interpreter console is a single-page tool with a fixed set of controls. A framework would add build complexity and bundle size with no meaningful benefit at this scope. Native browser APIs (`RTCPeerConnection`, `getUserMedia`, `MediaDevices`, `AnalyserNode`, `WebSocket`) cover every requirement directly.

The `src/` Vue SPA was an early prototype. It is **not connected** to the Flask backend and will be removed in a dedicated cleanup PR (tracked separately). The `package.json`, `vite.config.js`, and related node config files remain in-tree until that PR lands — do not treat them as the active frontend stack.

#### HLS Output Evidence

`hls-output/` is gitignored (local runtime output, not committed). When the aiortc ingest pipeline runs locally, it writes a sliding-window HLS playlist to `hls-output/<channel_id>/playlist.m3u8` plus MPEG-TS segment files (~2s each, AAC audio). A successful local test run produces approximately 9 segments in a demo session. This confirms the aiortc → FFmpeg → HLS pipeline executes correctly on a local machine before the MediaMTX migration.

---

### 1.2 Eventyay Main Platform — Stream URL Model

The main platform (`app/eventyay/`) already has infrastructure for per-room stream URLs:

| Model/Location | Purpose |
|---|---|
| `base/models/stream_schedule.py` — `StreamSchedule` | Room-scoped, time-bound stream URL with `stream_type` choices: `youtube`, `vimeo`, `hls`, `iframe`, `native`. Has a free-form `config` JSONField labelled "language settings". |
| `base/models/schedule.py` — schedule data builder | Injects `stream_url` + `stream_type` into talk-level schedule JSON when a `StreamSchedule` overlaps the talk's time window. |
| `api/views/stream_schedule.py` + `room.py` | REST CRUD for `StreamSchedule`. `/rooms/<pk>/streams/current` returns the active stream for real-time updates. |
| `base/services/room.py` periodic task | Polls for active `StreamSchedule` per room and broadcasts changes over Django Channels (`stream.change` message). Enables live URL switching without page reload. |

**Key insight:** `StreamSchedule.stream_type = 'hls'` and `StreamSchedule.url` already exist as first-class fields. There is no special-purpose interpretation-language field yet, but the `config` JSONField is the designated extension point.

---

## 2. Gap Analysis

### 2.1 What Is Not Working Yet

| Gap | Impact | Root cause |
|---|---|---|
| **WebRTC fails behind NAT/firewall** | Mic audio never reaches server in any real deployment | No STUN/TURN server configured. aiortc `RTCPeerConnection` has no STUN URIs. |
| **HLS files are not served** | Even when HLS is written to disk, nothing serves `playlist.m3u8` over HTTP with correct CORS headers | No web server (Nginx/Caddy) configured to expose `hls-output/` |
| **No authentication or RBAC** | Any person with the URL can join any booth in any role | There is only a flat `BOOTH_ACCESS_TOKEN` shared secret. No per-user identity, no role enforcement beyond the in-memory state machine. |
| **No admin panel** | Booths and events cannot be configured without directly editing env vars or the API | No UI for Admin → Events → Rooms → Booths hierarchy |
| **No interpreter invite links** | Interpreters have no secure, role-scoped URL to access their booth | Token system does not exist yet |
| **No audio device selection** | Interpreter cannot choose which microphone to use | `getUserMedia` uses the browser default device with no UI to change it |
| **In-memory state lost on restart** | All booth state, active interpreters, and sessions vanish on every deploy | No Redis or database persistence layer |
| **Single active ingest per channel not enforced at network level** | Two interpreters could both `POST /api/interpreter/connect` for the same channel if state drifts | Enforcement is purely in-memory in `booth_state.py` |
| **No event-level scoping** | Booth IDs are flat strings. No concept of which Eventyay event or room a booth belongs to | `booth_id` is unstructured. No foreign key to Eventyay data. |
| **No Eventyay integration hook** | The HLS URL produced has no way to appear in Eventyay's `StreamSchedule` for the correct room/language | API bridge does not exist yet |
| **aiortc reliability at scale** | aiortc is a research-grade Python WebRTC implementation. ICE restarts, codec negotiation edge cases, and high concurrency will be problematic | Architecture decision required |

### 2.2 What Is Working Well (Keep and Build On)

- The **WebRTC SDP offer/answer flow** in `interpreter-booth.js` is solid and correct.
- The **booth state machine** (`booth_state.py`) correctly models roles, active-interpreter enforcement, and handoff.
- The **Socket.IO coordination layer** in `app.py` is functional.
- The **HLS segmenter output format** (AAC 128kbps, 2s segments, sliding window) is correct for live delivery.
- The **multi-booth data model** is already designed — each `channel_id` is an independent HLS stream.
- Eventyay's `StreamSchedule` model already has `stream_type='hls'` support.

---

## 3. Architecture Decisions

### 3.1 Audio Server Selection

The core question is: **where does the browser's WebRTC audio land, and what turns it into an HLS stream?**

#### Option A — Keep aiortc + add coturn + Nginx (minimal change)

```
Browser → WebRTC → aiortc (Python, in Flask process) → MediaRecorder → HLS files → Nginx
```

**Pros:** Already partially implemented.  
**Cons:** aiortc is not production-grade. Running WebRTC inside a Python Flask thread alongside HTTP request handling is fragile. ICE restarts, concurrent sessions, and container restarts all have sharp edges. The AsyncRuntime thread bridge is clever but adds failure modes.

#### Option B — MediaMTX (recommended)

[MediaMTX](https://github.com/bluenviron/mediamtx) is a production-ready Go-based media server that:
- Accepts **WHIP** (WebRTC HTTP Ingest Protocol — the IETF-standard replacement for custom SDP negotiation)
- Has a **built-in TURN server** (no coturn needed)
- Outputs **native HLS** from each WHIP stream
- Runs as a single stateless binary / tiny Docker image (`~20MB`)
- Maps each **"path"** to one stream — `english-booth-1`, `spanish-booth-2`, etc.
- No external FFmpeg process needed

```
Browser → WHIP (HTTP + WebRTC) → MediaMTX → HLS → Nginx (or MediaMTX built-in HTTP)
```

The interpretation portal Flask server **stays** for all coordination logic (booth state, participants, chat, Socket.IO). It only hands off the actual audio ingest to MediaMTX via WHIP. This removes aiortc from the critical path entirely.

#### Option C — Livekit

Full WebRTC SFU with egress recording to HLS. More powerful (supports mixing, transcription), but significantly more complex to operate and overkill for single-stream-per-booth audio.

**Decision: Use MediaMTX for audio ingest and HLS delivery. Keep Flask+Socket.IO for coordination.**

### 3.2 Transport Protocol: WHIP

**What it is:** WHIP (WebRTC-HTTP Ingest Protocol) is an IETF-standardised way for a browser to push a WebRTC audio/video stream to a media server using a single HTTP POST. The browser sends an SDP offer as the request body; the server responds with an SDP answer; then RTP/UDP flows directly browser → server.

**Why chosen over the current custom SDP endpoint:** The current `POST /api/interpreter/connect` in Flask is effectively a hand-rolled version of WHIP. Replacing it with the standard means:
- The browser can talk directly to MediaMTX without Flask in the audio path (Flask only validates auth, then hands back the WHIP URL)
- Any WHIP-compatible media server can be swapped in later without changing browser code
- Simpler code: `fetch(whipUrl, { method: 'POST', body: offer.sdp })` — no JSON wrapper, no custom schema

### 3.3 HLS Delivery and CORS

**What HLS is:** HTTP Live Streaming (HLS) is Apple's adaptive streaming protocol. A server writes a rolling playlist file (`index.m3u8`) listing short audio/video segment files (`.ts`). A player fetches the playlist periodically and downloads new segments as they appear — this is how live streaming works on the web without any persistent connection.

**Why HLS over WebRTC for audience delivery:** WebRTC gives < 500ms latency but requires a persistent signalling server per viewer and does not scale cost-effectively to hundreds of simultaneous listeners. HLS scales horizontally to any number of viewers via a standard CDN, works in every browser natively on Safari and with `hls.js` everywhere else, and the 2–4s latency is acceptable for simultaneous interpretation.

**Why not DASH or other formats:** HLS has universal browser support and MediaMTX outputs it natively. DASH is equally capable but requires additional player library support with no advantage at this scale.

MediaMTX serves HLS at `http://media-server:8888/<path>/index.m3u8`. An Nginx reverse proxy sits in front to apply CORS headers (so any origin — Eventyay, a third-party embed, or a standalone player — can fetch the playlist) and to terminate TLS. The public-facing URL is what operators share and what any consumer (Eventyay or otherwise) plugs in.

### 3.4 State Persistence

**Why Redis for Socket.IO:** Flask-SocketIO in multi-worker mode requires a message queue so that an event emitted by worker A reaches clients connected to worker B. Redis pub/sub is the standard Flask-SocketIO backend for this. Without it, Socket.IO events only reach clients connected to the same process.

**Why SQLite for booth/auth data (Phase 4):** The portal's relational data (events, rooms, booths, invite tokens) has a simple schema with low write volume. SQLite handles this without an additional service. PostgreSQL can be substituted by changing one env var (`DATABASE_URL`) if the deployment grows.

The PostgreSQL database of the main Eventyay platform is **not** touched by the portal. The portal only calls Eventyay's REST API when Eventyay integration is enabled — and that is entirely optional.

### 3.5 Why Flask

The project already uses Flask and Flask-SocketIO. Flask is kept because:
- It is already integrated with aiortc and the booth state machine
- Flask-SocketIO provides the realtime coordination layer (booth state sync, chat, role events) with minimal ceremony
- Jinja2 template rendering is native to Flask — no template engine to add
- The portal is a small, single-purpose service; Django or FastAPI would be overengineered

### 3.6 Why Nginx

Nginx sits in front of both Flask (portal) and MediaMTX (media) to:
1. Terminate TLS so both services serve `https://`
2. Set CORS response headers on HLS responses — MediaMTX does not set them by default
3. Enforce `Cache-Control: no-cache` on HLS playlists (critical for live streams)
4. Proxy WebSocket upgrades for Socket.IO
5. Provide a single public IP/hostname for the entire stack

---

## 4. Phase 1 — Mic Controls + End-to-End Ingest Working (Local Dev)

**Goal:** An interpreter opens the portal, joins a booth, clicks "Go Live", speaks into their mic, and an HLS stream appears at a local URL.

### 4.1 Steps

#### Step 1 — Fix STUN Configuration (aiortc, interim)

While still on aiortc (before MediaMTX migration), add STUN URIs so WebRTC can work in LAN/local-network scenarios:

```python
# portal/ingest.py  — in _connect()
from aiortc import RTCConfiguration, RTCIceServer
config = RTCConfiguration(iceServers=[
    RTCIceServer(urls=["stun:stun.l.google.com:19302"])
])
peer_connection = RTCPeerConnection(configuration=config)
```

In `interpreter-booth.js` (vanilla frontend), the `RTCPeerConnection` constructor already accepts ICE config — add `STUN_SERVERS` env injection via a Flask template variable:

```js
const iceConfig = {
  iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
};
const pc = new RTCPeerConnection(iceConfig);
```

#### Step 2 — Serve HLS from Flask (dev only)

Add a route to Flask that serves the `hls-output/` directory with correct `Content-Type` and CORS:

```python
# app.py
from flask import send_from_directory

@app.route('/hls/<path:filename>')
def serve_hls(filename):
    response = send_from_directory(settings.INGEST_HLS_ROOT, filename)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Cache-Control'] = 'no-cache'
    return response
```

This gives interpreters a test URL: `http://localhost:5000/hls/<channel_id>/playlist.m3u8`

#### Step 3 — Audio Device Selection

Interpreters must be able to select which audio input device to use (headset mic, USB audio interface, etc.). Add device enumeration and a `<select>` dropdown to the booth page:

```js
// static/js/interpreter-booth.js  — device enumeration
async function populateMicDevices() {
  // Must call getUserMedia first to get permission before enumerateDevices labels appear
  const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  tempStream.getTracks().forEach(t => t.stop());

  const devices = await navigator.mediaDevices.enumerateDevices();
  const audioInputs = devices.filter(d => d.kind === 'audioinput');

  const select = document.getElementById('mic-device-select');
  select.innerHTML = '';
  for (const device of audioInputs) {
    const opt = document.createElement('option');
    opt.value = device.deviceId;
    opt.textContent = device.label || `Microphone ${select.options.length + 1}`;
    select.appendChild(opt);
  }
}

// When starting ingest, use the selected device
async function ensureMicStream() {
  const deviceId = document.getElementById('mic-device-select').value;
  state.micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      deviceId: deviceId ? { exact: deviceId } : undefined,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    }
  });
}
```

Also add a **mic level meter** (AnalyserNode → canvas bar) so the interpreter can confirm the selected device is picking up audio before going live.

```js
function startMicMeter(stream) {
  const ctx = new AudioContext();
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  source.connect(analyser);

  const data = new Uint8Array(analyser.frequencyBinCount);
  const canvas = document.getElementById('mic-meter');
  const canvasCtx = canvas.getContext('2d');

  function draw() {
    requestAnimationFrame(draw);
    analyser.getByteFrequencyData(data);
    const volume = data.reduce((a, b) => a + b, 0) / data.length;
    canvasCtx.clearRect(0, 0, canvas.width, canvas.height);
    canvasCtx.fillStyle = volume > 10 ? '#22c55e' : '#374151';
    canvasCtx.fillRect(0, 0, (volume / 128) * canvas.width, canvas.height);
  }
  draw();
}
```

#### Step 4 — End-to-End Validation

Local test sequence:
1. `uv run python app.py` (starts Flask on :5000)
2. Open `http://localhost:5000/interpreter/booth-1`
3. Join as Active interpreter
4. Click "Go Live" → browser asks mic permission → WebRTC connect fires
5. Check Flask logs: `IngestSession` created, `MediaRecorder` started
6. Open `http://localhost:5000/hls/booth-1/playlist.m3u8` → should return valid M3U8
7. Play in VLC or browser with HLS.js

#### Step 5 — Preflight Checklist (port to Jinja2 + plain JS)

The prototype Vue SPA contains a `PreflightChecklist.vue` component that can be used as a **reference for the logic**, but the implementation must be built fresh in the Jinja2 + plain JS frontend. Add a pre-join check screen in `templates/preflight.html` + `static/js/preflight.js`:

```js
// Before showing "Go Live", confirm mic access
async function checkMicPermission() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  stream.getTracks().forEach(t => t.stop());
  return true;
}
```

### 4.2 Phase 1 Success Criteria

- [ ] Interpreter can click "Go Live" and mic is captured (green indicator)
- [ ] HLS playlist appears at the dev URL within 4 seconds
- [ ] Mute/unmute toggles mic track `.enabled`
- [ ] "Pass Relay" sets next interpreter as active and stops current ingest
- [ ] HLS stream ends cleanly when "Stop" is clicked
- [ ] Mic device dropdown populates with all available audio inputs
- [ ] Selecting a different device and clicking "Go Live" uses that device
- [ ] Mic level meter responds visually to voice input

---

## 5. Phase 2 — Production Audio Server (MediaMTX)

**Goal:** Replace aiortc with MediaMTX for reliable, scalable audio ingest.

### 5.1 MediaMTX Path Design

Each booth-language pair maps to a **MediaMTX path** (a named stream):

```
Path name convention:  {event_slug}/{language_code}
Examples:
  pycon-2026/en
  pycon-2026/es
  pycon-2026/fr
```

This is stable, human-readable, and maps directly to Eventyay's event slug and language code.

### 5.2 MediaMTX Configuration (`mediamtx.yml`)

```yaml
# mediamtx.yml
logLevel: info
logDestinations: [stdout]

# WHIP ingest on :8889
webrtcAddress: :8889
webrtcEncryption: no

# Optional: built-in TURN server
webrtcICEServers2:
  - url: turn:mediamtx:3478
    username: eventyay
    password: ${TURN_PASSWORD}

# HLS output on :8888
hlsAddress: :8888
hlsEncryption: no
hlsAlwaysRemux: yes
hlsSegmentDuration: 2s
hlsSegmentMaxSize: 50MB
hlsPartDuration: 200ms  # Low-latency HLS (LHLS)

# Path config — all paths allowed (interpret portal enforces who can publish)
pathDefaults:
  record: no

# Per-event paths can be configured dynamically via MediaMTX API
# or left to default (any path auto-created on first publish)
```

### 5.3 Browser-Side Change (WHIP)

In `interpreter-booth.js`, replace the custom SDP endpoint call:

```js
// BEFORE (custom SDP exchange):
const res = await fetch(`/api/interpreter/connect/${channelId}`, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ type: offer.type, sdp: offer.sdp, ... })
});

// AFTER (WHIP standard):
const whipUrl = `${MEDIAMTX_WHIP_URL}/${eventSlug}/${languageCode}`;
const res = await fetch(whipUrl, {
  method: 'POST',
  headers: {
    'Content-Type': 'application/sdp',
    // Auth header enforced by MediaMTX path config or proxy
  },
  body: offer.sdp
});
const answerSdp = await res.text();
await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp });
```

The Flask `/api/interpreter/connect` endpoint is **kept** but now only updates booth state (marking ingest as connected) — it no longer does the WebRTC SDP negotiation itself.

### 5.4 Server-Side Change (Remove aiortc)

With MediaMTX handling WebRTC, `portal/ingest.py` (aiortc) is **removed from the critical path**. The Flask server only needs to:
1. Validate that the caller is the active interpreter for the booth
2. Return the WHIP URL for the correct path
3. Track ingest status via MediaMTX webhook callbacks

MediaMTX webhook example (called by MediaMTX on stream start/stop):
```python
@app.route('/hooks/mediamtx', methods=['POST'])
def mediamtx_hook():
    data = request.json
    # data: { event: 'read' | 'publish', path: 'pycon-2026/en', ... }
    path = data.get('path', '')
    # path format: {event_slug}/{language}
    # Update booth state accordingly
    ...
```

### 5.5 HLS URL Format

MediaMTX serves HLS at:
```
http://media-server:8888/{event_slug}/{language_code}/index.m3u8
```

Via Nginx reverse proxy (public URL):
```
https://media.your-domain.com/{event_slug}/{language_code}/index.m3u8
```

This URL is what gets stored in `StreamSchedule.url`.

### 5.6 Phase 2 Success Criteria

- [ ] `docker compose up` starts Flask portal + MediaMTX
- [ ] Interpreter clicks "Go Live" → browser POSTs WHIP to MediaMTX → RTP flows directly browser → MediaMTX
- [ ] HLS at `http://localhost:8888/{path}/index.m3u8` is playable
- [ ] aiortc removed from `pyproject.toml` dependencies
- [ ] Ingest survives interpreter browser refresh (MediaMTX keeps stream active, Flask reconnects)

---

## 6. Phase 3 — Multi-Booth, Multi-Event Data Model

**Goal:** Support `N` booths for `M` events simultaneously, with exactly one active audio stream per language per event.

### 6.1 Booth Identity Schema

A booth is identified by three coordinates:

```
{event_slug} / {language_code} / {booth_instance}
```

Where `booth_instance` exists for redundancy (Primary/Backup). At any time, **only one** booth instance per `{event_slug}/{language_code}` is the active publisher. The others are listening/standby.

MediaMTX path: `{event_slug}/{language_code}` (single active stream per language per event)  
Flask booth ID: `{event_slug}-{language_code}` (e.g. `pycon2026-en`, `pycon2026-es`)

### 6.2 Updated Booth State Model

The existing `BoothRegistry` in `booth_state.py` needs two additions:

```python
@dataclass
class Booth:
    booth_id: str
    language: str
    channel_id: str       # = MediaMTX path (event_slug/language_code)
    event_slug: str       # NEW — links booth to an Eventyay event
    room_id: int | None   # NEW — links to Eventyay Room FK for StreamSchedule update
    ...
```

The `channel_id` is the authoritative MediaMTX path. When the active interpreter goes live, only one `POST /whip/{channel_id}` is valid at a time — MediaMTX enforces single publisher per path by default.

### 6.3 Active-Interpreter Enforcement (Multi-Layer)

```
Layer 1: Flask booth_state.py     — only active interpreter role can set mic_active=True
Layer 2: Flask /api/booth route   — returns the WHIP URL only when caller is active interpreter
Layer 3: MediaMTX                 — rejects second WHIP publisher on same path (single publisher mode)
```

Three independent layers means no race condition can result in two live audio streams for the same language.

### 6.4 Booth Bootstrap Flow

```
1. Eventyay organiser creates a language channel for their event
   → calls Interpretation Portal API: POST /api/events/{event_slug}/booths
   → Portal creates Booth entry, returns booth_id and join URL

2. Interpreter navigates to: https://portal.domain/interpreter/{event_slug}/{language_code}
   → Portal serves the booth page, joined to the correct booth

3. Coordinator assigns active interpreter
   → Socket.IO booth:set-active event

4. Active interpreter clicks "Go Live"
   → Flask validates active role
   → Returns WHIP URL: {mediamtx_url}/whip/{event_slug}/{language_code}
   → Browser streams to MediaMTX

5. HLS becomes available at: {media_url}/{event_slug}/{language_code}/index.m3u8
```

### 6.5 Multi-Event Namespace Isolation

Each event gets its own namespace in MediaMTX paths. There is no cross-event interference. An organiser for `fosdem-2027` cannot see or affect paths for `pycon-2026`.

---

## 7. Phase 4 — Authentication, RBAC, and Admin Panel

**Goal:** Every user has a verified identity and scoped access. Admins configure the hierarchy; interpreters arrive via invite links and can only access their assigned booth.

### 7.1 Role Model

Five roles exist in the portal, each with distinct permissions:

| Role | Who | Can do |
|---|---|---|
| **Super Admin** | Portal operator | Create/delete events; manage all admins; view all booths |
| **Event Admin** | Conference organiser | Create/edit rooms and booths for their event; generate invite links; view all booths in their event |
| **Coordinator** | Booth technical lead | Assign active interpreter; view participant roster; send booth chat; cannot go live |
| **Interpreter** | Human interpreter | Join assigned booth; go live (if active); mute/unmute; pass relay; chat |
| **Listener** | Observer, trainee | Join booth audio-only; chat; cannot go live or assign roles |

Role is embedded in the invite token — not user-selectable at join time.

### 7.2 Database Model

The portal adds a small **SQLite database** (upgradeable to PostgreSQL for scale). Schema:

```
events
  id            TEXT PRIMARY KEY  -- matches Eventyay event_slug
  display_name  TEXT
  created_at    DATETIME
  admin_secret  TEXT              -- hashed, used to generate invite tokens

rooms
  id            TEXT PRIMARY KEY  -- e.g. "main-hall", "room-b"
  event_id      TEXT REFERENCES events(id)
  display_name  TEXT
  eventyay_room_id  INTEGER       -- FK to Eventyay Room, nullable

booths
  id            TEXT PRIMARY KEY  -- e.g. "pycon-2026-en"
  room_id       TEXT REFERENCES rooms(id)
  event_id      TEXT REFERENCES events(id)
  language_code TEXT              -- ISO 639-1, e.g. "en", "es", "fr"
  language_name TEXT              -- "English", "Spanish", "French"
  mediamtx_path TEXT              -- "{event_id}/{language_code}"
  hls_url       TEXT              -- public HLS URL when live, null otherwise
  created_at    DATETIME

invite_tokens
  token         TEXT PRIMARY KEY  -- cryptographically random, 32 bytes, hex
  booth_id      TEXT REFERENCES booths(id)
  role          TEXT              -- coordinator | interpreter | listener
  label         TEXT              -- "John Smith (EN interpreter)"
  expires_at    DATETIME          -- null = never expires
  used_at       DATETIME          -- null = not yet used; single-use optional
  created_by    TEXT              -- admin who issued it
```

SQLAlchemy or plain `sqlite3` with explicit SQL — no ORM framework overhead needed at this scale.

### 7.3 Token-Based Authentication Flow

Invite links encode the token directly in the URL:

```
https://portal.your-domain.com/join/{token}
```

**Flow:**

```
1. Event Admin creates a booth (pycon-2026 / Main Hall / English)
2. Admin generates an invite token for role=interpreter, label="John Smith"
3. Admin copies the invite link and sends it to John
4. John clicks: https://portal.your-domain.com/join/{token}
5. Flask validates token:
   - Not expired
   - Booth exists
   - (Optional: single-use check — mark used_at)
6. Flask issues a signed session cookie:
   { participant_id, booth_id, role, label, event_id }
   Signed with SECRET_KEY (itsdangerous.URLSafeTimedSerializer)
7. John is redirected to: /interpreter/{event_id}/{booth_id}
   The page loads with their role pre-set — no form to fill in
```

The session cookie expires after 24 hours. John can re-use his invite link to refresh (unless single-use mode is enabled).

**No passwords. No registration form. The invite link IS the credential.**

### 7.4 Admin Panel Routes

The admin panel is server-rendered Jinja2 (same as the interpreter console). No separate admin framework — plain Flask routes with a superadmin session guard.

```
GET  /admin/                              → Dashboard: event list + live booth status
GET  /admin/login                         → Superadmin login form (password from env var)
POST /admin/login                         → Sets superadmin session cookie

GET  /admin/events/                       → Event list
POST /admin/events/                       → Create event
GET  /admin/events/{event_id}/            → Event detail: rooms list, booth list, live status
POST /admin/events/{event_id}/delete      → Delete event (confirmation required)

GET  /admin/events/{event_id}/rooms/      → Room list for event
POST /admin/events/{event_id}/rooms/      → Create room
GET  /admin/events/{event_id}/rooms/{room_id}/   → Room detail: booth list
POST /admin/events/{event_id}/rooms/{room_id}/delete  → Delete room

GET  /admin/events/{event_id}/rooms/{room_id}/booths/        → Booth list
POST /admin/events/{event_id}/rooms/{room_id}/booths/        → Create booth (language_code, language_name)
GET  /admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/  → Booth detail:
                                                                      live status,
                                                                      HLS URL (copy button),
                                                                      participant roster,
                                                                      invite token list
POST /admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/tokens/  → Generate invite token
POST /admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/tokens/{token}/revoke  → Revoke token
```

**Admin Dashboard view example:**

```
Eventyay Interpretation Portal — Admin
─────────────────────────────────────────────────────────────────
Events (3)                                    [+ New Event]

  PyCon 2026
  ├─ Main Hall
  │   ├─ 🟢 English (pycon-2026/en)  [LIVE]  HLS: https://...  [Copy]
  │   ├─ 🟢 Spanish (pycon-2026/es)  [LIVE]  HLS: https://...  [Copy]
  │   └─ ⚪ French  (pycon-2026/fr)  [OFFLINE]
  └─ Workshop Room B
      └─ ⚪ German  (pycon-2026/de)  [OFFLINE]

  FOSDEM 2027
  └─ Janson Auditorium
      └─ ⚪ French  (fosdem-2027/fr)  [OFFLINE]
```

### 7.5 Generating Invite Links (Admin UI)

On the booth detail page, the admin sees a table of tokens:

```
Invite Tokens for: PyCon 2026 / Main Hall / English
──────────────────────────────────────────────────────────────────
Label               Role          Expires    Used      Actions
John Smith          interpreter   never      –         [Revoke] [Copy Link]
Maria Garcia        interpreter   never      –         [Revoke] [Copy Link]
Sarah Lee           coordinator   never      –         [Revoke] [Copy Link]
                                                        [+ Generate Token]
```

The "Copy Link" button copies `https://portal.domain/join/{token}` to clipboard. Admin sends it via email/Slack/etc.

### 7.6 RBAC Enforcement in Flask Routes

Every interpreter-facing and admin route is guarded:

```python
# portal/auth.py

from functools import wraps
from flask import session, abort, redirect, url_for

def require_role(*roles):
    """Decorator: ensure current session has one of the given roles."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            participant = session.get('participant')
            if not participant:
                return redirect(url_for('join_page'))
            if participant['role'] not in roles:
                abort(403)
            # Ensure the participant is accessing their own booth
            booth_id = kwargs.get('booth_id')
            if booth_id and participant['booth_id'] != booth_id:
                abort(403)
            return f(*args, **kwargs)
        return wrapper
    return decorator

def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return wrapper
```

Usage:

```python
@app.route('/interpreter/<event_id>/<booth_id>')
@require_role('interpreter', 'coordinator', 'listener')
def interpreter_console(event_id, booth_id):
    ...

# Only coordinators and active interpreters can call set-active
@socketio.on('booth:set-active')
def handle_set_active(data):
    participant = session.get('participant')
    if participant['role'] not in ('coordinator', 'interpreter'):
        emit('booth:error', {'message': 'Insufficient role'})
        return
    ...
```

### 7.7 Superadmin Password

The superadmin login uses a bcrypt-hashed password stored in the environment:

```bash
ADMIN_PASSWORD_HASH=<bcrypt hash of your password>
```

There is intentionally **only one** superadmin account. Event admins are managed via the same invite token system (role=`event_admin`, scoped to a specific event).

### 7.8 Phase 4 Success Criteria

- [ ] Admin can log in at `/admin/` with the configured password
- [ ] Admin can create Event → Room → Booth hierarchy via the UI
- [ ] Admin can generate an invite link for coordinator/interpreter/listener roles
- [ ] Clicking an invite link lands the user on the correct booth page with the correct role pre-set
- [ ] An interpreter with a Spanish booth token cannot access the English booth
- [ ] A listener cannot trigger "Go Live" — the button is absent from their UI
- [ ] Expired or revoked tokens return a 403 with a clear message
- [ ] Superadmin can revoke any token from the admin panel

---

## 8. Phase 5 — Interpreter Console UX (Google Meet-style)

**Goal:** The interpreter's booth page feels like a professional conferencing tool — not a debug panel. Controls are large, accessible, clearly labelled, and respond immediately. The interpreter can focus entirely on their work.

### 8.1 Layout

Single-page layout with four regions:

```
┌─────────────────────────────────────────────────────────────────────┐
│  HEADER: Eventyay • PyCon 2026 • Main Hall / English                │
│  Status: 🟢 LIVE  |  Participants: 3  |  [Leave Booth]              │
├────────────────────────────┬────────────────────────────────────────┤
│  LEFT PANEL                │  RIGHT PANEL                           │
│                            │                                        │
│  ┌──────────────────────┐  │  ┌────────────────────────────────┐   │
│  │  Jitsi Monitor Feed  │  │  │  Participants                  │   │
│  │  (floor audio/video) │  │  │  ● John Smith  (active) 🎤     │   │
│  │  [iframe]            │  │  │  ○ Maria Garcia (backup)       │   │
│  └──────────────────────┘  │  │  ◈ Sarah Lee (coordinator)     │   │
│                            │  └────────────────────────────────┘   │
│  ┌──────────────────────┐  │                                        │
│  │  Booth Chat          │  │  ┌────────────────────────────────┐   │
│  │  [internal comms]    │  │  │  Connection Health             │   │
│  └──────────────────────┘  │  │  WebRTC: ● connected           │   │
│                            │  │  MediaMTX: ● streaming         │   │
│                            │  │  HLS delay: ~2.1s              │   │
│                            │  └────────────────────────────────┘   │
├────────────────────────────┴────────────────────────────────────────┤
│  BOTTOM CONTROL BAR  (always visible, fixed)                        │
│                                                                      │
│  [🎤 Mic: OFF  ▼ Headset Mic (USB)] [████░░░░] level meter         │
│                                                                      │
│  [  🔴 GO LIVE  ]   [  ⏸ MUTE  ]   [  ↔ PASS RELAY  ]            │
│                                                                      │
│  Role: Active Interpreter                                            │
└─────────────────────────────────────────────────────────────────────┘
```

### 8.2 Control Bar Behaviour

#### Mic Device Selector

A `<select>` dropdown with a microphone icon shows the currently selected audio input:

```
[🎤  Headset Mic (USB)  ▼]
```

- Populated on page load via `navigator.mediaDevices.enumerateDevices()`
- Re-populated on `devicechange` event (user plugs/unplugs a device)
- Disabled while ingest is live (cannot switch device mid-stream — user must Stop first)
- Stores selection in `localStorage` so it persists across page reloads

#### Mic Level Meter

A thin animated bar to the right of the device selector shows real-time input level using `AnalyserNode`. It works whenever a mic stream is acquired — even before going live — so the interpreter can confirm their mic is working.

- Green = audio detected
- Red = clipping (volume > 95%)
- Grey = no stream / muted

#### Go Live / Stop Button

The primary call-to-action. Large, full-width in mobile:

```
State: not live    →   [  🔴 GO LIVE  ]   (red accent, pulsing outline)
State: live        →   [  ⬛ STOP     ]   (dark, solid)
State: not active  →   button is hidden entirely (role enforcement)
```

Clicking "Go Live":
1. Shows inline spinner: "Connecting…"
2. Acquires mic stream with selected device
3. Performs WebRTC WHIP handshake
4. On success: button becomes "STOP", status chip becomes "🟢 LIVE"
5. On failure: shows inline error with retry button

#### Mute / Unmute Button

Toggles `track.enabled` on the active mic stream. Does **not** stop the WebRTC connection — the stream continues, just silenced at the source.

```
State: unmuted  →  [  🎙 UNMUTE  ]   (green)
State: muted    →  [  🔇 MUTED   ]   (amber, pulsing)
```

Keyboard shortcut: `Space` while the control bar has focus toggles mute (standard broadcast console convention). A visible keyboard shortcut hint is shown below the button.

#### Pass Relay Button

Hands off the Active role to the next interpreter in the roster. Before executing, shows a confirmation modal:

```
┌─────────────────────────────────────────┐
│  Pass relay to Maria Garcia?            │
│  Your ingest will stop immediately.     │
│                                         │
│  [Cancel]          [Confirm Handoff]    │
└─────────────────────────────────────────┘
```

Only visible to coordinators and the active interpreter.

### 8.3 Status Indicators

Every connection state is visible without needing to open dev tools:

| Indicator | Location | States |
|---|---|---|
| Stream status | Header chip | ⚫ Offline / 🟡 Connecting / 🟢 Live |
| WebRTC connection | Health panel | new / checking / connected / disconnected / failed |
| MediaMTX stream | Health panel | No stream / Publishing / Error |
| Mic input | Level meter bar | Grey / Green / Red (clip) |
| Role | Bottom bar | Active / Backup / Coordinator / Listener |
| Booth participants | Participant list | Name + role + mic-active indicator per person |

### 8.4 Accessibility

- All interactive elements are keyboard-navigable with visible focus rings
- Status changes are announced via `aria-live` regions (screen-reader compatible)
- Button labels never rely on colour alone — icons + text labels used together
- Minimum touch target size: 44×44px (mobile interpreters on tablets)
- High-contrast mode: the CSS uses CSS custom properties (`--color-*`) so theming is clean

### 8.5 Preflight Checklist

Before joining a booth, the interpreter sees a preflight check screen:

```
Before you go live — quick checks
──────────────────────────────────────────────
✅  Microphone permission granted
✅  Audio device detected: Headset Mic (USB)
✅  Ingest server reachable (200 OK)
⚠️  No active floor audio feed (Jitsi URL not set)

[  Continue to Booth  ]
```

Each check runs automatically. The "Continue to Booth" button is disabled until mic permission and ingest reachability pass.

### 8.6 CSS Architecture

All styles in `static/css/interpreter.css` using CSS custom properties. No CSS framework. Dark mode supported via `prefers-color-scheme`:

```css
:root {
  --color-primary: #6366f1;    /* indigo */
  --color-live:    #ef4444;    /* red — "on air" */
  --color-muted:   #f59e0b;    /* amber */
  --color-success: #22c55e;    /* green */
  --color-surface: #1e1e2e;
  --color-text:    #cdd6f4;
  --radius-btn:    8px;
  --font-mono:     'JetBrains Mono', 'Fira Code', monospace;
}

@media (prefers-color-scheme: light) {
  :root {
    --color-surface: #ffffff;
    --color-text:    #1e1e2e;
  }
}
```

### 8.7 Phase 5 Success Criteria

- [ ] Interpreter sees mic device dropdown populated with all available inputs
- [ ] Selecting a device and speaking shows level meter responding
- [ ] "Go Live" button only visible to active interpreter; hidden for backup/listener
- [ ] Go Live → Connecting → Live state transitions are visually clear
- [ ] Mute button toggles visually + functionally; Space key shortcut works
- [ ] Pass Relay shows confirmation modal before executing
- [ ] Status indicators update in real time without page reload
- [ ] Preflight checklist passes / fails each check independently
- [ ] Page is usable on a tablet (iOS Safari, Android Chrome)

---

## 9. Phase 6 — Eventyay Integration (Audio Links)

**Goal:** Wire the portal's HLS stream URLs into Eventyay's existing stream schedule UI so that audience members on the Eventyay viewer page can select an interpretation language. This phase is **entirely optional** \u2014 the portal functions without it; the HLS URLs it produces are usable in any player or platform regardless of whether Eventyay integration is configured.

**Note on platform independence:** `StreamSchedule.url` already accepts an `hls` stream type. The interpretation portal provides a valid HLS URL at `https://media.your-domain.com/{event}/{lang}/index.m3u8`. There is nothing Eventyay-specific about this URL \u2014 it is a standard HLS manifest that any player can consume.

### 9.1 How Eventyay Currently Shows Stream URLs

1. Organiser creates a `StreamSchedule` entry for a room/time-slot with `url` + `stream_type`.
2. The schedule data builder injects `stream_url` into talk JSON.
3. The viewer page receives the URL and plays it.
4. Django Channels broadcasts `stream.change` when the active stream switches.

For the `stream_type='hls'` case, an `<audio>` element (not `<video>`) with HLS.js is the viewer's playback surface.

### 9.2 StreamSchedule Extension for Multi-Language Audio

The `config` JSONField on `StreamSchedule` is the designated extension point. No schema migration is needed for the portal integration — we store interpretation track URLs there:

```json
{
  "interpretation_tracks": {
    "en": "https://media.domain/pycon-2026/en/index.m3u8",
    "es": "https://media.domain/pycon-2026/es/index.m3u8",
    "fr": "https://media.domain/pycon-2026/fr/index.m3u8"
  }
}
```

The main video stream (`url` field) remains the primary video source (e.g. Jitsi or YouTube feed).

The Eventyay viewer page reads `config.interpretation_tracks` and renders a **language selector** + hidden `<audio>` element (synced to the main video clock). This is the viewer synchronisation loop described in `ARCHITECTURE.md § 4`.

### 9.3 Portal → Eventyay API Bridge

When a booth goes live, the interpretation portal calls the Eventyay REST API to register the HLS URL:

```
PATCH /api/v1/organizer/{organizer}/events/{event}/rooms/{room}/stream-schedules/{id}/
Content-Type: application/json
Authorization: Token {EVENTYAY_API_TOKEN}

{
  "config": {
    "interpretation_tracks": {
      "en": "https://media.domain/pycon-2026/en/index.m3u8"
    }
  }
}
```

This call is made by the portal's MediaMTX webhook handler when a new stream starts publishing on a path.

**Alternatively (simpler for Phase 4):** The organiser manually pastes the HLS URL into the Eventyay admin for the `StreamSchedule` entry. This removes the need for portal → Eventyay API integration in Phase 4 and can be automated in a later phase.

### 9.4 Viewer-Side Language Selector (Eventyay)

A new Django template partial is added to the talk/schedule viewer page:

```html
{% if stream_schedule.config.interpretation_tracks %}
<div class="interpretation-selector">
  <label>🎧 Interpretation language:</label>
  <select id="lang-select">
    <option value="">— Original (floor) —</option>
    {% for lang, url in stream_schedule.config.interpretation_tracks.items %}
      <option value="{{ url }}">{{ lang|upper }}</option>
    {% endfor %}
  </select>
</div>
<audio id="interp-audio" hidden></audio>
{% endif %}
```

JavaScript (no jQuery; plain ES module — no build step required in this repo):
```js
// static/eventyay/js/interpretation-player.js
import Hls from 'https://cdn.skypack.dev/hls.js';

const select = document.getElementById('lang-select');
const audio = document.getElementById('interp-audio');
let hls = null;

select.addEventListener('change', () => {
  const url = select.value;
  if (!url) {
    audio.pause();
    if (hls) { hls.destroy(); hls = null; }
    return;
  }
  if (Hls.isSupported()) {
    if (hls) hls.destroy();
    hls = new Hls({ lowLatencyMode: true });
    hls.loadSource(url);
    hls.attachMedia(audio);
    hls.on(Hls.Events.MANIFEST_PARSED, () => audio.play());
  } else if (audio.canPlayType('application/vnd.apple.mpegurl')) {
    audio.src = url;
    audio.play();
  }
});
```

**Note:** Viewer drift correction (syncing HLS audio to the primary video clock) is deferred to a post-Phase-4 optimisation. It is fully designed in `ARCHITECTURE.md § 4`.

### 9.5 Phase 6 Success Criteria

- [ ] Organiser adds HLS URL to `StreamSchedule.config.interpretation_tracks` in admin
- [ ] Viewer page shows language selector dropdown when interpretation tracks exist
- [ ] Selecting a language starts playback of the correct HLS audio stream
- [ ] Selecting "Original" stops interpretation audio
- [ ] Works in Chrome, Firefox, Safari (native HLS on Safari)

---

## 10. Phase 7 — Docker Compose Deployment

**Goal:** Single `docker-compose.yml` that runs the complete interpretation stack. The main Eventyay platform has its own `docker-compose.yml` — the interpretation portal is a **separate compose stack** that connects to Eventyay's network.

### 10.1 Services

```
┌─────────────────────────────────────────────────────────────────┐
│  Interpretation Compose Stack (docker-compose.interpretation.yml)│
│                                                                   │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────────────┐ │
│  │  portal      │   │  mediamtx    │   │  redis               │ │
│  │  (Flask)     │   │  (media svc) │   │  (state/pubsub)      │ │
│  │  :5000       │   │  :8888 HLS   │   │  :6379               │ │
│  │              │   │  :8889 WHIP  │   └──────────────────────┘ │
│  └──────────────┘   │  :3478 TURN  │                             │
│                     └──────────────┘                             │
│  ┌──────────────────────────────────────────────────────────────┐│
│  │  nginx (reverse proxy + CORS + TLS termination)  :80 / :443 ││
│  └──────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
         ↕ (Eventyay API calls via token auth)
┌─────────────────────────────────────┐
│  Eventyay Main Stack                │
│  (separate compose, existing)       │
└─────────────────────────────────────┘
```

### 10.2 `docker-compose.interpretation.yml`

```yaml
version: "3.9"

services:

  # ── Flask coordination portal ────────────────────────────────
  portal:
    build:
      context: .
      dockerfile: Dockerfile
    env_file: .env
    environment:
      - FLASK_DEBUG=false
      - REDIS_URL=redis://redis:6379/0
      - MEDIAMTX_URL=http://mediamtx:8889
      - MEDIAMTX_WHIP_BASE=http://mediamtx:8889/whip
      - MEDIAMTX_HLS_BASE=http://mediamtx:8888
    ports:
      - "5000:5000"
    depends_on:
      - redis
      - mediamtx
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/healthz"]
      interval: 15s
      timeout: 5s
      retries: 3

  # ── MediaMTX audio server ─────────────────────────────────────
  mediamtx:
    image: bluenviron/mediamtx:latest
    volumes:
      - ./mediamtx.yml:/mediamtx.yml:ro
    ports:
      - "8888:8888"   # HLS
      - "8889:8889"   # WHIP (WebRTC HTTP ingest)
      - "3478:3478/udp"  # TURN (UDP)
      - "3478:3478/tcp"  # TURN (TCP)
      - "50000-50100:50000-50100/udp"  # RTP/RTCP port range
    environment:
      - MTX_WEBRTCICEUDPMUXADDRESS=:8189
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8888/"]
      interval: 15s
      timeout: 5s
      retries: 3

  # ── Redis (Socket.IO pubsub + booth state) ────────────────────
  redis:
    image: redis:7-alpine
    command: redis-server --appendonly yes
    volumes:
      - redis-data:/data
    ports:
      - "6379:6379"
    restart: unless-stopped

  # ── Nginx (TLS, CORS, reverse proxy) ─────────────────────────
  nginx:
    image: nginx:alpine
    volumes:
      - ./nginx/interpretation.conf:/etc/nginx/conf.d/default.conf:ro
      - ./nginx/certs:/etc/nginx/certs:ro  # TLS certs (Let's Encrypt via certbot or pre-provisioned)
    ports:
      - "80:80"
      - "443:443"
    depends_on:
      - portal
      - mediamtx
    restart: unless-stopped

volumes:
  redis-data:
```

### 10.3 Nginx Configuration (`nginx/interpretation.conf`)

```nginx
# Portal — booth coordination console
server {
    listen 443 ssl http2;
    server_name portal.your-domain.com;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    location / {
        proxy_pass http://portal:5000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";  # WebSocket / Socket.IO
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 86400;  # Long-lived Socket.IO connections
    }
}

# Media server — HLS streams (read by audience)
server {
    listen 443 ssl http2;
    server_name media.your-domain.com;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    location / {
        proxy_pass http://mediamtx:8888;
        proxy_set_header Host $host;

        # CORS — allow any origin to fetch HLS (Eventyay, third-party embeds, standalone players)
        # Restrict to specific origins in production if needed (e.g., "https://your-eventyay-domain.com")
        add_header Access-Control-Allow-Origin "*" always;
        add_header Access-Control-Allow-Methods "GET, HEAD, OPTIONS" always;
        add_header Cache-Control "no-cache" always;
        if ($request_method = OPTIONS) {
            return 204;
        }
    }

    # WHIP ingest — interpreter browsers POST their SDP offer here
    # NOTE: Both HLS (/) and WHIP (/whip/) are in the same server block.
    # Two server blocks with the same server_name would cause Nginx to silently
    # ignore one of them — always merge locations under a single block.
    location /whip/ {
        proxy_pass http://mediamtx:8889/whip/;
        proxy_set_header Host $host;
        # CORS for browser WHIP POST (interpreters may be on any origin)
        add_header Access-Control-Allow-Origin "*" always;
        add_header Access-Control-Allow-Headers "Content-Type, Authorization" always;
        if ($request_method = OPTIONS) {
            return 204;
        }
    }
}
```

### 10.4 Portal `Dockerfile`

```dockerfile
FROM python:3.13-slim

WORKDIR /app

# Install uv
RUN pip install uv

# Copy dependency files first (layer cache)
COPY pyproject.toml uv.lock ./

# Install dependencies (no aiortc — removed in Phase 2)
RUN uv sync --no-dev

# Copy application code
COPY . .

EXPOSE 5000

CMD ["uv", "run", "gunicorn", \
     "--worker-class", "eventlet", \
     "--workers", "1", \
     "--bind", "0.0.0.0:5000", \
     "app:app"]
```

> **Note:** Flask-SocketIO in production mode requires either `eventlet` or `gevent` workers. `async_mode='threading'` (current) must be changed to `async_mode='eventlet'` when switching from dev to production.

### 10.5 Environment Variables (`.env.example` additions)

```bash
# Portal
FLASK_DEBUG=false
SECRET_KEY=<random-256-bit-key>
BOOTH_ACCESS_TOKEN=<shared-secret-for-interpreter-auth>
REDIS_URL=redis://redis:6379/0

# MediaMTX integration
MEDIAMTX_WHIP_BASE=https://media.your-domain.com/whip
MEDIAMTX_HLS_BASE=https://media.your-domain.com
TURN_PASSWORD=<random-password>

# Eventyay API integration (Phase 4)
EVENTYAY_API_BASE=https://your-eventyay-domain.com
EVENTYAY_API_TOKEN=<organiser-api-token>

# Jitsi
JITSI_DOMAIN=meet.jit.si
DEFAULT_JITSI_ROOM=eventyay-floor
```

### 10.6 Phase 7 Success Criteria

- [ ] `docker compose -f docker-compose.interpretation.yml up -d` starts all services cleanly
- [ ] Healthchecks pass for portal, mediamtx, redis, nginx
- [ ] Admin console accessible at `https://portal.your-domain.com/admin`
- [ ] Interpreter invite link works end-to-end: click link → token validated → booth page loads
- [ ] WHIP POST to `https://media.your-domain.com/whip/{path}` succeeds from browser
- [ ] HLS stream playable at `https://media.your-domain.com/{path}/index.m3u8`
- [ ] Container restart (portal) does not lose active MediaMTX streams
- [ ] Redis persists booth state across portal restarts

---

## 11. Full System Architecture (Final State)

```
                   ┌─────────────────────────────────────┐
                   │     EVENTYAY MAIN PLATFORM           │
                   │  (existing Docker Compose)           │
                   │                                       │
                   │  Django + PostgreSQL + Redis          │
                   │  StreamSchedule:                      │
                   │    url: <main stream>                 │
                   │    config.interpretation_tracks: {    │
                   │      en: https://media.d/pycon/en/... │
                   │      es: https://media.d/pycon/es/... │
                   │    }                                  │
                   │                                       │
                   │  Viewer page:                         │
                   │   [Main video] + [🎧 Lang selector]  │
                   └─────────────────┬───────────────────┘
                                     │ HLS audio fetch
                                     │ (CORS allowed)
                   ┌─────────────────▼───────────────────┐
                   │     NGINX (media.your-domain.com)     │
                   └──────────┬──────────────┬────────────┘
                              │              │
               HLS :8888      │              │ WHIP :8889
                   ┌──────────▼──────────────▼────────────┐
                   │          MEDIAMTX                     │
                   │  path: pycon-2026/en  → HLS + TURN   │
                   │  path: pycon-2026/es  → HLS + TURN   │
                   │  (single publisher per path enforced) │
                   └─────────────────────────────────────┘
                              ▲ WHIP (WebRTC / Opus RTP)
                              │
         ┌────────────────────┴──────────────────────────────┐
         │           INTERPRETER BROWSERS                     │
         │  ┌──────────────────────┐  ┌──────────────────────┐│
         │  │  Booth: EN           │  │  Booth: ES           ││
         │  │  Active interpreter  │  │  Active interpreter  ││
         │  │  (mic live)          │  │  (mic live)          ││
         │  └──────────────────────┘  └──────────────────────┘│
         │  ┌──────────────────────┐                          │
         │  │  Booth: EN           │                          │
         │  │  Backup interpreter  │                          │
         │  │  (monitoring Jitsi)  │                          │
         │  └──────────────────────┘                          │
         └────────────────────────────────────────────────────┘
                              ▲ Socket.IO (coordination)
                              │
                   ┌──────────┴───────────────────┐
                   │   FLASK PORTAL (:5000)        │
                   │   booth_state.py              │
                   │   + Redis pubsub              │
                   │   + /api/... routes           │
                   └───────────────────────────────┘
```

### 11.1 Data Flow Summary

| Step | Actor | Action |
|---|---|---|
| 1 | Organiser | Creates interpretation booths via portal admin UI |
| 2 | Interpreter | Opens `https://portal.domain/interpreter/{event}/{lang}` |
| 3 | Interpreter | Joins booth → Socket.IO `booth:join` |
| 4 | Coordinator | Sets active interpreter → `booth:set-active` |
| 5 | Active interpreter | Clicks "Go Live" |
| 6 | Browser | `POST https://media.domain/whip/{event}/{lang}` with SDP offer |
| 7 | MediaMTX | Returns SDP answer, accepts Opus RTP stream |
| 8 | MediaMTX | Begins writing HLS segments |
| 9 | Flask webhook | Receives MediaMTX `publish` event, updates booth state |
| 10 | Organiser | Adds `https://media.domain/{event}/{lang}/index.m3u8` to Eventyay StreamSchedule |
| 11 | Viewer | Opens event page, sees language selector, picks language |
| 12 | Browser (viewer) | Fetches HLS playlist, starts playing interpretation audio |

---

## 12. Open Questions and Deferred Decisions

### 12.1 Authentication for WHIP

MediaMTX supports `publishUser` / `publishPass` per path, but passing credentials in the WHIP request from the browser is non-standard. **Options:**
- A. Pre-signed short-lived URL token (Flask issues a one-time token, passed as WHIP URL query param)
- B. Bearer token in `Authorization` header of the WHIP POST (supported by MediaMTX 1.x+)
- C. Rely on `BOOTH_ACCESS_TOKEN` in Flask route, and trust that only the portal page is used

**Recommendation:** Option B — Flask `/api/booth/{booth_id}/whip-token` returns a short-lived JWT, browser includes it as `Authorization: Bearer <token>` in the WHIP POST. MediaMTX validates via its `publishPass` external auth webhook.

### 12.2 Low-Latency HLS vs Standard HLS

Standard HLS with 2s segments produces 6–8s of end-to-end latency (3–4 segments in buffer). For simultaneous interpretation, the target latency is **< 3 seconds** to track the speaker.

**Options:**
- **LL-HLS (Low-Latency HLS):** MediaMTX 1.x supports it (`hlsPartDuration: 200ms`). Reduces latency to ~1–2s. Browser HLS.js 1.x supports LL-HLS. **Recommended.**
- **WebRTC WHEP** for viewers: Gives < 500ms but requires WebRTC client on the viewer side. More complex, defer to post-MVP.

### 12.3 Redis vs In-Memory State

For a single-instance deployment (one portal container), in-memory `BoothRegistry` is sufficient. Redis is needed only when:
- Multiple portal replicas run behind a load balancer
- Portal containers restart and state must survive

**Phase 1–3:** Keep in-memory state.  
**Phase 5:** Add Redis, migrate `BoothRegistry` to store snapshots in Redis hashes.

### 12.4 Eventyay API Integration Automation

Phase 4 describes manual URL registration. A future enhancement is:
1. Portal calls Eventyay `PATCH /api/.../stream-schedules/{id}/` when a stream goes live
2. Portal receives Eventyay event slug and room ID at booth creation time
3. This closes the loop fully: organiser creates interpretation channels, everything else is automatic

This requires organiser-level API token scoped to the event.

### 12.5 Recording / Archive

MediaMTX supports recording streams to disk (MP4 or HLS VOD). After the event, recordings can be:
- Uploaded to Eventyay's `Media Library` (the Venueless-origin feature)
- Stored as post-event interpreted track assets

This is fully deferred — not needed for MVP.

### 12.6 Fallback When Active Interpreter Drops

Currently: if the active interpreter disconnects mid-session, the WebRTC connection to MediaMTX drops. The HLS stream stalls and eventually the `#EXT-X-ENDLIST` marker is written.

**Needed for production reliability:**
- Backup interpreter is pre-selected and receives a "take over" prompt when the primary drops
- Socket.IO `booth:state` update triggers `startLiveIngest()` on the new active interpreter's browser automatically
- MediaMTX accepts the new WHIP publisher on the same path (after old connection times out, ~10s)

This is partially designed in `booth_state.py` (`leave_participant` already picks next interpreter). The browser side needs auto-trigger on role change.

---

## Appendix: Implementation Order Summary

| Phase | What | Key deliverable |
|---|---|---|
| **Phase 1** | Fix STUN, serve HLS locally, audio device selection + mic meter | Local: mic → HLS playable URL |
| **Phase 2** | Replace aiortc with MediaMTX + WHIP browser ingest | Reliable WebRTC through NAT |
| **Phase 3** | Add event_slug / room_id to booth model, multi-event namespace | N booths for M events in isolation |
| **Phase 4** | SQLite DB, invite token auth, RBAC, Admin → Events → Rooms → Booths UI | Secure, role-scoped access via invite links |
| **Phase 5** | Interpreter console UX: Google Meet-style controls, preflight, dark mode | Professional interpreter experience |
| **Phase 6** | Eventyay viewer language selector + StreamSchedule config hook | Audience can switch to interpreted audio |
| **Phase 7** | Docker Compose: portal + mediamtx + redis + nginx | `docker compose up` → production |
| **Post-MVP** | LL-HLS (< 2s latency), auto WHIP auth, Eventyay API automation, recording | Performance + UX polish |

## Frontend Stack

The active frontend is **vanilla JS ES modules + Jinja2 templates**. No build step, no bundler, no framework. All new UI goes in `templates/` + `static/js/` + `static/css/`.

The `src/` directory (Vue 3 / Vite prototype) remains in-tree but is **not connected to the Flask backend and is not used in production**. It will be removed in a dedicated cleanup PR. The presence of `package.json` and `vite.config.js` in the repo reflects this legacy prototype, not the active stack.

## Platform Independence

The interpretation portal is a **standalone product**. It operates independently of Eventyay and produces standard HLS stream URLs as output. These URLs can be:

- Added to Eventyay's `StreamSchedule` (Phase 6 of this plan)
- Embedded in any other event platform that accepts an HLS URL
- Played directly in any HLS-capable player (VLC, QuickTime, browser + hls.js)
- Shared as a raw link for monitoring or recording

Eventyay integration is a consumer of the portal's output, not a dependency of its operation. The portal has no compile-time or runtime coupling to the main Eventyay codebase.
