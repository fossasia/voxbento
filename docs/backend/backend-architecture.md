# Backend Architecture

The backend is a FastAPI application using native WebSocket for realtime communication. Audio ingest is handled entirely by MediaMTX via the WHIP protocol — Python never touches audio data.

---

## Technology stack

| Technology | Version | Role |
|---|---|---|
| FastAPI | — | HTTP server, route handling, ASGI |
| uvicorn | — | ASGI server |
| WebSocket (FastAPI native) | — | Realtime booth coordination |
| PyJWT | — | JWT token authentication |
| pydantic-settings | — | Environment variable loading and validation |
| MediaMTX | bluenviron/mediamtx:1 | WHIP ingest and HLS delivery (external service) |
| Python | 3.12.x | Runtime |
| uv | — | Dependency management and venv |

---

## Module structure

```
fastapi_app.py               # FastAPI app, routes, WebSocket handlers, access control
portal/
├── __init__.py
├── config.py                # Settings via pydantic-settings loaded from env vars
├── auth.py                  # JWT authentication via PyJWT
└── booth_state.py           # Async in-memory booth registry and state machine
templates/
├── base.html                # Page shell (Eventyay-style header)
├── interpreter_booth.html   # Booth page (server-rendered with Jinja2)
└── listen.html              # HLS listener page with hls.js
static/
└── js/
    └── interpreter-booth.js # Plain ES module: state machine, WebSocket, WHIP, UI
```

---

## `fastapi_app.py` — FastAPI routes and WebSocket handlers

### HTTP routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Redirect to `/interpreter/demo-booth` |
| `GET` | `/healthz` | Health check |
| `GET` | `/interpreter/{booth_id}` | Render interpreter booth page (Jinja2) |
| `GET` | `/listen/{booth_id}` | Render HLS listener page (Jinja2) |
| `GET` | `/api/booth/{booth_id}/state` | Fetch current booth state snapshot |

### WebSocket endpoint

| Path | Purpose |
|---|---|
| `/ws/booth/{booth_id}` | Realtime booth coordination (join, leave, chat, handoff, state updates) |

### WebSocket messages (client sends)

| Message type | Payload | Purpose |
|---|---|---|
| `booth:join` | `{ booth_id, token, display_name, role, language, channel_id, participant_id }` | Join booth; creates participant |
| `booth:leave` | `{ booth_id, participant_id, language, channel_id }` | Leave booth; remove participant |
| `booth:chat` | `{ booth_id, sender_id, body, language, channel_id }` | Send chat message |
| `booth:set-active` | `{ booth_id, requester_id, target_id, language, channel_id }` | Assign active interpreter |
| `booth:update-state` | `{ booth_id, participant_id, mic_active, ingest_connected, connected, ... }` | Update participant state |

### WebSocket messages (server sends)

| Message type | Target | Payload |
|---|---|---|
| `booth:state` | All booth WebSocket connections | Full booth state snapshot |
| `booth:joined` | Connecting client only | `{ participant_id, state }` |
| `booth:chat` | All booth WebSocket connections | Chat message object |
| `booth:error` | Connecting client only | `{ message }` |

### Access control

`portal/auth.py` handles JWT-based authentication via PyJWT. Tokens are validated on HTTP API calls and WebSocket connections. If the token is invalid or missing, the request is rejected.

### Connection management

Each booth has a set of active WebSocket connections. All `booth:state` broadcasts are scoped to connections for the same booth so participants only receive state for their booth.

The server tracks WebSocket connections and performs automatic leave-on-disconnect when a connection drops.

---

## `portal/booth_state.py` — BoothRegistry

The `BoothRegistry` is the single source of truth for all booth state at runtime. It is an async in-memory registry.

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

## `portal/auth.py` — JWT Authentication

Handles token creation and validation using PyJWT. Provides middleware for protecting HTTP endpoints and WebSocket connections.

---

## `portal/config.py` — Settings

A pydantic-settings model loaded from environment variables. All settings have safe development defaults.

`settings` is a module-level singleton created at import time.

---

## Templates

### `templates/base.html`

Provides the Eventyay-style page shell: meta tags, CSS imports, and a `{% block content %}` for page content.

### `templates/interpreter_booth.html`

Extends `base.html`. Renders the interpreter console with all panels. Server-side config is passed into the HTML via Jinja2 template variables:

- `booth_id`, `booth_token`, `booth_language`, `booth_channel_id`
- `default_jitsi_room`, `jitsi_domain`
- `mediamtx_whip_url`, `mediamtx_hls_url`

JavaScript in `static/js/interpreter-booth.js` reads these values and drives the UI.

### `templates/listen.html`

Renders the HLS listener page for a specific booth. Uses hls.js with auto-recovery to play the interpretation audio stream from MediaMTX.

---

## Production considerations

### In-memory state

`BoothRegistry` stores all state in-memory. This means:

- Booth state is lost on server restart.
- Multi-worker deployments will have separate state per worker — participants in different workers will not see each other.

**Production fix:** Add PostgreSQL persistence for `Booth` and `Participant` records, and use a shared pub/sub layer (e.g., Redis) for cross-worker WebSocket broadcasting.

### JWT secret

The JWT signing secret must be changed in production and kept secret.

### CORS

CORS origins should be configured explicitly in production rather than allowing all origins.

### HTTPS

The browser will not grant microphone access on non-HTTPS origins (except `localhost`). Production deployments must use HTTPS.
