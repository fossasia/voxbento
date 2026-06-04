# Admin & User Guide — Eventyay Interpretation Portal

## Quick Start

### 1. Set the admin password

```bash
# In .env or as environment variable
ADMIN_PASSWORD=your-secure-password
```

### 2. Start the portal

```bash
# Docker (recommended)
docker compose up --build

# Or native
uv run uvicorn fastapi_app:app --host 127.0.0.1 --port 8000 --reload
```

### 3. Access the admin panel

1. Open http://localhost:8000/admin/login
2. Enter the `ADMIN_PASSWORD` you set
3. You're in the dashboard

---

## Admin Panel Guide

### Dashboard (`/admin/`)

Shows all events with live booth status and MediaMTX health indicator.

### Creating an Event

1. Go to **Events** → fill in slug (URL-safe, e.g. `pycon2026`) and display name
2. Click **Create Event**
3. Open the event to add rooms and booths

### Creating Rooms

1. Open an event → click **Rooms**
2. Enter room name → **Add Room**

### Creating Booths

1. Open a room → click **Booths**
2. Enter language code (2-letter ISO 639-1, e.g. `en`, `fr`, `es`) and language name
3. Click **Add Booth**

### Booth Detail Page

Each booth detail page shows:
- **Interpreter Page URL** — share this with interpreters
- **WHEP Listener URL** — for attendees (copy button provided)
- **MediaMTX Stream Path** — internal MediaMTX identifier
- **Live Status** — whether an interpreter is currently broadcasting
- **Active Interpreter** — who is currently live
- **Participant Roster** — all connected users with roles
- **Assigned Members** — assign registered users directly to this booth
- **Invite Tokens** — generate, copy, and revoke booth access tokens

### Token Management (on Booth Detail Page)

1. Open a booth detail page
2. In the **Invite Tokens** section, fill in:
   - **Role** — listener, interpreter, or coordinator
   - **Label** — a human-readable label (e.g. "Alice — Spanish interpreter")
   - **Expires in (hours)** — optional; leave empty for tokens that never expire
3. Click **Generate Token**
4. The new token appears in the table below with a **Copy Link** button
5. Share the invite link with the intended participant

#### Revoking a token

- Click **Revoke** next to any active token
- Revoked tokens are marked and cannot be used to join the booth
- This action cannot be undone

---

## User Registration

### How users register

1. Visit http://localhost:8000/register
2. Fill in email, display name, password (min 8 characters)
3. Account is created (non-admin, active)
4. Redirected to account page

### What new users can do

- Browse events on the home page
- View their account info at `/account`
- See which events they have roles for

### What new users cannot do

- Access the admin panel
- Go live as an interpreter (until assigned an interpreter role for an event)
- Manage booths or events

---

## Role Management

### Event-scoped roles

Roles are assigned **per event**, not globally. A user can be an `interpreter` for one event and a `listener` for another.

| Role | Scope | Permissions |
|------|-------|-------------|
| `listener` | Booth / Event | View booths, listen to streams, send chat messages |
| `interpreter` | Booth / Event | All listener permissions + go live (publish audio via WHIP) |
| `coordinator` | Booth / Event | All listener permissions + assign active interpreter, manage handoffs + go live |
| `event_admin` | Event | All coordinator permissions + manage booths and generate invite tokens |

### Assigning event roles

1. Log in to admin panel → open an **Event** detail page
2. Click **Manage Members**
3. Enter the user's registered email address in the **Assign Event Admin** form
4. Click **Assign Role**

### Assigning booth roles (Interpreter assignments)

For stricter access control, you can assign an interpreter to a *specific booth* rather than the whole event:

1. Go to an event's **Rooms** → **Booths** and open a specific booth
2. Scroll to **Assigned Members**
3. Under **Assign Member**, enter the user's registered email address
4. Select `interpreter` from the Role dropdown
5. Click **Assign Member**
6. When the interpreter logs in, they will see an "Open Booth" button on their home dashboard directly to this booth

### Removing a role

1. Go to the event's or booth's **Members** section
2. Click **Remove** next to the user's name

### User admin flags

The **Users** page (`/admin/users/`) shows all registered users with:
- **Admin** badge — whether the user has site-wide admin access
- **Status** — active or deactivated
- **Deactivate/Activate** — toggle login access
- **Delete** — permanently remove the account

---

## Typical Workflow for an Event

### Before the event

1. **Admin** creates event, rooms, and booths in the admin panel
2. **Admin** assigns per-event roles from the event's **Members** page (e.g. interpreter, coordinator)
3. **Admin** generates invite tokens on each booth detail page and shares links
4. **Admin** shares interpreter page URLs with interpreters

### During the event

1. **Interpreters** open their booth URL, join, and go live
2. **Listeners** visit the home page, find their event, and click "Listen"
3. **Coordinators** manage interpreter handoffs within booths
4. **Admin** monitors live status from the dashboard

### After the event

1. Booths stop automatically when interpreters disconnect
2. Admin can delete events/rooms/booths to clean up
3. User accounts persist for future events

---

## Environment Variables Reference

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `ADMIN_PASSWORD` | *(empty)* | Yes (for admin access) | Password for admin panel login |
| `SECRET_KEY` | `change-me` | Yes (production) | JWT signing key — use 64+ random chars |
| `DATABASE_URL` | `sqlite+aiosqlite:///./interpretation.db` | No | Database connection string |
| `MEDIAMTX_WHIP_BASE` | `http://localhost:8889` | No | Browser-facing WHIP/WHEP URL |
| `MEDIAMTX_HLS_BASE` | `http://localhost:8888` | No | Browser-facing HLS URL |
| `JITSI_DOMAIN` | `localhost:8080` | No | Jitsi Meet domain |

---

## Database Commands

```bash
# Apply all migrations (creates tables)
alembic upgrade head

# Check current migration version
alembic current

# After adding/changing models, generate a new migration
alembic revision --autogenerate -m "describe the change"
```

In Docker, migrations run automatically on container startup.
