# Voxbento

Real-time interpretation coordination for  events.
Interpreters stream live audio via WebRTC/WHIP → MediaMTX → WHEP (WebRTC playback).
HLS is retained as a fallback delivery path.
Booth coordination (who is active, relay handoff, chat) runs over WebSocket.

---

## How it works

```
Interpreter browser
  │  mic → RTCPeerConnection → WHIP POST
  ▼
MediaMTX :8889 (WHIP ingest + WHEP)   Python is never in the audio path
  │  WebRTC termination + remux
  ├──► WHEP :8889 (primary) ←── attendees connect via WebRTC (sub-second latency)
  └──► HLS  :8888 (fallback) ←── attendees pull index.m3u8 (~2-3 s latency)

Interpreter / Coordinator browser
  │  WebSocket /ws/booth/{booth_id}
  ▼
FastAPI portal :8000 (coordination, state, JWT, REST)
```

**Seamless interpreter handoff**: when the coordinator switches the active interpreter,
MediaMTX's `overridePublisher: yes` lets the incoming interpreter connect immediately
while kicking the outgoing one. MediaMTX paths are created with `alwaysAvailable: true`
via the Control API so WHEP listeners stay connected during handoff and receive the
new publisher's audio within ~1.5–3 s. HLS fallback listeners auto-recover with a
longer gap (~10–15 s).

---

## Setup

### Prerequisites

| Dependency | Version | Purpose |
|-----------|---------|---------|
| Python | 3.13+ | FastAPI portal |
| [uv](https://github.com/astral-sh/uv) | latest | Python package manager |
| [MediaMTX](https://github.com/bluenviron/mediamtx/releases) | 1.x | WebRTC/HLS audio server |
| Docker & Docker Compose | latest | Jitsi stack (or full Docker setup) |

### Option 1 — Docker Compose (everything in containers)

All services (portal, MediaMTX, Jitsi) start with one command:

```bash
git clone https://github.com/fossasia/voxbento.git
cd voxbento

# Configure environment
cp .env.example .env

# Required: set your admin password
echo 'ADMIN_PASSWORD=my-secure-admin-pass' >> .env

# Required for Jitsi video: set your machine's LAN IP
# macOS:  ipconfig getifaddr en0
# Linux:  hostname -I | awk '{print $1}'
echo 'DOCKER_HOST_ADDRESS=192.168.1.x' >> .env

docker compose up --build
```

Open http://localhost:8000 — all services are running.

### Option 2 — Native setup (recommended for development)

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
INF [HLS] listener opened on :8888
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
5. Open `http://localhost:8000/listener-webrtc/<booth-slug>` in another tab
   - This is the WHEP WebRTC listener — sub-second latency
6. Or open `http://localhost:8000/listen/<booth-slug>` for HLS fallback (~3 s delay)
7. Speak — you should hear yourself

**Quick test without admin panel:**
```bash
# Direct interpreter booth (for development/testing only)
open http://localhost:8000/interpreter/demo-booth
# Enter a name → select Interpreter → Join Booth → Go Live
# Then open http://localhost:8000/listener-webrtc/demo-booth in another tab
```

### Services and ports

| Service | Port | Protocol | Purpose |
|---------|------|----------|---------|
| FastAPI portal | 8000 | HTTP | Web UI, REST API, WebSocket |
| MediaMTX WHIP/WHEP | 8889 | HTTP+UDP | WebRTC ingest (WHIP) and playback (WHEP) |
| MediaMTX HLS | 8888 | HTTP | HLS fallback stream |
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
| `MEDIAMTX_HLS_BASE` | `http://localhost:8888` | Browser-facing HLS URL |
| `MEDIAMTX_INTERNAL_BASE` | *(empty)* | Python→MediaMTX URL (Docker: `http://mediamtx:8888`) |
| `MEDIAMTX_API_BASE` | `http://localhost:9997` | MediaMTX Control API |
| `DATABASE_URL` | `sqlite+aiosqlite:///./interpretation.db` | Database (PostgreSQL for production) |
| `JVB_AUTH_PASSWORD` | `changeme` | Jitsi JVB auth (change in production) |
| `JICOFO_AUTH_PASSWORD` | `changeme` | Jitsi Jicofo auth (change in production) |

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
| `GET`  | `/interpreter/{booth_id}` | Interpreter booth UI |
| `GET`  | `/listener-webrtc/{booth_id}` | Attendee WHEP listener (WebRTC, primary) |
| `GET`  | `/listen/{booth_id}` | Attendee HLS listener (hls.js, fallback) |
| `POST` | `/api/auth/token` | Issue a signed JWT |
| `GET`  | `/api/booth/{booth_id}/state` | Current booth snapshot |
| `GET`  | `/api/interpreter/status/{channel_id}` | MediaMTX reachability |
| `GET`  | `/healthz` | Health check |
| `WS`   | `/ws/booth/{booth_id}` | Booth coordination WebSocket |

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
node --check static/js/interpreter-booth.js   # JS syntax
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
fastapi_app.py                # FastAPI — REST, WebSocket, Jinja2
portal/
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
  listener.html               # Jinja2 attendee page (hls.js fallback)
  listener-webrtc.html        # Jinja2 attendee page (WHEP WebRTC, primary)
  admin/                      # admin panel templates (dashboard, CRUD, users)
static/
  js/interpreter-booth.js     # Plain browser JS — WebRTC/WHIP + WebSocket
  js/whep-listener.js         # WHEP WebRTC listener client
  css/interpreter.css
  css/admin.css               # admin panel and auth page styles
mediamtx.yml                  # MediaMTX config (WHIP ingest, WHEP playback, HLS fallback, Control API)
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
