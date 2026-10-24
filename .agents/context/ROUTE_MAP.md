# VoxBento — Route Map

> All HTTP and WebSocket routes are now modularized in `portal/routers/` and `portal/websockets/`.
> Auth column: `user` = user_token cookie; `session` = session_token cookie; `open` = no auth required; `token/Bearer` = optional legacy token guard.

---

## Public / User-Facing Pages

| Method | Path | Auth | Template | Notes |
|---|---|---|---|---|
| GET | `/` | open | `home.html` | Landing page; no event or booth lists; navbar links personalised if logged in |
| GET | `/local` | open | `local.html` | VoxBento Local desktop console download page with client OS detection & release links |
| GET | `/healthz` | open | — | JSON: `{ok, server, mediamtx_ok}` |
| GET | `/register` | open | `register.html` | Redirects to `/account` if already logged in |
| POST | `/register` | open | `register.html` | Creates user, sets `user_token` cookie → `/account`; if `password` is set, `password_confirm` must match (422 otherwise) |
| GET | `/login` | open | `login.html` | Redirects to `/account` or `?next=` if logged in |
| POST | `/login` | open | `login.html` | Verifies bcrypt, sets `user_token` cookie |
| GET | `/logout` | open | — | Deletes `user_token` cookie → `/` |
| GET | `/account` | user | `account.html` | Account (#767): Profile (display name), Hosting events (Request access / Request sent / has access), Developer API (apply / pending / approved / rejected / suspended), Sign-in (change, set or remove password). Roles live on My events (`/workspace/`) |
| POST | `/account/profile` | user + same-origin | — | Changes `display_name` (whitespace collapsed, 1–200 chars, no control characters) → `/account?success=profile_saved`, or `?error=display_name` |
| POST | `/api/auth/set-password` | user | — | JSON `{password, current_password}`; `current_password` is required when the user already has a password (403 otherwise); min 8 chars |
| POST | `/api/auth/remove-password` | user | — | JSON `{password}`; verifies it, then clears `password_hash` so the user signs in with email links only |
| GET | `/account/hosting-request` | user | `account_hosting_request.html` | Hosting access request form (#768); redirects to `/account` for hosts, Admins and people with an open request |
| POST | `/account/hosting-request` | user + same-origin | `account_hosting_request.html` | Validates, saves `HostingRequest`, emails `HOSTING_REQUESTS_EMAIL` → `/account?success=hosting_request_sent`; 400 re-renders the form with errors |
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
| POST | `/api/auth/token` | `BOOTH_ACCESS_TOKEN` (body) | `{access_token, token_type}` | Issues a globally scoped legacy API JWT; production requires the credential |
| GET | `/api/booth/{booth_id}/state` | configured token/Bearer | booth state dict | Legacy; protected in production |
| GET | `/api/booth/{booth_id}/whip-url` | configured token/Bearer | `{whip_url, channel_id, booth_id}` | Layer-2 active-interpreter enforcement |
| POST | `/api/events/{event_slug}/booths` | configured token/Bearer | booth state + whip/whep URLs | Creates in-memory booth; returns 201; production protected |
| GET | `/api/events/{event_slug}/booths` | configured token/Bearer | `{event_slug, booths:[…]}` | Lists all in-memory booths for event; production protected |
| GET | `/api/events/{event_slug}/booths/{language_code}/state` | configured token/Bearer, or user/session with a role in the booth | booth state dict | 404 if booth not in memory; production protected |
| GET | `/api/events/{event_slug}/booths/{language_code}/whip-url` | configured token/Bearer | `{whip_url, channel_id, booth_id}` | Validates event ownership first; production protected |
| POST | `/api/events/{event_slug}/rooms/{room_id}/booths/{language_code}/transcription/start` / `stop` | configured token/Bearer, or user/session whose booth role can go live (cookie-only requests must be same-origin) | `{status, …}` | Booth page calls these when the interpreter goes live or off live. An interpreter can stop only while live or when nobody is live (checked against the session that joined the live participant) |
| GET | `/api/interpreter/status/{channel_id:path}` | open | `{channel_id, state, reachable}` | MediaMTX reachability (preflight check) |

---

## WebSocket Endpoints

| Path | Auth | Protocol |
|---|---|---|
| `/ws/booth/{booth_id}` | optional JWT via `?token=` + cookies (`session_token` or `user_token`) | See WebSocket Protocol in `REPOSITORY_CONTEXT.md` |
| `/ws/captions/{booth_id}` | protected via `resolve_ws_auth` (requires `BOOTH_ACCESS_TOKEN` check, valid JWT via `?token=` or session cookies `user_token`/`session_token` in production; open in dev when unset) | Receives `booth:state`, `caption`, and `translated_caption` messages; listener captions feed. Supports `{event_slug}-{room_id}-{language_code}` (`floor-{room_id}` allowed only in unauthenticated dev) |
| `/ws/tts/{room_id}/{language_code}/{booth_id}` | pre-authorized (JWT query param, session token, or listener join code cookie) | Receives framed binary bundle messages (1-byte version `0x01`, 4-byte big-endian header length `L`, `L`-byte UTF-8 JSON header, optional 24 kHz 16-bit mono PCM payload) |

---

## Management Surfaces (`/workspace/*` and `/admin/*`)

`/workspace/*` is the canonical event-management surface for event owners. Room coordinators use `/mission-control/*`, which filters the operational view to their assigned rooms. `/admin/*` is reserved for Admins, apart from legacy room-scoped coordinator handlers retained for compatibility. All admin and workspace routes require a `user_token` with `is_admin=True`, or the appropriate `event_owner` / `room_coordinator` membership for scoped routes. The paths in the table below name the shared `/admin` handler routes; current event-management paths have explicit event-owner mappings formed by replacing `/admin` with `/workspace`. Likewise, the listed `/api/admin/providers/...` and `/api/admin/events/...` routes have explicit `/api/workspace/...` event-owner mappings. New admin routes are not exposed through the workspace unless they are added to that allowlist.

**Access checks (#757).** Routes use dependencies named after who they admit (Admins pass all of them):
`require_admin` (Admins only), `require_event_owner` (owners of the event in the path),
`require_owner_or_coordinator` (owners, or coordinators of the room in the path) and
`require_room_event_access` (floor transcription: owners and coordinators of the room).
Every change to an event, its rooms, booths, members, invite links, keys and settings, and the
setup, keys and members pages, use `require_event_owner`; coordinators keep read-only room and
booth pages, transcripts, Mission Control and floor transcription. A Room coordinator given per
event coordinates every room of it; given per booth, that booth's room. Role assignments are
checked with `portal.roles.assignment_error` before anything is created; a refused one redirects
with `?error=invalid_role&role=…` and the page explains why.

`/workspace/` itself is **My events** (#766): its own route, open to every signed-in user, not mapped onto the admin dashboard. Legacy organizer requests to `/admin/` still redirect there. Legacy organizer requests to allowlisted event-management paths under `/admin/events...`, `/admin/setup...`, or their management API equivalents receive a `307` redirect to the matching workspace URL. The redirect preserves the HTTP method and query string. System-only and unknown routes, including `/admin/users/`, `/admin/developer-accounts`, and `/admin/login`, have no workspace equivalent. OAuth routes under `/oauth/*` are unchanged. General workspace entry points such as the dashboard and setup require ownership of at least one event; routes containing `{event_id}` require ownership of that specific event.

| Method | Path | Template | Notes |
|---|---|---|---|
| GET | `/admin/login` | — | `303` → `/login?next=/admin/` (admins use the shared sign-in page) |
| GET | `/admin/logout` | — | Deletes session cookies → `/login` |
| GET | `/admin/` | `admin/dashboard.html` | System-wide events for Admins |
| GET | `/workspace/` | `admin/my_events.html` | **My events** (#766): every event the user has a role in, once per event, with roles in plain language and Manage event / Open Mission Control / Enter booth buttons; New event for hosting access and Admins; `303` → `/login?next=/workspace/` when signed out |
| GET | `/admin/events/` | `admin/event_list.html` | Search by event slug or display name with `?search=`; blank queries show all events; `%` and `_` are literal; pagination URL-encodes the term and clamps out-of-range pages |
| POST | `/admin/events/` | — | Creates event (slug + display_name) |
| GET | `/admin/events/{event_id}/` | `admin/event_detail.html` | Event + rooms + booths |
| GET | `/admin/events/{event_id}/api-settings/` | `admin/api_settings.html` | View encrypted API keys |
| POST | `/admin/events/{event_id}/api-settings` | — | Update transcription API keys (Fernet-encrypted) |
| POST | `/admin/events/{event_id}/delete` | — | Cascade-deletes event |
| GET | `/admin/events/{event_id}/rooms/` | `admin/room_list.html` | Supports optional `search` query parameter to filter rooms server-side against `Room.display_name` (case-insensitive `ilike` match with SQL wildcard escaping for `%`, `_`, and `\\`). |
| POST | `/admin/events/{event_id}/rooms/` | — | Creates room; auto-generates Jitsi URL |
| GET | `/admin/events/{event_id}/rooms/{room_id}/` | `admin/room_detail.html` | Room + booths |
| POST | `/admin/events/{event_id}/rooms/{room_id}/edit` | — | Updates jitsi_url + relay_booth_id |
| POST | `/admin/events/{event_id}/rooms/{room_id}/ai-vocabulary/upload` | — | Upload CSV glossary entries for room AI interpretation |
| GET | `/admin/events/{event_id}/rooms/{room_id}/ai-vocabulary/export` | — | Export room AI interpretation glossary as CSV |
| POST | `/admin/events/{event_id}/rooms/{room_id}/ai-vocabulary/delete` | — | Delete specific entry or clear all room glossary entries |
| POST | `/admin/events/{event_id}/rooms/{room_id}/delete` | — | Deletes room and dependent data only if room belongs to path event; 404 on missing/mismatched room |
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
| GET | `/admin/users/` | `admin/user_list.html` | Users (#765): `search` (name or email), `show` (`all`, `admins`, `hosting`, `deactivated`, `unverified`), sortable User/Joined (`sort_by`/`sort_order`, default `created_at` desc), pagination; on/off switches for Hosting access, Admin and Active per row (`static/js/admin-users.js`) |
| GET | `/admin/users/{user_id}/` | `admin/user_detail.html` | User page (#765): Platform access (hosting, Admin), Roles in events (read-only; owners manage them), Events owned, Account facts, Danger zone |
| POST | `/admin/users/{user_id}/access` | — | Same-origin; form `field` (`hosting`/`admin`/`active`/`verified`) + `value` (`true`/`false`). JSON `{ok, field, value, message}` when `Accept: application/json`, else `303` back with `?notice=`/`?error=`. Rules in `portal/user_admin.py`: no changing your own Admin or Active status, at least one active Admin always remains, hosting only for active accounts that are set up, Admins always host |
| POST | `/admin/users/{user_id}/toggle-active` | — | Older form endpoint; same-origin; flips Active through the same rules |
| POST | `/admin/users/{user_id}/toggle-admin` | — | Older form endpoint; same-origin; flips Admin through the same rules |
| POST | `/admin/users/{user_id}/delete` | — | Same-origin; `confirm_email` must equal the account's email; never your own account or the last active Admin; deletes the user and all their roles |
| GET | `/admin/emails/` | `admin/emails.html` | Admin-only template registry and redacted delivery history |
| GET | `/admin/emails/{key}/` | `admin/email_template.html` | Admin-only draft editor, safe preview and test-send controls |
| POST | `/admin/emails/{key}/draft` | — | Admin + same-origin; validates allowed placeholders and saves a draft |
| POST | `/admin/emails/{key}/publish` | — | Admin + same-origin; publishes the draft and records a revision |
| POST | `/admin/emails/{key}/test` | — | Admin + same-origin; queues a safe example message to the signed-in Admin |
| GET | `/admin/hosting-access/` | `admin/hosting_access.html` | "People with access" tab: hosts and pending hosting invitations, events owned, Remove access / Cancel invitation (#768) |
| GET | `/admin/hosting-access/requests/` | `admin/hosting_access.html` | "Requests" tab: open hosting requests, newest first |
| POST | `/admin/hosting-access/requests/{request_id}/grant` | — | Same-origin; sets `can_host`, closes request as `granted`, emails the person |
| POST | `/admin/hosting-access/requests/{request_id}/decline` | — | Same-origin; `reason` required (≤1000 chars), closes request as `declined`, emails the reason |

---

## Route Ownership

| Module | Owns |
|---|---|
| `portal/routers/public.py` | Home page, health check, registration |
| `portal/routers/auth.py` | Login, logout, invite-token validation, and user profile page |
| `portal/routers/interpreter.py` | Booth UI route |
| `portal/routers/listener.py` | WHEP listener UI route |
| `portal/routers/api.py` | Booth state REST API, WHIP URL generation |
| `portal/routers/admin.py` | Shared event-management handlers and system-admin routes |
| `portal/workspace_routing.py` | `/workspace` handler mapping, legacy organizer redirects, and template URL context |
| `portal/websockets/manager.py` | `ws_booth` and `ws_captions` endpoints |
| `portal/websockets/handlers.py` | Specific `_handle_*` logic for WS messages |
| `fastapi_app.py` | Application lifespan, router include aggregation |

## Program ingest routes (#690)

- `POST /internal/media-auth`: private MediaMTX callback only, blocked by Caddy;
  requires the shared `MEDIAMTX_AUTH_HOOK_SECRET`, then checks canonical floor
  identity, source mode, digest, expiry, capacity and per-IP failure rate.
  Interpreter booth paths require a single-use ticket from
  `booth:request-publish-ticket` (WebRTC only, same per-IP throttle, #783).
- `GET/POST /workspace/events/{event_id}/rooms/{room_id}/program-ingest`:
  event-owner controls/status, mapped to shared `/admin/...` handlers. No-store;
  rotate/enable responses reveal a new token once. POST supports enable, rotate,
  revoke, disable and sync. Revoke returns 202 with `cleanup_pending` when the
  credential is invalidated but MediaMTX cleanup must be retried.
  `portal/routers/program_ingest.py` owns both routes.
- `POST/DELETE /workspace/events/{event_id}/rooms/{room_id}/program-sender`:
  event-owner, origin-checked browser sender session mint/release. `sync_offset_ms`
  is optional; omitted keeps the room's saved sync delay.
- `GET /workspace/events/{event_id}/rooms/{room_id}/sender/`: event-owner standalone
  sender page (`admin/program_sender.html`, `program-sender.js`), no-store. Both
  sender routes map to shared `/admin/...` handlers.
- Existing floor-bot start/stop returns 409 while program ingest owns the room.

## Program relay routes (#749)

- `POST .../program-ingest` also accepts `mode` (`whip` | `rtmps_relay`) on enable;
  relay-mode enable/rotate responses add `stream_key` (`floor?key=<secret>`). Status
  adds `mode`, `relay_available`, `rtmps_server`, write-only `destination`
  (`configured`, `server_url`, `updated_at`) and `health.{ingest,relay,processing}`.
- `PUT/DELETE /workspace/events/{event_id}/rooms/{room_id}/program-relay`: event-owner,
  origin-checked. PUT `{server_url, stream_key}` validates RTMPS + allow-listed host and
  stores the key encrypted (422 invalid, 409 relay disabled). DELETE clears it and stops
  the relay (202 + `cleanup_pending` if the relay service is unreachable).
- `/internal/media-auth` accepts `protocol: rtmp` only for relay-mode rooms with query
  exactly `key=<secret>`; WHIP is refused for relay-mode rooms.
- `program-relay` service (private, `Authorization: Bearer $PROGRAM_RELAY_CONTROL_SECRET`):
  `GET /relays`, `PUT /relays/{room_id}` `{source, destination, fingerprint}`,
  `DELETE /relays/{room_id}`, unauthenticated `GET /healthz`.

## Meeting source routes (#747)

- `GET/POST /workspace/events/{event_id}/rooms/{room_id}/meeting-source`:
  event-owner status and controls, mapped to shared `/admin/...` handlers in
  `portal/routers/meeting_source.py`. POST `{action: connect|disconnect,
  meeting_url?, confirm_source_change?}`. Connect returns 422 for an invalid URL,
  409 `{code: source_conflict}` while program ingest owns the floor (retry with
  `confirm_source_change: true`), and 502 when the floor-bot service is
  unreachable. Disconnect returns 202 with `cleanup_pending` if the agent stop
  must be retried. Responses never include the meeting URL.
- `POST /api/rooms/{room_id}/floor-transcription/start|stop` and
  `GET .../status` (room coordinators and owners) delegate to the same managed
  lifecycle. Start no longer launches the transcription worker directly; the
  reconcile loop starts it once audio arrives. Stop returns 202 with
  `cleanup_pending` when the agent could not be confirmed stopped. Status keeps
  the legacy `running`, `stage` and `bot_reachable` fields with their original
  meaning: `stage` is the raw floor-bot stage (`launching`, `in_meeting`, `dead`, …;
  `unknown` when the service is unreachable); the managed lifecycle is in `state`.

`POST /login` requires verified email and uses the `login` rate-limit bucket:
10 attempts per normalized email per hour. There is no separate admin sign-in form.
