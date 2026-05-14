# Backend Architecture

The backend is a Flask application with Flask-SocketIO for realtime communication and aiortc for WebRTC ingest.

---

## Technology stack

| Technology | Version | Role |
|---|---|---|
| Flask | 3.1.3 | HTTP server and route handling |
| Flask-SocketIO | 5.6.1 | WebSocket/Socket.IO server |
| aiortc | 1.14.0 | Server-side WebRTC peer connection |
| av (PyAV) | 16.1.0 | FFmpeg bindings used by aiortc |
| python-dotenv | 1.2.2 | `.env` file loading |
| Python | 3.13.x | Runtime |
| uv | — | Dependency management and venv |

---

## Module structure

```
app.py                   # Flask app, routes, Socket.IO handlers, access control
portal/
├── __init__.py
├── config.py            # Settings dataclass loaded from env vars
├── booth_state.py       # In-memory booth registry and state machine
└── ingest.py            # aiortc ingest service and async runtime
templates/
├── base.html            # Page shell (Eventyay-style header)
└── interpreter_booth.html  # Booth page (passes config to Vue)
```

---

## `app.py` — Flask routes and Socket.IO handlers

### HTTP routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Redirect to `/interpreter/demo-booth` |
| `GET` | `/healthz` | Health check; includes `aiortc_available` flag |
| `GET` | `/interpreter/<booth_id>` | Render interpreter booth page |
| `GET` | `/api/booth/<booth_id>/state` | Fetch current booth state snapshot |
| `POST` | `/api/interpreter/connect/<channel_id>` | Accept WebRTC SDP offer; return answer |
| `POST` | `/api/interpreter/disconnect/<channel_id>` | Disconnect ingest session |
| `GET` | `/api/interpreter/status/<channel_id>` | Query ingest session state |

### Socket.IO events (server receives)

| Event | Payload | Purpose |
|---|---|---|
| `booth:join` | `{ booth_id, token, display_name, role, language, channel_id, participant_id }` | Join booth; creates participant |
| `booth:leave` | `{ booth_id, participant_id, language, channel_id }` | Leave booth; remove participant |
| `booth:chat` | `{ booth_id, sender_id, body, language, channel_id }` | Send chat message |
| `booth:set-active` | `{ booth_id, requester_id, target_id, language, channel_id }` | Assign active interpreter |
| `booth:update-state` | `{ booth_id, participant_id, mic_active, ingest_connected, connected, ... }` | Update participant state |
| `disconnect` | — | Socket.IO disconnect; auto-leave booth |

### Socket.IO events (server emits)

| Event | Target | Payload |
|---|---|---|
| `booth:state` | All booth room members | Full booth state snapshot |
| `booth:joined` | Connecting client only | `{ participant_id, state }` |
| `booth:chat` | All booth room members | Chat message object |
| `booth:error` | Connecting client only | `{ message }` |

### Access control

`require_access_token(token)` checks the token against `settings.booth_access_token`. If the token is set in the environment and the request/event does not provide a matching value, a `PermissionError` is raised and translated to a `403` HTTP response or a `booth:error` Socket.IO event.

The ingest endpoint (`POST /api/interpreter/connect/{channel_id}`) additionally enforces that the requesting participant is the active interpreter for the channel via `booths.is_active_interpreter(...)`.

### Room management

Each booth has a Socket.IO room named `booth:{booth_id}`. All `booth:state` broadcasts are scoped to this room so participants only receive state for their booth.

The `sid_index` dict maps Socket.IO session IDs to `{ booth_id, participant_id, language, channel_id }` to enable automatic leave-on-disconnect.

---

## `portal/booth_state.py` — BoothRegistry

The `BoothRegistry` is the single source of truth for all booth state at runtime. It is a thread-safe in-memory registry using an `RLock`.

### Data model

```
BoothRegistry
└── _booths: dict[booth_id → Booth]

Booth
├── booth_id: str
├── language: str
├── channel_id: str
├── active_interpreter_id: str | None
├── handoff_state: 'idle' | 'pending' | 'completed'
├── ingest_status: 'connected' | 'disconnected'
├── participants: dict[participant_id → Participant]
└── chat_messages: list[ChatMessage]  (capped at 500)

Participant
├── participant_id: str  (UUID hex)
├── display_name: str
├── role: 'interpreter' | 'coordinator' | 'listener'
├── language: str
├── channel_id: str
├── mic_active: bool
├── ingest_connected: bool
├── connected: bool
├── joined_at: ISO datetime string
└── updated_at: ISO datetime string

ChatMessage
├── message_id: str  (UUID hex)
├── sender_id: str
├── sender_name: str
├── body: str
└── sent_at: ISO datetime string
```

### Key operations

**`join_participant`**: Creates a `Participant` and adds it to the booth. If no active interpreter exists and the new participant is an interpreter, they automatically become active.

**`leave_participant`**: Removes the participant. If the leaving participant was the active interpreter, the next available interpreter in the roster is promoted (FCFS). `handoff_state` is set to `'pending'` if a replacement is found.

**`set_active_interpreter`**: Reassigns the active role. Enforces that:
- The requester is a coordinator, the current active interpreter, or is assigning themselves.
- The target has the `interpreter` role.
- Clears `mic_active` and `ingest_connected` for all non-target participants.

**`update_participant_state`**: Updates individual participant flags. Enforces that only the active interpreter can set `mic_active=True` or `ingest_connected=True`.

**`add_chat_message`**: Appends a message. Enforces that the sender is registered in the booth and the message body is non-empty. Trims history to the last 500 messages.

---

## `portal/ingest.py` — IngestService

Manages server-side WebRTC peer connections using aiortc.

### AsyncRuntime

aiortc is asyncio-based. Flask uses threading. `AsyncRuntime` runs a dedicated asyncio event loop on a daemon thread:

```python
class AsyncRuntime:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._run_forever, daemon=True)
        self.thread.start()

    def run(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result()   # blocks calling thread
```

All Flask request handlers call `ingest.connect(...)` or `ingest.disconnect(...)` which dispatch to this runtime and block until the result is available.

### IngestSession

```python
@dataclass
class IngestSession:
    channel_id: str
    booth_id: str
    participant_id: str
    peer_connection: RTCPeerConnection
    recorder: MediaRecorder
    connection_state: str = 'new'
    recorder_started: bool = False
```

Sessions are keyed by `channel_id` in `IngestService.sessions`.

### Connect sequence

1. Disconnect any existing session for the channel.
2. Create a new `RTCPeerConnection`.
3. Create a `MediaRecorder` pointed at `{INGEST_HLS_ROOT}/{channel_id}/playlist.m3u8`.
4. Register `connectionstatechange` handler — tears down on `failed`/`closed`.
5. Register `track` handler — adds audio tracks to the recorder and starts it.
6. `setRemoteDescription(offer)` → `createAnswer()` → `setLocalDescription(answer)`.
7. Wait for ICE gathering.
8. Store session; return `{ type, sdp }` answer.

### Disconnect sequence

1. Stop the recorder if started.
2. Close the peer connection.
3. Remove session from `sessions` dict.

### Graceful shutdown

`@atexit.register close_ingest()` calls `ingest.shutdown()`, which stops all active sessions and shuts down the async runtime thread.

---

## `portal/config.py` — Settings

A frozen dataclass loaded from environment variables via `python-dotenv`. All settings have safe development defaults.

```python
@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    debug: bool
    secret_key: str
    booth_access_token: str
    socket_cors_origins: list[str] | str
    default_jitsi_room: str
    jitsi_domain: str
    ingest_hls_root: Path
    hls_segment_seconds: int
    hls_playlist_length: int
```

`settings` is a module-level singleton created at import time. It is injected into `IngestService` at startup.

---

## Templates

### `templates/base.html`

Provides the Eventyay-style page shell: meta tags, CSS imports, and a `{% block content %}` for the booth page.

### `templates/interpreter_booth.html`

Extends `base.html`. Renders a `<div id="app">` where Vue mounts. Passes server-side config to Vue via `<script>` data attributes or a JSON config object:

- `booth_id`, `booth_token`, `booth_language`, `booth_channel_id`
- `default_jitsi_room`, `jitsi_domain`
- `aiortc_available`

---

## Production considerations

### In-memory state

`BoothRegistry` stores all state in a Python dict. This means:

- Booth state is lost on server restart.
- Multi-worker deployments (Gunicorn with multiple processes) will have separate state per worker — participants in different workers will not see each other.

**Production fix:** Add PostgreSQL persistence for `Booth` and `Participant` records, and use Redis as the Flask-SocketIO message queue (`socketio = SocketIO(app, message_queue='redis://...')`).

### Secret key

`SECRET_KEY` must be changed in production and kept secret. It is used for Flask session signing.

### CORS

`BOOTH_WS_CORS_ORIGINS` defaults to `*` (development). In production, set it to the explicit Eventyay origin(s).

### HTTPS

The browser will not grant microphone access on non-HTTPS origins (except `localhost`). Production deployments must use HTTPS.
