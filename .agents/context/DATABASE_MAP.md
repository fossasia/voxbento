# VoxBento — Database Map

> Derived from `portal/models.py`, `portal/database.py`, and `alembic/versions/`.
> Engine: SQLAlchemy 2.0 async. Dev: SQLite (`aiosqlite`). Prod: PostgreSQL (`asyncpg`).

---

## Table Overview

```
events (1)
  └── rooms (n)  ─── relay_booth_id → booths.id (nullable FK, SET NULL)
        └── room_memberships (n) ─── user_id → users.id
  └── booths (n)
        └── invite_tokens (n)
        └── booth_memberships (n) ─── user_id → users.id

users (1)
  └── event_memberships (n) ─── event_id → events.id
  └── room_memberships (n) ─── room_id → rooms.id
  └── booth_memberships (n) ─── booth_id → booths.id
  └── hosting_requests (n) ─── at most one with status 'open'
```

---

## Table Schemas

### `events`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `slug` | String(64) UNIQUE | validated: lowercase alphanumeric + hyphens; used in URLs and booth identity |
| `display_name` | String(200) | — |
| `listener_join_code` | String(64) nullable | Unique code for listener portal access |
| `transcription_api_enabled` | Boolean | Default False; guards use of external transcription APIs |
| `openai_api_key` | Text nullable | Fernet-encrypted via `portal.crypto.encrypt_val` |
| `deepgram_api_key` | Text nullable | Fernet-encrypted |
| `nvidia_api_key` | Text nullable | Fernet-encrypted |
| `elevenlabs_api_key` | Text nullable | Fernet-encrypted |
| `translation_openai_api_key` | Text nullable | Fernet-encrypted |
| `openrouter_api_key` | Text nullable | Fernet-encrypted |
| `gemini_api_key` | Text nullable | Fernet-encrypted |
| `anthropic_api_key` | Text nullable | Fernet-encrypted |
| `groq_api_key` | Text nullable | Fernet-encrypted |
| `created_at` | DateTime(tz) | UTC |

Cascade: deletes rooms + booths when event is deleted.

---

### `rooms`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `event_id` | FK → events.id CASCADE | — |
| `display_name` | String(200) | — |
| `eventyay_room_id` | String(200) nullable | Future Eventyay room linkage |
| `jitsi_url` | String(500) nullable | Full Jitsi meeting URL for this room; overrides default |
| `relay_booth_id` | FK → booths.id SET NULL nullable | Points to the booth whose WHEP stream is relayed in this room |
| `floor_transcription_enabled` | Boolean | Default False |
| `floor_transcription_provider` | String(20) | Default `'local'` |
| `floor_transcription_model` | String(40) | Default `'tiny'` |
| `floor_language_code` | String(10) nullable | — |
| `floor_translation_enabled` | Boolean | Default False |
| `floor_translation_provider` | String(50) nullable | — |
| `floor_translation_model` | String(100) nullable | — |
| `floor_source_language_code` | String(20) | Default `'en'` |
| `floor_tts_enabled` | Boolean | Default False; controls whether TTS is generated for floor audio translations |
| `floor_tts_provider` | String(20) | Default `'deepgram'`; TTS engine — `'deepgram'` (cloud) or `'supertonic'` (self-hosted ONNX) |
| `floor_tts_voice` | String(50) | Default `'M1'`; Supertonic preset voice (M1–M5, F1–F5) |
| `audio_delay_ms` | Integer | Default 0; optional listener-side WHEP playback delay for all sources in the room |
| `created_at` | DateTime(tz) | UTC |

---

### `booths` (mapped as `DBBooth` in Python)

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `event_id` | FK → events.id CASCADE | — |
| `room_id` | FK → rooms.id CASCADE | — |
| `language_code` | String(2) | ISO 639-1; validated by `validate_language_code` |
| `language_name` | String(100) | Human-readable (e.g. "French") |
| `transcription_enabled` | Boolean | Default False |
| `transcription_provider` | String(20) | Default `'local'`; validated against `ProviderEnum` |
| `transcription_model` | String(20) | Default `'tiny'`; validated against `ALLOWED_MODELS` |
| `broadcast_unlocked` | Boolean | Default False; allows unauthenticated ingest |
| `translation_enabled` | Boolean | Default False |
| `translation_provider` | String(50) nullable | — |
| `translation_model` | String(100) nullable | — |
| `created_at` | DateTime(tz) | UTC |

Unique index on `(event_id, language_code)` — one booth per language per event.

**`mediamtx_path` is a runtime property** — NOT stored: `make_mediamtx_path(event.slug, language_code)` requires `event` relationship to be loaded.

---

### `invite_tokens`

| Column | Type | Notes |
|---|---|---|
| `token` | String(64) PK | `secrets.token_hex(32)` — 32 bytes of entropy |
| `booth_id` | FK → booths.id CASCADE | — |
| `role` | String(20) | Validated against `portal.roles.ALL_ROLES` |
| `label` | String(200) | Human label for admin panel display |
| `expires_at` | DateTime(tz) nullable | None = never expires |
| `used_at` | DateTime(tz) nullable | Set on redemption; non-null = used/revoked |
| `created_by` | String(200) | Admin email or identifier |
| `created_at` | DateTime(tz) | UTC |

Properties: `is_expired` (compares UTC), `is_used` (used_at is not None).

---

### `users`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `email` | String(200) UNIQUE | Stored lowercase; indexed |
| `display_name` | String(200) | — |
| `password_hash` | String(200) | bcrypt via `portal.auth.hash_password` |
| `is_active` | Boolean | Default True; deactivated users cannot login |
| `is_admin` | Boolean | Default False; grants full admin panel access |
| `created_at` | DateTime(tz) | UTC |

---

### `event_memberships`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `user_id` | FK → users.id CASCADE | — |
| `event_id` | FK → events.id CASCADE | — |
| `role` | String(20) | e.g. `event_owner` |

Unique on `(user_id, event_id)` — one role per user per event.

---

### `room_memberships`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `user_id` | FK → users.id CASCADE | — |
| `room_id` | FK → rooms.id CASCADE | — |
| `role` | String(20) | e.g. `room_coordinator` |

Unique on `(user_id, room_id)`.

---

### `booth_memberships`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `user_id` | FK → users.id CASCADE | — |
| `booth_id` | FK → booths.id CASCADE | — |
| `role` | String(20) | e.g. `interpreter` |

Unique on `(user_id, booth_id)`.

---

### `transcript_segments`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `event_id` | FK → events.id CASCADE | — |
| `channel_id` | String(100) | `floor-{room_id}` or `{booth_id}` |
| `text` | Text | The final canonical transcription text |
| `start_time` | Float | Timestamp from audio stream |
| `end_time` | Float | Timestamp from audio stream |
| `created_at` | DateTime(tz) | UTC |

---

### `transcript_translations`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `segment_id` | FK → transcript_segments.id CASCADE | Links translation directly to canonical transcript |
| `language_code` | String(20) | ISO 639-1 target language |
| `text` | Text | The translated text |
| `created_at` | DateTime(tz) | UTC |

Unique index on `(segment_id, language_code)`.

---

### `room_translation_languages` & `booth_translation_languages`

| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | — |
| `room_id` / `booth_id` | FK CASCADE | Target entity |
| `language_code` | String(20) | ISO 639-1 target language |
| `language_name` | String(100) | Human readable |
| `enabled` | Boolean | Default True |

Tracks which languages the translation worker should generate for a given room or booth.

---

## Alembic Migration Chain

| ID | File | What it adds |
|---|---|---|
| 001 | `001_initial_schema.py` | `events`, `rooms`, `booths`, `invite_tokens` |
| 002 | `002_add_users.py` | `users` table |
| 003 | `003_event_memberships.py` | `event_memberships` |
| 004 | `004_booth_memberships.py` | `booth_memberships` |
| 005 | `005_add_jitsi_url_to_room.py` | `rooms.jitsi_url` column |
| 006 | `006_add_relay_booth_id_to_room.py` | `rooms.relay_booth_id` FK |
| 007 | `007_add_transcription_config.py` | `booths.transcription_enabled`, `booths.transcription_model` |
| 008 | `008_add_and_encrypt_api_keys.py` | `booths.transcription_provider`, `events.{openai,deepgram,nvidia,elevenlabs}_api_key`, `events.transcription_api_enabled` |
| 009 | `009_add_floor_transcription.py` | `rooms.floor_transcription_enabled`, `rooms.floor_language_code`, `rooms.floor_transcription_provider`, `rooms.floor_transcription_model` |
| 010 | `010_add_transcriptsegment_model.py` | `transcript_segments` table |
| 011 | `011_add_translation_models.py` | `transcript_translations`, `room_translation_languages` tables, translation API keys in `events`, floor translation settings in `rooms` |
| 012 | `012_add_booth_translation_settings.py` | `booth_translation_languages` table, `booths.translation_enabled/provider/model` |
| 013 | `013_add_broadcast_unlocked_to_booths.py` | `booths.broadcast_unlocked` column |
| 014 | `014_rbac_refactor.py` | `room_memberships` table, `events.listener_join_code` |
| 015 | `015_add_floor_tts_enabled.py` | `rooms.floor_tts_enabled` |
| 016 | `016_add_room_audio_delay.py` | `rooms.audio_delay_ms` |
| 017 | `017_add_tts_provider_fields.py` | `rooms.floor_tts_provider`, `rooms.floor_tts_voice` |
| 018 | `018_add_authtoken_and_user_email_verified.py` | `users.email_verified`, `auth_tokens` table |
| 019 | `019_add_api_keys_and_usage_metrics.py` | `api_keys`, `usage_metrics` tables |
| 020 | `020_multi_room_architecture.py` | multi-room booth support (`booths.room_id`, unique index `(room_id, language_code)`) |
| 021 | `021_add_oauth2_and_developer_platform_models.py` | `oauth_clients`, `oauth_tokens`, `developer_accounts`, `oauth_audit_logs` |
| 022 | `022_add_webhooks.py` | `webhooks`, `webhook_deliveries` tables |
| 023 | `023_phase_4_models_update.py` | model relationships and index updates |
| 024 | `024_make_oauth_audit_logs_client_id_nullable.py` | nullable `client_id` in audit logs |
| 025 | `025_program_ingest.py` | `rooms.floor_source`, `rooms.program_key_hash`, `rooms.program_key_expires_at`, `rooms.program_sync_offset_ms`, `rooms.floor_caption_seq` |
| 026 | `026_program_sender_session.py` | `rooms.program_session_id`, `program_connected_at`, `program_disconnected_at` |
| 027 | `027_meeting_agent.py` | `rooms.meeting_agent_enabled` (desired state for Jitsi floor agent) |
| 028 | `028_program_relay.py` | `rooms.program_ingest_mode`, `rooms.program_relay_server`, `rooms.program_relay_key_enc`, `rooms.program_relay_updated_at` |
| 029 | `029_add_can_host_to_users.py` | `users.can_host`, `users.hosting_requested_at`, `hosting_invitations` table |
| 030 | `030_hosting_requests.py` | `hosting_requests` table with partial unique index for open requests |
| 031 | `031_add_ai_interpretation_settings.py` | `rooms.floor_ai_interpreter_persona`, `rooms.floor_ai_interpretation_style`, `rooms.floor_ai_vocabulary_enabled`, `ai_vocabulary_entries` table |

Run migrations: `uv run alembic upgrade head`

---

## Key CRUD Functions (`portal/database.py`)

| Function | Table | Notes |
|---|---|---|
| `create_event`, `get_event_by_slug`, `get_event_by_id`, `list_events`, `delete_event` | events | — |
| `create_room`, `get_room_by_id`, `list_rooms_for_event`, `delete_room` | rooms | — |
| `create_booth`, `get_booth_by_id`, `list_booths_for_event`, `list_booths_for_room`, `delete_booth` | booths | `get_booth_by_id` joinedloads `event` for `mediamtx_path` property |
| `create_invite_token`, `get_invite_token`, `redeem_invite_token`, `list_tokens_for_booth`, `revoke_invite_token` | invite_tokens | `redeem_invite_token` raises ValueError on used/expired |
| `create_user`, `get_user_by_email`, `get_user_by_id`, `list_users`, `update_user_active`, `delete_user` | users | — |
| `set_event_membership`, `remove_event_membership`, `list_memberships_for_user`, `list_memberships_for_event` | event_memberships | upsert pattern |
| `set_room_membership`, `remove_room_membership`, `list_memberships_for_room`, `list_room_memberships_for_user` | room_memberships | upsert pattern |
| `set_booth_membership`, `remove_booth_membership`, `list_memberships_for_booth`, `list_booth_memberships_for_user` | booth_memberships | upsert pattern |
| `create_hosting_request`, `get_hosting_request`, `get_open_hosting_request`, `list_open_hosting_requests`, `count_open_hosting_requests`, `close_hosting_request`, `close_open_hosting_request` | hosting_requests | `close_*` records the outcome and clears `users.hosting_requested_at` |

### Session Pattern

```python
from portal.database import get_session
async with get_session() as session:
    # session.begin() is entered automatically
    result = await some_crud_fn(session, ...)
    # auto-commit on exit, auto-rollback on exception
```

### Engine Configuration

- Lazy init on first use via `_get_engine()`.
- `configure(url)` overrides engine (used in tests).
- `init_db()` creates all tables without Alembic (tests only).
- `dispose()` disposes connection pool.
## Migration 030 — hosting access requests (#768)

Table `hosting_requests`: `user_id` (FK users, CASCADE), `organization`, `event_name`,
nullable `event_starts_on` / `event_ends_on` (Date), `languages`, `message`, `status`
(`open` | `granted` | `declined`), `decline_reason`, `decided_by_user_id` (FK users,
SET NULL), `decided_at`, `created_at`. The partial unique index
`uq_hosting_requests_open_user` (`WHERE status = 'open'`) allows one open request per
user. Handled requests are kept for the record. `users.hosting_requested_at` mirrors
the open request's creation time and is cleared when it is handled; the homepage demo
form no longer sets it.

## Migration 031 — AI interpretation settings and vocabulary entries (#639)

Room fields: `floor_ai_interpreter_persona` (Text, nullable), `floor_ai_interpretation_style`
(Text, nullable), `floor_ai_vocabulary_enabled` (Boolean, default true).
Table `ai_vocabulary_entries`: indexed glossary terms scoped to event, room, or booth.
Fields: `id`, `event_id`, `room_id` (nullable), `booth_id` (nullable), `source_term` (String(255)),
`target_language` (String(20), default `all`), `target_term` (String(255)), `description` (Text),
`case_sensitive` (Boolean, default false), `match_type` (String(20), `phrase` default | `exact`),
`priority` (Integer, default 0), `created_at`. Scopes cascade on delete.

## Migration 028 — RTMPS relay mode (#749)

Room fields: `program_ingest_mode` (`whip` default | `rtmps_relay`; binds the program
key to its transport), nullable `program_relay_server` (normalized allow-listed RTMPS
URL), `program_relay_key_enc` (Fernet ciphertext of the YouTube stream key; write-only,
never serialize or log) and `program_relay_updated_at`. Helpers live in
`portal/program_relay.py` (`store_destination`, `clear_destination`, `destination`).

## Migration 034 — transactional email outbox (#962)

`email_templates` stores draft and published subject/HTML for every registered
notification type. `email_template_revisions` retains immutable published
versions. `email_deliveries` contains the encrypted rendered subject and HTML,
delivery status, retry lease and schedule fields, expiry, an optional
idempotency key and an optional `auth_token_id`. That ties an email carrying a
sign-in or invitation link to its `auth_tokens` row (a plain integer, not a
foreign key, so the id survives the token's deletion), so the outbox suppresses
it once the token is redeemed, revoked, expired or deleted. The worker in
`portal/notifications.py` claims due rows, retries failed sends with back-off,
marks pending rows past `expires_at` as expired, and erases the rendered
subject and HTML of finished deliveries after 30 days (the audit row stays).
No links or email content are shown in the administration history.
`email_settings` holds one row (id 1) of branding shared by every email:
`show_logo` (default true) and an optional https `logo_url` (empty means the
VoxBento logo served by the site). No row means the defaults.

## Migration 027 — managed meeting agent

Room field `meeting_agent_enabled` (Boolean, default false): the durable desired
state of the managed Jitsi floor agent. Only meaningful while `floor_source` is
`jitsi_bot`; enabling program ingest clears it. `portal/meeting_source.py`
reconciles it against floor-bot `/status` and restores state after a portal restart.

## Migration 025 — program floor ownership

Room fields: `floor_source` (default `jitsi_bot`), nullable `program_key_hash`
(PBKDF2-HMAC-SHA256 digest; never serialize), `program_key_expires_at`, `program_session_id`,
`program_connected_at`, `program_disconnected_at`, plus `program_sync_offset_ms`
and `floor_caption_seq` (both default 0). Source configuration uses a serialized
transaction; the MediaMTX auth callback reserves a session before negotiation.
Final floor sequence increments atomically and survives worker/portal restarts.
Raw credentials are only returned at create/rotate. YouTube keys exist only in
relay mode, encrypted (migration 028).
