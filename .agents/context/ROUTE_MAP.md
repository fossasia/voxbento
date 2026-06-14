# VoxBento — Route Map

> All HTTP and WebSocket routes derived from `fastapi_app.py`.
> Auth column: `user` = user_token cookie; `admin` = admin_token cookie; `session` = session_token cookie; `open` = no auth required; `token/Bearer` = optional legacy token guard.

---

## Public / User-Facing Pages

| Method | Path | Auth | Template | Notes |
|---|---|---|---|---|
| GET | `/` | open | `home.html` | Lists events + booth statuses; personalised if logged in |
| GET | `/healthz` | open | — | JSON: `{ok, server, mediamtx_ok}` |
| GET | `/register` | open | `register.html` | Redirects to `/account` if already logged in |
| POST | `/register` | open | `register.html` | Creates user, sets `user_token` cookie → `/account` |
| GET | `/login` | open | `login.html` | Redirects to `/account` or `?next=` if logged in |
| POST | `/login` | open | `login.html` | Verifies bcrypt, sets `user_token` cookie |
| GET | `/logout` | open | — | Deletes `user_token` cookie → `/` |
| GET | `/account` | user | `account.html` | Shows profile + event memberships |
| GET | `/join/{token}` | open | — | Validates invite token, sets `session_token` → booth or listener |

---

## Booth & Listener Pages

| Method | Path | Auth | Template | Notes |
|---|---|---|---|---|
| GET | `/interpreter/{event_slug}/{language_code}` | session or user | `interpreter_booth.html` | **Preferred URL**; resolves Jitsi URL, relay WHEP, mediamtx path |
| GET | `/interpreter/{booth_id}` | session or user | `interpreter_booth.html` | Legacy free-form booth ID; no event scope |
| GET | `/listener/{event_slug}` | session or user | `listener-event.html` | Lists rooms/booths for event with WHEP URLs |

---

## REST API

| Method | Path | Auth | Returns | Notes |
|---|---|---|---|---|
| POST | `/api/auth/token` | token (body) | `{access_token, token_type}` | Issues bearer JWT if BOOTH_ACCESS_TOKEN matches |
| GET | `/api/booth/{booth_id}/state` | token/Bearer | booth state dict | Legacy; auto-creates booth in memory |
| GET | `/api/booth/{booth_id}/whip-url` | token/Bearer | `{whip_url, channel_id, booth_id}` | Layer-2 active-interpreter enforcement |
| POST | `/api/events/{event_slug}/booths` | token/Bearer | booth state + whip/whep URLs | Creates in-memory booth; returns 201 |
| GET | `/api/events/{event_slug}/booths` | token/Bearer | `{event_slug, booths:[…]}` | Lists all in-memory booths for event |
| GET | `/api/events/{event_slug}/booths/{language_code}/state` | token/Bearer | booth state dict | 404 if booth not in memory |
| GET | `/api/events/{event_slug}/booths/{language_code}/whip-url` | token/Bearer | `{whip_url, channel_id, booth_id}` | Validates event ownership first |
| GET | `/api/interpreter/status/{channel_id:path}` | open | `{channel_id, state, reachable}` | MediaMTX reachability (preflight check) |

---

## WebSocket Endpoints

| Path | Auth | Protocol |
|---|---|---|
| `/ws/booth/{booth_id}` | optional JWT via `?token=` + cookies (`session_token` or `user_token`) | See WebSocket Protocol in `REPOSITORY_CONTEXT.md` |
| `/ws/captions/{booth_id}` | open (no auth) | Receives `booth:state`, `caption` messages; listener captions feed |

---

## Admin Panel (`/admin/*`)

All admin routes require `admin_token` cookie (or `user_token` with `is_admin=True` or `event_owner` membership for event-scoped routes).

| Method | Path | Template | Notes |
|---|---|---|---|
| GET | `/admin/login` | `admin/login.html` | Redirects to `/admin/` if already admin |
| POST | `/admin/login` | — | Sets `admin_token` cookie → `/admin/` |
| GET | `/admin/logout` | — | Deletes `admin_token` → `/admin/login` |
| GET | `/admin/` | `admin/dashboard.html` | Event list with live booth counts + MediaMTX status |
| GET | `/admin/events/` | `admin/event_list.html` | — |
| POST | `/admin/events/` | — | Creates event (slug + display_name) |
| GET | `/admin/events/{event_id}/` | `admin/event_detail.html` | Event + rooms + booths |
| GET | `/admin/events/{event_id}/api-settings/` | `admin/api_settings.html` | View encrypted API keys |
| POST | `/admin/events/{event_id}/api-settings` | — | Update transcription API keys (Fernet-encrypted) |
| POST | `/admin/events/{event_id}/delete` | — | Cascade-deletes event |
| GET | `/admin/events/{event_id}/rooms/` | `admin/room_list.html` | — |
| POST | `/admin/events/{event_id}/rooms/` | — | Creates room; auto-generates Jitsi URL |
| GET | `/admin/events/{event_id}/rooms/{room_id}/` | `admin/room_detail.html` | Room + booths |
| POST | `/admin/events/{event_id}/rooms/{room_id}/edit` | — | Updates jitsi_url + relay_booth_id |
| POST | `/admin/events/{event_id}/rooms/{room_id}/delete` | — | Deletes room |
| GET | `/admin/events/{event_id}/rooms/{room_id}/booths/` | `admin/booth_list.html` | — |
| POST | `/admin/events/{event_id}/rooms/{room_id}/booths/` | — | Creates DBBooth |
| GET | `/admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/` | `admin/booth_detail.html` | DB + live state, tokens, members |
| POST | `…/booths/{booth_id}/members/` | — | Upserts `BoothMembership` by email + role |
| POST | `…/booths/{booth_id}/members/{membership_id}/delete` | — | Removes membership |
| POST | `…/booths/{booth_id}/delete` | — | Deletes DBBooth (cascade) |
| POST | `…/booths/{booth_id}/transcription-settings` | — | Updates provider/model; starts/stops worker |
| POST | `…/booths/{booth_id}/tokens/` | — | Creates InviteToken with optional expiry |
| POST | `…/booths/{booth_id}/tokens/{token_id}/revoke` | — | Marks token as used (revokes) |
| GET | `/admin/events/{event_id}/members/` | `admin/event_members.html` | EventMembership list |
| POST | `/admin/events/{event_id}/members/` | — | Upserts EventMembership by email + role |
| POST | `/admin/events/{event_id}/members/{membership_id}/delete` | — | Removes EventMembership |
| GET | `/admin/users/` | `admin/user_list.html` | — |
| GET | `/admin/users/{user_id}/` | `admin/user_detail.html` | User + event admin assignments |
| POST | `/admin/users/{user_id}/toggle-active` | — | Flips `is_active` |
| POST | `/admin/users/{user_id}/delete` | — | Deletes user |
| POST | `/admin/users/{user_id}/toggle-admin` | — | Flips `is_admin` |
| POST | `/admin/users/{user_id}/events/{event_id}/toggle-admin` | — | Grants/revokes `event_owner` membership |

---

## Route Ownership

| Module | Owns |
|---|---|
| `fastapi_app.py` | **All** routes — no router splitting yet |
| `portal/auth.py` | Auth utilities consumed by routes |
| `portal/booth_state.py` | Business logic for WS handlers |
| `portal/database.py` | CRUD used by page and admin routes |
