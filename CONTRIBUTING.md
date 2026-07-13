# Contributing to Voxbento

## Running tests

```bash
uv run pytest tests/ -v
npm run typecheck   # JS syntax check
```

All tests must pass before opening a PR. The same checks run in CI
(`.github/workflows/tests.yml`).

## Branch and PR workflow

- Branch off `main`: `git checkout -b feat/your-feature`
- Keep commits focused and atomic (one concern per commit)
- Open PRs against `main`
- All CI checks must be green before merge

## Code conventions

### Python

- Python 3.13+; run with `uv`
- `asyncio.Lock` for all shared mutable state in `BoothRegistry`
- Use specific exception types (`ValueError`, `PermissionError`) — do not catch bare `Exception`
- `portal.*` namespace for new imports; no `pretix.*`, `pretalx.*`, or `venueless.*`

### JavaScript

- Plain browser ES modules — no jQuery, no Alpine, no bundler required for `interpreter-booth.js`
- No inline scripts in HTML templates
- Use `element.textContent` / `element.setAttribute` / `element.dataset` — never assign user-controlled data to `innerHTML`

### Django templates / Jinja2

- All user-controlled values must be escaped via the template engine or `escapeHtml()` in JS

## Architecture constraints

- **Python is never in the audio path.** Audio flows: browser mic → WHIP → MediaMTX → WHEP → attendee. Do not add aiortc or similar.
- **No Flask, no Socket.IO.** FastAPI + native WebSocket is the sole backend.
- **Two data stores.** Real-time booth state lives in `BoothRegistry` (in-memory). Persistent admin entities (events, rooms, booths, tokens) live in SQLAlchemy models with Alembic migrations. See `portal/models.py` and `portal/database.py`.
- **Booth fields are immutable after creation.** `language` and `channel_id` on a `Booth` object are set on first join and not overwritten.

## Database and migrations

The portal uses SQLAlchemy 2.0 (async) with Alembic for schema management.
Models are database-agnostic — SQLite for development, PostgreSQL for production.

### When you change a model

```bash
# 1. Edit portal/models.py
# 2. Generate a migration
alembic revision --autogenerate -m "describe the change"
# 3. Review the generated file in alembic/versions/
# 4. Apply locally
alembic upgrade head
# 5. Commit the migration file — it is version-controlled schema history
git add alembic/versions/*.py
```

### Rules

- **Always commit migration files.** They are the authoritative schema history.
- **Never commit `.db` files.** They are git-ignored and environment-specific.
- **Never edit a migration after it is merged.** Create a new migration instead.
- **Test migrations in CI.** The test suite creates tables from models directly (no Alembic), but the migration must match the model definitions.
- **Keep models database-agnostic.** Use SQLAlchemy column types that work on both SQLite and PostgreSQL. Do not use raw SQL.

## Audio handoff

The seamless interpreter-switch relies on the silence-mode handoff in
`static/js/interpreter-booth.js → applyBoothState`. If you change timing
constants (`700 ms` outgoing silence window, `200 ms` retry interval), test with
a real MediaMTX instance to verify WHEP continuity.

WHEP listeners recover in ~1.5–3 s (RTCPeerConnection stays open via
`alwaysAvailable` paths).

## Transcription, Translation, and AI Services

Voxbento provides real-time transcription (Speech-to-Text) and multilingual translation (Text-to-Text) using various AI providers.

### Architecture

1. **Audio Sources:** 
   - **Booth Audio:** Interpreters speak into their microphones, which pushes audio via WebRTC WHIP.
   - **Floor Audio:** A headless Chromium browser (`floor-bot`) secretly joins the Jitsi meeting, captures the floor audio, and pushes it via RTSP to MediaMTX.
2. **Worker (`portal/transcription/worker.py`):** Spawns an FFmpeg process that pulls audio from MediaMTX via RTSP, transcodes it to PCM S16LE, and pipes it to a `TranscriptionProvider`.
3. **Provider (`portal/transcription/providers/`):** Handles communication with AI speech-to-text services (OpenAI, Deepgram, ElevenLabs, NVIDIA, or Local Faster-Whisper).
4. **Aggregator (`portal/transcription/aggregator.py`):** Collects partial/final transcripts, broadcasts partials via WebSockets (`/ws/captions/{channel_id}`), and saves finalized sentences to the `transcript_segments` database table.
5. **Translation (`portal/translations/worker.py`):** Once a transcript segment is finalized, the translation worker spins up concurrent LLM requests (Anthropic, Groq, Gemini) to translate the text into all enabled target languages. Translations are saved to `transcript_translations` and broadcast via WebSockets.

### Implementing a New Provider

Inherit from `TranscriptionProvider` in `portal/transcription/providers/base.py`:

- `process_chunk`: For providers that accept discrete audio chunks (REST).
- `run_stream`: For providers that support streaming via WebSockets.

### Configuration

API keys for third-party transcription and translation providers are stored encrypted in the database and managed via the Admin Panel under **Global API Integrations**. Each Booth and Room can independently toggle transcription and translation on or off, and select their preferred models.

## Dependency management

```bash
# Add a runtime dep
uv add <package>

# Add a dev dep
uv add --dev <package>

# Always commit the updated uv.lock
uv sync --all-groups --python 3.13   # regenerate lock
git add pyproject.toml uv.lock
```

## Contributing to Documentation 

Our documentation is built with [Docusaurus](https://docusaurus.io/) and is located in the `docs/` folder (content) and the root directory (configuration). 

### Running documentation locally 

1.  Navigate to the root directory. 
2.  Install dependencies: `npm install` 
3.  Start the dev server: `npm start` 
4.  The documentation will be available at `http://localhost:3000/voxbento/`. 

### Editing Content 

Documentation is written in [MDX](https://mdxjs.com/). You can find the source files in the `docs/` directory. 

-   **Adding a page:** Create a new `.mdx` file in the appropriate subdirectory of `docs/`. 
-   **Updating the sidebar:** Edit `sidebars.ts` in the root directory to include your new page. 

### Adding Images 

1.  Place your image files in the `static/img/` directory. 
2.  Reference the image in your MDX file using standard Markdown syntax: 
    ```markdown 
    ![Description](/img/your-image.png) 
    ``` 
3.  **Pasting Images:** If you have an image in your clipboard, save it to `static/img/` first, give it a descriptive name (lowercase, hyphens), and then reference it as shown above. 

### Documentation Standards 

-   Use clear, concise language. 
-   Use Docusaurus [admonitions](https://docusaurus.io/docs/markdown-features/admonitions) for notes, tips, and warnings: 
    ```markdown 
    :::note 
    This is a note. 
    ::: 
    ``` 
-   Ensure all links are relative and functional. 

## Security

- Never pass user-controlled data to `innerHTML` / `outerHTML` / `document.write`
- Validate and escape all inputs at system boundaries
- Keep `SECRET_KEY` and `BOOTH_ACCESS_TOKEN` out of version control (use `.env`)
- Sourcery runs security checks on every PR — resolve any blocking findings before merge

## Local Development Setup

Run the portal and MediaMTX natively for fast iteration with hot-reload. Jitsi runs
in Docker since it requires four interconnected Java/Lua services.

#### Step 1: Clone and install dependencies

```bash
git clone https://github.com/fossasia/voxbento.git
cd voxbento
uv sync                      # install Python dependencies
```

#### Step 2: Configure environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```ini
ADMIN_PASSWORD=my-secure-admin-pass
DOCKER_HOST_ADDRESS=192.168.1.x   # your LAN IP (for Jitsi)
```

#### Step 3: Download MediaMTX

Download the binary for your platform from [MediaMTX releases](https://github.com/bluenviron/mediamtx/releases):

**macOS (Apple Silicon):**
```bash
curl -sL https://github.com/bluenviron/mediamtx/releases/download/v1.12.3/mediamtx_v1.12.3_darwin_arm64.tar.gz \
  | tar xzf - mediamtx
```

**macOS (Intel):**
```bash
curl -sL https://github.com/bluenviron/mediamtx/releases/download/v1.12.3/mediamtx_v1.12.3_darwin_amd64.tar.gz \
  | tar xzf - mediamtx
```

**Linux (x86_64):**
```bash
curl -sL https://github.com/bluenviron/mediamtx/releases/download/v1.12.3/mediamtx_v1.12.3_linux_amd64.tar.gz \
  | tar xzf - mediamtx
```

```bash
chmod +x mediamtx
```

#### Step 4: Start Jitsi (Docker)

Jitsi requires four services (web, prosody, jicofo, jvb). The easiest way to run it
is via the project's Docker Compose file with only the Jitsi services:

```bash
docker compose up -d jitsi-web jitsi-prosody jitsi-jicofo jitsi-jvb
```

Wait ~15 seconds for all services to start. Verify Jitsi is running:

```bash
curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/
# Should return 200 or 301
```

> **Note:** Jitsi is used by interpreters to monitor the speaker's live video/audio.
> If you don't need the speaker monitoring feature, you can skip this step — the
> portal will work without it, but the Jitsi iframe on the interpreter page will
> show a connection error.

#### Step 5: Run database migrations

```bash
# Natively
uv run alembic upgrade head

# In Docker
docker compose exec portal uv run alembic upgrade head
```

This creates the SQLite database (`interpretation.db`) and applies all migrations
(users, events, rooms, booths, invite tokens, event memberships).

#### Step 6: Start MediaMTX

**Terminal 1:**
```bash
./mediamtx mediamtx.yml
```

You should see:
```
INF MediaMTX v1.12.3
INF [WebRTC] listener opened on :8889 (TCP), :8189 (UDP)
INF [API] listener opened on :9997
```

#### Step 7: Start the portal

**Terminal 2:**
```bash
uv run uvicorn fastapi_app:app --host 127.0.0.1 --port 8000 --reload
```

You should see:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     Started reloader process
```

### Verify the setup

Run these checks to confirm everything is connected:

```bash
# 1. Portal health (checks MediaMTX connectivity too)
curl http://localhost:8000/healthz
# {"ok": true, "server": "fastapi", "mediamtx_ok": true}

# 2. MediaMTX API
curl http://localhost:9997/v3/paths/list
# {"items": [], ...}

# 3. Jitsi web (if running)
curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/
# 200

# 4. Admin panel
open http://localhost:8000/admin/login
# Log in with your ADMIN_PASSWORD
```

### Test audio end-to-end

1. Log in to admin panel → create an **Event** → **Room** → **Booth**
2. Open the booth detail page → generate an **invite token** with role "interpreter"
3. Copy the invite link (`/join/<token>`) and open it in a new browser tab
4. You'll be redirected to the interpreter booth — click **Go Live** and allow microphone
5. Open `http://localhost:8000/listener/<event_slug>` in another tab
   - This is the WHEP WebRTC listener — sub-second latency
7. Speak — you should hear yourself

**Quick test without admin panel:**
```bash
# Direct interpreter booth (for development/testing only)
open http://localhost:8000/events/demo-event/booths/en
# Enter a name → select Interpreter → Join Booth → Go Live
# Then open http://localhost:8000/listener/demo-event in another tab
```

### Services and ports

| Service | Port | Protocol | Purpose |
|---------|------|----------|---------|
| FastAPI portal | 8000 | HTTP | Web UI, REST API, WebSocket |
| MediaMTX WHIP/WHEP | 8889 | HTTP+UDP | WebRTC ingest (WHIP) and playback (WHEP) |
| MediaMTX ICE | 8189/udp | UDP | WebRTC media traffic |
| MediaMTX API | 9997 | HTTP | Dynamic path management |
| Jitsi Web | 8080 | HTTP | Speaker monitoring (interpreter iframe) |
| Jitsi Web (HTTPS) | 8443 | HTTPS | Speaker monitoring (production) |
| Jitsi JVB | 10000/udp | UDP | Jitsi video bridge media |

### Environment variables

Copy `.env.example` → `.env` and adjust as needed:

| Variable | Default | Purpose |
|----------|---------|---------|
| `ADMIN_PASSWORD` | *(empty)* | Admin panel login password (**required**) |
| `SECRET_KEY` | `change-me` | JWT signing key |
| `BOOTH_ACCESS_TOKEN` | *(empty)* | Booth password (empty = open access) |
| `DOCKER_HOST_ADDRESS` | *(empty)* | Host LAN IP for Jitsi JVB ICE candidates |
| `JITSI_DOMAIN` | `localhost:8080` | Jitsi Meet domain |
| `DEFAULT_JITSI_ROOM` | `eventyay-stage-room` | Default Jitsi room name |
| `JITSI_BASE_URL` | *(empty)* | Full Jitsi URL with scheme (empty = `http://{JITSI_DOMAIN}`) |
| `MEDIAMTX_WHIP_BASE` | `http://localhost:8889` | Browser-facing WHIP/WHEP URL |
| `MEDIAMTX_INTERNAL_BASE` | *(empty)* | Python→MediaMTX URL (Docker: `http://mediamtx:8888`) |
| `MEDIAMTX_API_BASE` | `http://localhost:9997` | MediaMTX Control API |
| `DATABASE_URL` | `sqlite+aiosqlite:///./interpretation.db` | Database (PostgreSQL for production) |
| `JVB_AUTH_PASSWORD` | `changeme` | Jitsi JVB auth (change in production) |
| `JICOFO_AUTH_PASSWORD` | `changeme` | Jitsi Jicofo auth (change in production) |
| `FLOOR_BOT_BASE` | `http://floor-bot:8080` | URL for the floor-bot container to extract Jitsi audio |
| `MEDIAMTX_RTSP_BASE` | `rtsp://mediamtx:8554` | Internal MediaMTX RTSP URL for floor audio transcription |

### Stopping services

```bash
# Stop portal: Ctrl+C in Terminal 2
# Stop MediaMTX: Ctrl+C in Terminal 1
# Stop Jitsi:
docker compose down jitsi-web jitsi-prosody jitsi-jicofo jitsi-jvb
# Or stop everything:
docker compose down
```

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Home page with event list and listener links |
| `GET`  | `/register` | User registration page |
| `POST` | `/register` | Create listener account |
| `GET`  | `/login` | User login page |
| `POST` | `/login` | Authenticate and set session cookie |
| `GET`  | `/logout` | Clear session cookie |
| `GET`  | `/account` | User account page (role, status) |
| `GET`  | `/events/{event_slug}/booths/{language_code}` | Interpreter booth UI |
| `GET`  | `/listener/{event_slug}` | Attendee listener (WebRTC playback) |
| `POST` | `/api/auth/token` | Issue a signed JWT |
| `GET`  | `/api/events/{event_slug}/booths/{language_code}/state` | Current booth snapshot |
| `GET`  | `/api/interpreter/status/{channel_id}` | MediaMTX reachability |
| `GET`  | `/healthz` | Health check |
| `WS`   | `/ws/booth/{booth_id}` | Booth coordination WebSocket |
| `WS`   | `/ws/tts/{room_id}` | TTS playback WebSocket |

### Admin panel routes

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/admin/login` | Admin login page |
| `POST` | `/admin/login` | Validate admin password |
| `GET`  | `/admin/` | Dashboard with event cards and live status |
| `GET`  | `/admin/events/` | Event list + create form |
| `GET`  | `/admin/events/{id}/` | Event detail with rooms and booths |
| `GET`  | `/admin/events/{id}/rooms/` | Room list + create form |
| `GET`  | `/admin/events/{id}/rooms/{id}/` | Room detail with interpreter URLs |
| `GET`  | `/admin/events/{id}/rooms/{id}/booths/` | Booth list + create form |
| `GET`  | `/admin/events/{id}/rooms/{id}/booths/{id}/` | Booth detail: WHEP URL, participants, tokens |
| `GET`  | `/admin/users/` | User management: roles, activate/deactivate |

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
uv run pytest tests/ -v       # run test suite
npm run typecheck   # JS syntax
```

### Database

The portal uses SQLAlchemy 2.0 (async) for persistent storage and Alembic for schema migrations.
SQLite is the default for development; PostgreSQL is supported for production via `DATABASE_URL`.

```bash
# Apply migrations (creates tables if needed)
alembic upgrade head

# After changing models, generate a migration
alembic revision --autogenerate -m "describe the change"

# Apply the new migration
alembic upgrade head
```

The SQLite database file (`interpretation.db`) is git-ignored. Migration scripts in
`alembic/versions/` are committed — they are the version-controlled schema history.

In Docker, `docker compose up` runs migrations automatically on startup. The database
file is stored on a named volume (`portal-data`) that persists across `docker compose down`
/ `docker compose up` cycles.

### Project layout

```
fastapi_app.py                # Lightweight ASGI entrypoint (main app)
portal/
  routers/                    # Modular HTTP endpoints (auth, api, admin, etc.)
  websockets/                 # WebSocket manager and connection handlers
  config.py                   # pydantic-settings (env vars / .env)
  auth.py                     # JWT issue / validate
  booth_state.py              # async in-memory booth registry
  booth_identity.py           # booth ID ↔ MediaMTX path mapping
  roles.py                    # Permission enum, role-permission mapping
  models.py                   # SQLAlchemy declarative models (Event, Room, DBBooth, InviteToken, User)
  database.py                 # async engine, session factory, CRUD helpers
alembic/
  env.py                      # async-aware Alembic environment
  versions/                   # migration scripts (committed to version control)
templates/
  home.html                   # public home page with event list
  register.html               # user registration form
  login.html                  # user login form
  account.html                # user account page
  interpreter_booth.html      # Jinja2 interpreter booth page
  listener-webrtc.html        # Jinja2 attendee page (WHEP WebRTC, primary)
  admin/                      # admin panel templates (dashboard, CRUD, users)
static/
  js/interpreter-booth.js     # Plain browser JS — WebRTC/WHIP + WebSocket
  js/whep-listener.js         # WHEP WebRTC listener client
  css/interpreter.css
  css/admin.css               # admin panel and auth page styles
mediamtx.yml                  # MediaMTX config (WHIP ingest, WHEP playback, Control API)
docker-compose.yml            # portal + mediamtx + jitsi services
Dockerfile                    # FastAPI container (uv, Python 3.13-slim)
tests/
  test_fastapi_app.py         # REST + WebSocket integration tests
  test_booth_state.py         # booth registry unit tests
  test_booth_identity.py      # booth identity scheme tests
  test_roles.py               # permission model tests
  test_database.py            # database model + CRUD tests
  test_admin_panel.py         # admin panel route tests
  test_user_auth.py           # registration, login, account, user management tests
  test_join_flow.py           # invite-token join flow tests
```

### Admin panel setup

```bash
# Set the admin password (required for admin access)
export ADMIN_PASSWORD='your-secure-password'

# Start the server
uv run uvicorn fastapi_app:app --host 127.0.0.1 --port 8000 --reload

# Visit http://localhost:8000/admin/login and enter the password
```

### Creating events, rooms, and booths

1. Log in to the admin panel at `/admin/login`
2. Go to **Events** → create an event (slug + display name)
3. Open the event → go to **Rooms** → create a room
4. Open the room → go to **Booths** → create a booth (language code + name)
5. The booth detail page shows:
   - Interpreter page URL (share with interpreters)
   - WHEP listener URL (for attendees)
   - Invite token management

### User registration and role management

1. Users register at `/register` — new accounts are non-admin with no event roles
2. Admins manage site-wide access (the `is_admin` flag) and activation status at `/admin/users/`
3. Per-event roles (`listener`, `interpreter`, `coordinator`, `event_admin`) are assigned from each event's **Members** page at `/admin/events/{id}/members/` — inline dropdowns let admins assign or remove roles per user
4. Deactivate users to prevent login without deleting their account
