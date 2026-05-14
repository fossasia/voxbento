# Frontend Architecture

The frontend is a Vue 3 single-page application built with Vite. It is served from Flask in production and from the Vite dev server during development.

---

## Technology stack

| Technology | Version | Role |
|---|---|---|
| Vue 3 | 3.x | Component framework (Composition API) |
| Vite | 5.x | Build tool and dev server |
| Vue Router | 4.x | Client-side routing |
| Socket.IO client | 4.x | Realtime booth communication |
| WebRTC (native browser API) | — | Microphone ingest |
| Web Audio API (native) | — | Level meter |
| BroadcastChannel API (native) | — | Cross-tab coordination |

There is no global state management library (no Pinia, no Vuex). All shared state lives in the `useInterpreterBooth` composable.

---

## Directory structure

```
src/
├── App.vue                          # Root component; renders <RouterView>
├── main.js                          # App bootstrap; creates Vue app and mounts
├── config/
│   └── env.js                       # Environment variable access (VITE_* vars)
├── router/
│   └── index.js                     # Route definitions
├── views/
│   └── InterpreterConsoleView.vue   # The single console view
├── composables/
│   └── useInterpreterBooth.js       # Central state machine and event wiring
├── services/
│   ├── ingestClient.js              # WebRTC SDP negotiation
│   ├── micStreamingManager.js       # getUserMedia, level meter, peer connection
│   ├── jitsiEmbed.js                # Jitsi URL parsing and embed URL construction
│   └── boothRealtime.js             # Socket.IO + BroadcastChannel transport
├── components/
│   ├── JitsiMonitorPanel.vue        # Jitsi iframe embed and join controls
│   ├── MicIngestPanel.vue           # Device selector, level meter, Go Live / Stop
│   ├── PreflightChecklist.vue       # Checklist gating Go Live
│   ├── ParticipantGrid.vue          # Booth participant list
│   ├── BoothChatPanel.vue           # Internal booth chat
│   ├── BoothHealthPanel.vue         # Live ingest health
│   └── common/                      # Shared UI primitives
└── styles/
    └── main.css                     # Global CSS and CSS custom properties
```

---

## Routing

The console has a single route:

```
/interpreter/:eventSlug/:boothId
```

Route params are read by `InterpreterConsoleView` on mount and passed to `useInterpreterBooth.initialize({ eventSlug, boothId })`.

A redirect from `/` to a default booth URL is handled by Flask, not the Vue router.

---

## `useInterpreterBooth` composable

This is the central state machine for the interpreter console. It owns:

- `state` — reactive object containing all UI state (session metadata, Jitsi status, mic status, ingest status, preflight flags, participant list, chat messages)
- `health` — computed object derived from `state` for the health panel
- All actions: `initialize`, `teardown`, `joinJitsi`, `setAudioDevice`, `runMicTest`, `startInterpretation`, `stopInterpretation`, `setActiveInterpreter`, `toggleChecklistItem`, `sendBoothChatMessage`

The composable creates and manages three service instances:

| Service | Purpose |
|---|---|
| `IngestClient` | POST SDP offer to the ingest API |
| `MicStreamingManager` | Manage getUserMedia stream and peer connection |
| `BoothRealtimeClient` | Manage Socket.IO and BroadcastChannel connections |

### State shape

```js
state = reactive({
  initialized: false,
  localParticipantId: String,
  localRole: String,
  session: {
    eventSlug, boothId, language, channelId
  },
  jitsi: {
    inputUrl, embedUrl, roomName, status, error
  },
  mic: {
    status,          // 'idle' | 'testing' | 'ready' | 'error'
    level,           // 0–1 float
    devices,         // MediaDeviceInfo[]
    selectedDeviceId
  },
  ingest: {
    status,          // 'disconnected' | 'connecting' | 'connected' | 'error'
    connectionState, // RTCPeerConnection.connectionState
    reconnecting,
    retries,
    streamingLive,
    bitrateKbps,
    error
  },
  preflight: {
    headphonesConnected,
    monitoringActive,
    micTestComplete,
    ingestReachable
  },
  participants: Participant[],
  activeParticipantId: String,
  chatMessages: ChatMessage[]
})
```

### Preflight gate

The **Go Live** button is disabled until all four preflight items are satisfied:

- `headphonesConnected` — manually checked by interpreter
- `monitoringActive` — set when Jitsi iframe loads successfully
- `micTestComplete` — set after mic test level is confirmed
- `ingestReachable` — set after `IngestClient.checkReachable()` returns true

---

## Service layer

### `IngestClient`

Handles the HTTP layer for WebRTC ingest:

- `checkReachable(channelId)` — OPTIONS request to verify the ingest endpoint is up
- `negotiate(channelId, localDescription)` — POST SDP offer, receive SDP answer

Constructed with `{ baseUrl, authToken }` from `env.ingestBaseUrl` and `env.ingestAuthToken`.

### `MicStreamingManager`

Manages the browser audio pipeline:

- `listInputDevices()` — enumerates audioinput devices
- `startMicrophone(deviceId, onLevel)` — getUserMedia + level meter
- `createIngestConnection(onConnectionStateChange)` — creates RTCPeerConnection, adds track, creates offer, gathers ICE, returns local description
- `applyRemoteAnswer(answer)` — sets remote description on the peer connection
- `collectStats(onStats)` — polls getStats() for bitrate
- `stopMeter()` — cancels animation frame loop
- `stopTracks()` — stops all MediaStreamTrack instances
- `stopPeerConnection()` — closes RTCPeerConnection

### `buildJitsiEmbedUrl` / `parseJitsiMeetingUrl`

Pure functions for Jitsi URL handling. No side effects.

### `BoothRealtimeClient`

Manages two realtime transports:

1. **BroadcastChannel** — for cross-tab coordination within the same browser origin
2. **WebSocket** — for multi-machine coordination (optional; requires `VITE_BOOTH_WS_URL`)

Both transports deliver event envelopes of the shape:

```js
{
  eventType: 'booth-chat' | 'active-interpreter' | 'participant-state',
  payload: { ... },
  sentAt: timestamp
}
```

Chat messages are also persisted to `localStorage` so they survive page refreshes.

---

## Component responsibilities

### `InterpreterConsoleView`

Top-level layout using CSS Grid:

```
grid-template-rows: topbar | console-layout | participant-grid | chat-panel
```

The `console-layout` row uses a two-column grid:
- Left: `JitsiMonitorPanel` (2fr)
- Right: sidebar (`MicIngestPanel` + `PreflightChecklist` + `BoothHealthPanel`) (1fr, min 300px)

Below 1080px viewport width, the layout collapses to a single column.

### `JitsiMonitorPanel`

- Renders a URL input pre-filled with `state.jitsi.inputUrl`
- Emits `join` when the interpreter clicks Join
- Renders the Jitsi `<iframe>` once `state.jitsi.embedUrl` is set
- Emits `loaded` when the iframe fires `load`

### `MicIngestPanel`

- Renders a `<select>` for audio device selection
- Renders a level meter bar driven by `state.mic.level`
- Renders **Test Mic**, **Go Live**, and **Stop** buttons
- The Go Live button is disabled unless all preflight items are complete and the local participant is the active interpreter
- Emits `set-device`, `mic-test`, `start`, `stop`

### `PreflightChecklist`

- Renders four checkbox items from `state.preflight`
- `headphonesConnected` is manually toggled by the interpreter
- `monitoringActive`, `micTestComplete`, `ingestReachable` are set programmatically
- Emits `toggle` for items that can be manually toggled

### `ParticipantGrid`

- Renders a card for each participant in `state.participants`
- Shows role badge, connection state, mic state, and ingest state
- Shows a **Set Live** button on non-active interpreter cards (visible to coordinator or active interpreter)
- Emits `set-live` with the target participant ID

### `BoothChatPanel`

- Renders a scrollable message list
- Auto-scrolls to the newest message
- Renders a text input for sending new messages
- Emits `send` with the message body

### `BoothHealthPanel`

- Renders ingest status (connected/disconnected/reconnecting)
- Renders live streaming indicator
- Renders active interpreter name
- Renders bitrate and packet health (healthy / degraded / idle)

---

## Environment variables (Vite)

Set in `.env` with the `VITE_` prefix:

| Variable | Default | Description |
|---|---|---|
| `VITE_INGEST_BASE_URL` | `http://127.0.0.1:5000` | Base URL of the ingest API |
| `VITE_INGEST_AUTH_TOKEN` | _(empty)_ | Bearer token for ingest API auth |
| `VITE_BOOTH_WS_URL` | _(empty)_ | WebSocket URL for booth realtime (optional) |
| `VITE_STUN_SERVERS` | `stun:stun.l.google.com:19302` | STUN server(s) for ICE |
| `VITE_JITSI_DOMAIN` | `meet.jit.si` | Expected Jitsi domain for URL validation |
| `VITE_DEFAULT_JITSI_URL` | `https://meet.jit.si/eventyay-stage-room` | Default monitoring room |
| `VITE_DEFAULT_EVENT_SLUG` | `demo-event` | Default event slug for dev |
| `VITE_DEFAULT_BOOTH_ID` | `demo-booth` | Default booth ID for dev |
| `VITE_DEFAULT_LANGUAGE` | `English` | Default language label |
| `VITE_DEFAULT_CHANNEL_ID` | `demo-booth-audio` | Default channel ID |

---

## Build

```bash
npm install
npm run build    # outputs to static/dist/
```

The Vite build output is served by Flask as static files. The Flask template (`templates/interpreter_booth.html`) includes the Vite manifest entry point.

For development, run both the Flask server and the Vite dev server in parallel. The Vite dev server proxies API requests to Flask.
