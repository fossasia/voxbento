# VoxBento — Change Impact Map

> Use this map to determine which files need to change for a given task.
> Derived from live implementation. Cross-reference with [ROUTE_MAP.md](ROUTE_MAP.md) and [DATABASE_MAP.md](DATABASE_MAP.md).

---

## 1. Adding a New Language to a Booth

**Scope:** Admin operation, not a code change.
- Admin creates a new `DBBooth` via `/admin/events/{id}/rooms/{id}/booths/` (form POST).
- The `language_code` must be a valid ISO 639-1 code (`portal/booth_identity.py`).
- MediaMTX path is derived automatically.
- No code changes required.

---

## 2. Adding a New Booth Role

**Files to change:**
1. `portal/booth_state.py` — add to `ParticipantRole` Literal type
2. `portal/roles.py` — add to `ROLE_PERMISSIONS`, `_ROLE_RANK`, `ALL_ROLES`
3. `portal/auth.py` — update `can_perform_role` / `resolve_booth_role` if needed
4. `portal/websockets/handlers.py` — update WS join handler `_handle_join` if role has special logic
5. `templates/interpreter_booth.html` — update role display if shown in UI
6. `static/js/interpreter-booth.js` — update role-based UI gating
7. `tests/test_roles.py` — add role permission tests

---

## 3. Adding a New Transcription Provider

**Files to change:**
1. `portal/transcription/constants.py` — add to `ProviderEnum`, add to `ALLOWED_MODELS`
2. `portal/transcription/providers/` — create `{provider_name}.py` implementing `TranscriptionProvider`
3. `portal/transcription/worker.py` — add to `PROVIDERS` dict
4. `portal/models.py` — if provider needs its own API key column, add it to `Event` model (+ new Alembic migration)
5. `portal/transcription/providers/base.py` — add to `key_map` in `get_api_key` if new column
6. `alembic/versions/` — create new migration for the DB column
7. `templates/admin/api_settings.html` — add form field for new API key
8. `portal/routers/admin/settings.py` — update `admin_event_api_settings_post` handler to handle new key
9. `tests/test_transcription_concurrency.py` — add provider test

---

## 4. Adding a New Page Route

**Files to change:**
1. `portal/routers/` — add route handler to the relevant router (or create a new one)
2. `templates/` — create HTML template (extend `base.html`)
3. `templates/base.html` — add nav link if needed
4. `tests/test_fastapi_app.py` — add route test

---

## 5. Adding a New Database Column

**Files to change:**
1. `portal/models.py` — add `Mapped` column to correct model class
2. `alembic/versions/` — create new migration (e.g. `00N_description.py`)
3. `portal/database.py` — update relevant CRUD functions (create/get if column is part of creation)
4. `portal/routers/` — update any forms/routes that set/read the column
5. `templates/` — update admin template if column is admin-configurable

---

## 6. Changing the JWT Secret / Auth Flow

**Files to change:**
1. `portal/config.py` — `Settings.effective_jwt_secret` (property)
2. `portal/auth.py` — `create_token`, `create_participant_token`, `create_user_token`, `create_admin_token`, `decode_token`
3. All cookie names: `session_token`, `user_token`, `admin_token` — search across `portal/routers/`
4. `static/js/interpreter-booth.js` — `?token=` query param for WS if legacy token auth is changed

---

## 7. Changing Booth Identity / URL Scheme

**Files to change:**
1. `portal/booth_identity.py` — `make_booth_id`, `make_mediamtx_path`, `parse_booth_id`, regex patterns
2. `portal/booth_state.py` — `_get_or_create_booth` parsing logic
3. `portal/routers/` — all calls to `make_booth_id`, `make_mediamtx_path`, route paths
4. `portal/models.py` — `DBBooth.mediamtx_path` property
5. `tests/test_booth_identity.py`
6. `mediamtx.yml` — path config if path format changes

---

## 8. Changing the WebSocket Protocol

**Files to change:**
1. `portal/websockets/` — WS endpoint (`manager.py`) + `_handle_*` functions (`handlers.py`)
2. `static/js/interpreter-booth.js` — client `sendMessage`, `onMessage` handling
3. `portal/booth_state.py` — if new state fields added to `Booth.as_public_dict()`
4. `tests/test_booth_state.py` — if new state operations added

---

## 9. Changing MediaMTX Config

**Files to change:**
1. `mediamtx.yml` — WHIP/WHEP path config, overridePublisher, alwaysAvailable
2. `docker-compose.yml` — port mappings if changed
3. `portal/config.py` — `mediamtx_whip_base`, `mediamtx_api_base` settings
4. `portal/routers/api.py` and `portal/routers/interpreter.py` — WHIP/WHEP URL construction

---

## 10. Adding Docker Services

**Files to change:**
1. `docker-compose.yml` — add service
2. `Dockerfile` — if portal container needs changes
3. `portal/config.py` — add env var for new service URL
4. `README.md` — update setup instructions

---

## 11. Changing Encryption (API Keys)

**Files to change:**
1. `portal/crypto.py` — `get_fernet`, `encrypt_val`, `decrypt_val`
2. `portal/config.py` — `api_key_encryption_key` setting
3. `portal/transcription/providers/base.py` — `get_api_key` (calls `decrypt_val`)
4. `portal/routers/admin/settings.py` — `admin_event_api_settings_post` (calls `encrypt_val`)
5. Existing encrypted DB rows become invalid if key changes — plan rotation

---

## 12. Adding a New Admin Panel Section

**Files to change:**
1. `portal/routers/admin/` — add routes to the appropriate file
2. `templates/admin/` — create templates extending `templates/admin/base.html`
3. `templates/admin/base.html` — add nav entry
4. `portal/database.py` — add CRUD functions if new DB access
5. `tests/test_admin_panel.py`

---

## Files That Affect Everything

| File | Impact if changed |
|---|---|
| `portal/config.py` | All settings — env vars, secrets, URLs |
| `portal/models.py` | DB schema — requires Alembic migration |
| `portal/auth.py` | All auth flows — all routes + WS |
| `portal/booth_identity.py` | Booth ID + MediaMTX path — breaks all in-memory state |
| `portal/routers/` | All routes — every user-facing feature |
| `mediamtx.yml` | Media server behaviour — WHIP/WHEP/transcription |
