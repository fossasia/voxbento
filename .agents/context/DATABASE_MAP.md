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
