# Database Guide — Schema, Viewer Setup & Configuration

This guide explains the interpretation portal's database architecture, how to
inspect it with a GUI viewer, and how a single environment variable switches
between SQLite (dev) and PostgreSQL (prod).

---

## 1. Quick overview

The portal uses **two data stores**:

| Store | What lives there | Lifetime |
|---|---|---|
| **SQLAlchemy database** (SQLite / PostgreSQL) | Events, rooms, booths, invite tokens | Persistent — survives restarts |
| **In-memory Python dicts** (`booth_state.py`) | Live WebSocket connections, who-is-talking flags | Ephemeral — lost on restart |

The database is managed by **Alembic** (migrations) and accessed through
async CRUD helpers in `portal/database.py`.

---

## 2. The four tables

```
┌──────────────────────────────────────────────────────────────────┐
│                         DATABASE SCHEMA                         │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  events                                                          │
│  ├── id            INTEGER  PK AUTO                              │
│  ├── slug          VARCHAR(80)  UNIQUE, INDEXED                  │
│  ├── display_name  VARCHAR(200)                                  │
│  └── created_at    DATETIME (UTC)                                │
│       │                                                          │
│       │ 1 ──── * (CASCADE DELETE)                                │
│       ▼                                                          │
│  rooms                                                           │
│  ├── id               INTEGER  PK AUTO                           │
│  ├── event_id         INTEGER  FK → events.id                    │
│  ├── display_name     VARCHAR(200)                               │
│  ├── eventyay_room_id VARCHAR(200)  NULLABLE                     │
│  └── created_at       DATETIME (UTC)                             │
│       │                                                          │
│       │ 1 ──── * (CASCADE DELETE)                                │
│       ▼                                                          │
│  booths                                                          │
│  ├── id             INTEGER  PK AUTO                             │
│  ├── event_id       INTEGER  FK → events.id                      │
│  ├── room_id        INTEGER  FK → rooms.id                       │
│  ├── language_code  VARCHAR(10)                                  │
│  ├── language_name  VARCHAR(100)                                 │
│  └── created_at     DATETIME (UTC)                               │
│       │                                                          │
│       │  UNIQUE INDEX: (event_id, language_code)                 │
│       │                                                          │
│       │ 1 ──── * (CASCADE DELETE)                                │
│       ▼                                                          │
│  invite_tokens                                                   │
│  ├── token       VARCHAR(64)  PK  (hex, auto-generated)          │
│  ├── booth_id    INTEGER  FK → booths.id                         │
│  ├── role        VARCHAR(50)  (interpreter/coordinator/...)      │
│  ├── label       VARCHAR(200)                                    │
│  ├── expires_at  DATETIME  NULLABLE                              │
│  ├── used_at     DATETIME  NULLABLE                              │
│  ├── created_by  VARCHAR(200)                                    │
│  └── created_at  DATETIME (UTC)                                  │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### Entity-relationship diagram (Mermaid)

```mermaid
erDiagram
    events ||--o{ rooms : has
    events ||--o{ booths : has
    rooms  ||--o{ booths : contains
    booths ||--o{ invite_tokens : grants

    events {
        int id PK
        string slug UK
        string display_name
        datetime created_at
    }
    rooms {
        int id PK
        int event_id FK
        string display_name
        string eventyay_room_id
        datetime created_at
    }
    booths {
        int id PK
        int event_id FK
        int room_id FK
        string language_code
        string language_name
        datetime created_at
    }
    invite_tokens {
        string token PK
        int booth_id FK
        string role
        string label
        datetime expires_at
        datetime used_at
        string created_by
        datetime created_at
    }
```

### Key design decisions

- **`mediamtx_path` is NOT a column** — it's a Python `@property` on `DBBooth`
  that computes `{event.slug}/{booth.language_code}` at runtime.
- **`hls_url` is NOT stored** — HLS is a fallback; URLs are derived from the
  MediaMTX path. WHEP is the primary playback protocol.
- **Cascade deletes** flow downward: deleting an event removes all its rooms,
  booths, and tokens automatically.
- **`InviteToken.token`** is a 64-character hex string (32 bytes of
  `secrets.token_hex`) used as the primary key — no auto-increment ID.

---

## 3. Where is the database file?

### Local development (no Docker)

```
eventyay-interpretation-portal/interpretation.db   ← default location
```

The default URL is `sqlite+aiosqlite:///./interpretation.db` (set in
`portal/config.py`).

### Docker Compose

```
/data/interpretation.db   ← inside the container
```

Mounted on a Docker volume called `portal-data`. This volume survives
`docker compose down` (without `-v`).

To find the volume on your host:

```bash
docker volume inspect eventyay-interpretation-portal_portal-data
# Look for "Mountpoint" — e.g. /var/lib/docker/volumes/…/_data/interpretation.db
```

---

## 4. Connecting a DB viewer

### Option A: VS Code — SQLite Viewer extension (easiest)

1. Install the **SQLite Viewer** extension in VS Code:
   - Open Extensions (`Cmd+Shift+X`)
   - Search for "SQLite Viewer" by Florian Klampfer
   - Click Install

2. Open the database file:
   - **Local dev**: just click `interpretation.db` in the file explorer
   - **Docker**: copy it out first:
     ```bash
     docker compose cp portal:/data/interpretation.db ./interpretation.db
     ```
     Then open `interpretation.db` in VS Code

3. You'll see all four tables. Click any table to browse rows.

### Option B: VS Code — SQLTools extension (query support)

1. Install **SQLTools** + **SQLTools SQLite** driver:
   - Extensions → search "SQLTools" by Matheus Teixeira → Install
   - Extensions → search "SQLTools SQLite" → Install

2. Create a connection:
   - Click the SQLTools icon in the sidebar
   - "New Connection" → SQLite
   - Database file path: `./interpretation.db` (or the copied Docker file)
   - Click "Test Connection" → "Save"

3. Now you can:
   - Browse tables in the sidebar
   - Write and run SQL queries
   - See schema definitions

   Example queries:
   ```sql
   -- List all events
   SELECT * FROM events;

   -- List all booths with their event slug
   SELECT b.id, e.slug, b.language_code, b.language_name
   FROM booths b
   JOIN events e ON e.id = b.event_id
   ORDER BY e.slug, b.language_code;

   -- List all tokens with booth and event info
   SELECT t.token, t.role, t.label, t.used_at IS NOT NULL as is_used,
          b.language_code, e.slug as event
   FROM invite_tokens t
   JOIN booths b ON b.id = t.booth_id
   JOIN events e ON e.id = b.event_id;

   -- Count booths per event
   SELECT e.slug, COUNT(b.id) as booth_count
   FROM events e
   LEFT JOIN booths b ON b.event_id = e.id
   GROUP BY e.slug;
   ```

### Option C: DB Browser for SQLite (standalone GUI)

1. Download from https://sqlitebrowser.org/
2. File → Open Database → select `interpretation.db`
3. "Database Structure" tab shows all tables and columns
4. "Browse Data" tab lets you explore rows
5. "Execute SQL" tab lets you run queries

### Option D: Command-line sqlite3

```bash
# Local
sqlite3 interpretation.db

# Docker — run inside the container
docker compose exec portal sqlite3 /data/interpretation.db
```

Useful commands:
```sql
.tables                          -- list all tables
.schema events                   -- show CREATE TABLE for events
.schema                          -- show all CREATE TABLE statements
.headers on                      -- show column headers
.mode column                     -- pretty-print output
SELECT * FROM events;            -- query data
```

### Option E: DBeaver (cross-platform, supports SQLite + PostgreSQL)

1. Download from https://dbeaver.io/
2. Database → New Connection → SQLite
3. Browse to `interpretation.db`
4. Click "Test Connection" → "Finish"
5. Expand the connection in the sidebar to see tables

**DBeaver is especially useful** if you later switch to PostgreSQL — the same
tool works for both, and you can compare schemas side by side.

---

## 5. Switching databases with one environment variable

The entire database backend is controlled by a single env var: **`DATABASE_URL`**.

### How it works

```
portal/config.py
    └── Settings.database_url = "sqlite+aiosqlite:///./interpretation.db"   ← default

portal/database.py
    └── _get_engine() reads settings.database_url and creates the SQLAlchemy engine

alembic/env.py
    └── reads settings.database_url and overrides alembic.ini for migrations
```

### SQLite (development — default)

```bash
# No env var needed — this is the default
DATABASE_URL=sqlite+aiosqlite:///./interpretation.db
```

### PostgreSQL (production)

```bash
# Just change the URL format — everything else is automatic
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/interpretation
```

That's it. **No code changes.** SQLAlchemy's ORM abstracts the SQL dialect,
and Alembic generates dialect-appropriate DDL.

### Docker Compose

In `docker-compose.yml`, the default is:

```yaml
environment:
  DATABASE_URL: "${DATABASE_URL:-sqlite+aiosqlite:////data/interpretation.db}"
```

To switch to PostgreSQL, create a `.env` file:

```env
DATABASE_URL=postgresql+asyncpg://interp:secret@db:5432/interpretation
```

Then add a PostgreSQL service to your `docker-compose.yml`:

```yaml
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: interp
      POSTGRES_PASSWORD: secret
      POSTGRES_DB: interpretation
    volumes:
      - pgdata:/var/lib/postgresql/data
    ports:
      - "5432:5432"

volumes:
  pgdata:
```

And install the async PostgreSQL driver:

```bash
uv add asyncpg
```

### URL format reference

| Database | URL format | Driver |
|---|---|---|
| SQLite (file) | `sqlite+aiosqlite:///./path/to/db.db` | aiosqlite |
| SQLite (in-memory) | `sqlite+aiosqlite://` | aiosqlite |
| PostgreSQL | `postgresql+asyncpg://user:pass@host:5432/dbname` | asyncpg |
| MySQL | `mysql+aiomysql://user:pass@host:3306/dbname` | aiomysql |

> **Note**: Three slashes `///` = relative path. Four slashes `////` = absolute
> path. This matters for SQLite in Docker where the DB is at `/data/interpretation.db`.

---

## 6. What you'll see in a viewer

After running the Docker e2e test (or the app in production), here's what each
table contains:

### `events` table

| id | slug | display_name | created_at |
|---|---|---|---|
| 1 | pycon2026 | PyCon US 2026 | 2026-05-31 18:47:43 |
| 2 | fossasia2026 | FOSSASIA Summit 2026 | 2026-05-31 18:47:43 |

### `rooms` table

| id | event_id | display_name | eventyay_room_id | created_at |
|---|---|---|---|---|
| 1 | 1 | Main Hall | NULL | … |
| 2 | 1 | Workshop Room | NULL | … |
| 3 | 1 | Lightning Talks | NULL | … |
| 4 | 2 | Keynote Stage | eventyay-room-42 | … |
| 5 | 2 | Track A | NULL | … |

### `booths` table

| id | event_id | room_id | language_code | language_name | created_at |
|---|---|---|---|---|---|
| 1 | 1 | 1 | en | English | … |
| 2 | 1 | 1 | fr | French | … |
| 3 | 1 | 1 | es | Spanish | … |
| 4 | 1 | 2 | de | German | … |
| 5 | 1 | 2 | ja | Japanese | … |
| … | … | … | … | … | … |

> **Remember**: `mediamtx_path` is NOT a column — it's computed as
> `{event.slug}/{language_code}`, e.g. `pycon2026/en`.

### `invite_tokens` table

| token | booth_id | role | label | expires_at | used_at | created_by |
|---|---|---|---|---|---|---|
| 1a2d4b72… | 1 | interpreter | Alice Interpreter | NULL | 2026-05-31… | admin@pycon.org |
| f6456b2f… | 1 | coordinator | Bob Coordinator | NULL | NULL | admin@pycon.org |
| … | … | … | … | … | … | … |

---

## 7. Common operations

### Inspect the database from Docker

```bash
# Copy DB to host for GUI viewing
docker compose cp portal:/data/interpretation.db ./interpretation.db

# Or use sqlite3 directly inside the container
docker compose exec portal sqlite3 /data/interpretation.db ".tables"
docker compose exec portal sqlite3 /data/interpretation.db "SELECT * FROM events;"
```

### Check current migration version

```bash
# Inside container
docker compose exec portal uv run alembic current

# Local
uv run alembic current
```

### Create a new migration after model changes

```bash
uv run alembic revision --autogenerate -m "Add new_column to booths"
uv run alembic upgrade head
```

### Reset the database (development only)

```bash
# Local — just delete the file
rm interpretation.db
uv run alembic upgrade head   # recreates everything

# Docker — remove the volume
docker compose down -v         # -v removes volumes
docker compose up -d           # fresh DB created on startup
```

---

## 8. Architecture diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Environment Variable                      │
│  DATABASE_URL=sqlite+aiosqlite:///./interpretation.db       │
│              ──── OR ────                                    │
│  DATABASE_URL=postgresql+asyncpg://user:pass@db:5432/interp │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│              portal/config.py  (Settings)                    │
│  database_url: str = "sqlite+aiosqlite:///./interpretation…"│
└─────────────────────┬───────────────────────────────────────┘
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
┌──────────────────┐   ┌──────────────────────┐
│  portal/database │   │  alembic/env.py      │
│  .py             │   │                      │
│                  │   │  reads database_url   │
│  _get_engine()   │   │  runs migrations     │
│  get_session()   │   │  before app starts   │
│  CRUD helpers    │   │                      │
└────────┬─────────┘   └──────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│          SQLAlchemy Async Engine          │
│  (aiosqlite or asyncpg — transparent)    │
└────────┬─────────────────────────────────┘
         │
         ▼
┌──────────────────────────────────────────┐
│         SQLite file  OR  PostgreSQL      │
│         ./interpretation.db              │
│         postgresql://…/interpretation    │
└──────────────────────────────────────────┘
```

---

## 9. FAQ

**Q: Do I need to install anything extra for PostgreSQL?**
A: Yes — `uv add asyncpg`. The `aiosqlite` driver is already included.

**Q: Will my migrations work on both SQLite and PostgreSQL?**
A: Yes. Alembic generates dialect-appropriate SQL. The initial migration
uses standard types (INTEGER, VARCHAR, DATETIME) that work on both.

**Q: Can I switch from SQLite to PostgreSQL without losing data?**
A: Not automatically. You'd need to export from SQLite and import into
PostgreSQL. For a fresh deployment, just change `DATABASE_URL` and run
`alembic upgrade head` on the empty PostgreSQL database.

**Q: Where are migrations stored?**
A: `alembic/versions/`. These files are committed to git and run
automatically on container startup (`alembic upgrade head` in the
Docker entrypoint).

**Q: What's the `alembic_version` table I see in the viewer?**
A: Alembic creates this automatically to track which migration version
the database is at. Don't modify it manually.
