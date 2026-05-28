# Eventyay Interpretation Portal

Real-time interpretation coordination for Eventyay events.
Interpreters stream live audio via WebRTC/WHIP → MediaMTX → HLS.
Booth coordination (who is active, relay handoff, chat) runs over WebSocket.

---

## How it works

```
Interpreter browser
  │  mic → RTCPeerConnection → WHIP POST
  ▼
MediaMTX :8889 (WHIP ingest)          Python is never in the audio path
  │  remux → HLS segments
  ▼
MediaMTX :8888 (HLS output) ←── attendees pull index.m3u8

Interpreter / Coordinator browser
  │  WebSocket /ws/booth/{booth_id}
  ▼
FastAPI portal :8000 (coordination, state, JWT, REST)
```

**Seamless interpreter handoff**: when the coordinator switches the active interpreter,
the outgoing interpreter mutes its mic tracks but keeps the WHIP session alive for
700 ms. MediaMTX never destroys the HLS muxer, so attendees see no 404 and need no
browser refresh. The incoming interpreter retries WHIP every 400 ms (up to 6
attempts) and connects cleanly once the outgoing side releases the path.

---

## Setup

### Option 1 — Docker Compose (recommended)

Everything starts with one command. No Python, MediaMTX, or manual config needed.

```bash
git clone https://github.com/fossasia/eventyay-interpretation-portal.git
cd eventyay-interpretation-portal
docker compose up --build
```

Open http://localhost:8000. That is it.

| Service | Port | Purpose |
|---------|------|---------|
| FastAPI portal | 8000 | Web UI, REST API, WebSocket |
| MediaMTX HLS | 8888 | Audio stream for attendees |
| MediaMTX WHIP | 8889 | Audio ingest from interpreters |

To stop: `Ctrl+C` or `docker compose down`.

### Option 2 — Native (two terminals)

**Requirements**: Python 3.13+, [uv](https://github.com/astral-sh/uv), [MediaMTX](https://github.com/bluenviron/mediamtx/releases)

```bash
git clone https://github.com/fossasia/eventyay-interpretation-portal.git
cd eventyay-interpretation-portal
uv sync                      # install Python dependencies
```

Download MediaMTX for your platform from [releases](https://github.com/bluenviron/mediamtx/releases):

```bash
# macOS ARM64 example
curl -sL https://github.com/bluenviron/mediamtx/releases/download/v1.12.3/mediamtx_v1.12.3_darwin_arm64.tar.gz \
  | tar xzf - mediamtx
chmod +x mediamtx
```

**Terminal 1 — MediaMTX:**
```bash
./mediamtx mediamtx.yml
```

**Terminal 2 — FastAPI:**
```bash
uv run uvicorn fastapi_app:app --host 127.0.0.1 --port 8000 --reload
```

Open http://localhost:8000.

### Verify it works

```bash
curl http://localhost:8000/healthz
# {"ok": true, "server": "fastapi", "mediamtx_ok": true}
```

### Test audio end-to-end

1. Open http://localhost:8000/interpreter/demo-booth
2. Enter a name, select **Interpreter**, click **Join Booth**
3. Click **Go Live** — allow microphone when prompted
4. Open VLC → File → Open Network → `http://localhost:8888/demo-booth-audio/index.m3u8`
5. Speak — you should hear yourself in VLC with ~3 s delay

### Environment variables

Copy `.env.example` → `.env` and adjust as needed:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SECRET_KEY` | `dev-secret` | JWT signing key |
| `BOOTH_ACCESS_TOKEN` | *(empty)* | Booth password (empty = open access) |
| `MEDIAMTX_WHIP_BASE` | `http://localhost:8889` | Browser-facing WHIP URL |
| `MEDIAMTX_HLS_BASE` | `http://localhost:8888` | Browser-facing HLS URL |
| `MEDIAMTX_INTERNAL_BASE` | *(empty)* | Python→MediaMTX URL (Docker: `http://mediamtx:8888`) |

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Redirect to demo booth |
| `GET`  | `/interpreter/{booth_id}` | Interpreter booth UI |
| `POST` | `/api/auth/token` | Issue a signed JWT |
| `GET`  | `/api/booth/{booth_id}/state` | Current booth snapshot |
| `GET`  | `/api/interpreter/status/{channel_id}` | MediaMTX reachability |
| `GET`  | `/healthz` | Health check |
| `WS`   | `/ws/booth/{booth_id}` | Booth coordination WebSocket |

### WebSocket messages (client → server)

| Type | Fields | Purpose |
|------|--------|---------|
| `booth:join` | `display_name`, `role`, `language`, `channel_id` | Enter a booth |
| `booth:leave` | — | Leave gracefully |
| `booth:update-state` | `mic_active`, `ingest_connected` | Report audio state |
| `booth:set-active` | `target_id` | Assign new active interpreter |
| `booth:chat` | `body` | Send chat message |

Server broadcasts `booth:state` to all connections on every state change.

---

## Development

```bash
uv sync --all-groups          # runtime + dev dependencies
uv run pytest tests/ -v       # 27 tests
node --check static/js/interpreter-booth.js   # JS syntax
```

### Project layout

```
fastapi_app.py                # FastAPI — REST, WebSocket, Jinja2
portal/
  config.py                   # pydantic-settings (env vars / .env)
  auth.py                     # JWT issue / validate
  booth_state.py              # async in-memory booth registry
templates/
  interpreter_booth.html      # Jinja2 template
static/
  js/interpreter-booth.js     # Plain browser JS — WebRTC/WHIP + WebSocket
  css/interpreter.css
mediamtx.yml                  # MediaMTX config (HLS 1 s segments, WHIP)
docker-compose.yml            # portal + mediamtx services
Dockerfile                    # FastAPI container (uv, Python 3.13-slim)
tests/
  test_fastapi_app.py         # REST + WebSocket integration tests
  test_booth_state.py         # booth registry unit tests
```
