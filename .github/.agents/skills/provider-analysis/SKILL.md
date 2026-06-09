# Skill: Provider Analysis

> Use this skill when integrating, debugging, or auditing third-party provider integrations
> (transcription APIs, MediaMTX, Jitsi, future Eventyay API integration).

---

## MediaMTX

**Role:** Media server — WHIP ingest, WHEP playback, RTSP for transcription.

### Key integration points

| Endpoint | Used by | Purpose |
|---|---|---|
| `{MEDIAMTX_WHIP_BASE}/{path}/whip` | Browser (JS) | Interpreter audio ingest |
| `{MEDIAMTX_WHIP_BASE}/{path}/whep` | Browser (JS) | Listener audio playback |
| `rtsp://mediamtx:8554/{path}` | ffmpeg (Python) | Transcription audio source |
| `{MEDIAMTX_API_BASE}/v3/config/paths/add/{path}` | FastAPI (`_ensure_mediamtx_path`) | Create named path with `alwaysAvailable` |
| `{MEDIAMTX_API_BASE}/v3/config/paths/patch/{path}` | FastAPI (fallback) | Update existing path config |
| `{MEDIAMTX_API_BASE}/v3/paths/list` | FastAPI (`_check_mediamtx`) | Reachability check |

### `alwaysAvailable` path creation
- Called before returning WHIP URL or rendering booth page.
- Creates named path so WHEP readers survive publisher handoffs.
- Cache: `_created_paths` set prevents duplicate API calls.
- Idempotent: if path exists, PATCHes it to ensure `alwaysAvailable: True`.

### `overridePublisher`
Set in `mediamtx.yml` — allows a new WHIP publisher to kick the previous one. Enables seamless handoff when coordinator reassigns active interpreter.

### Reachability check
```python
await _check_mediamtx()  # returns True if API responds < 500
```
Used by `/healthz` and booth page template.

---

## Jitsi Meet

**Role:** Floor session monitoring — receive-only iframe embedded in interpreter booth.

### Integration
- Embedded as an iframe in `templates/interpreter_booth.html`.
- URL: room-specific Jitsi URL from `DBBooth.room.jitsi_url` (admin-configured) OR `_make_jitsi_url(base_url, default_jitsi_room)`.
- URL format: `{effective_jitsi_base_url}/{room_name}` (if room is not already a full URL).
- Domain passed to template as `jitsi_domain` (derived from `effective_jitsi_base_url`).
- Self-hosted; image: `jitsi/*:stable-9823`.

### Jitsi room naming convention (auto-generated)
When admin creates a room, a Jitsi URL is auto-generated:
```python
clean_name = re.sub(r'[^a-zA-Z0-9]+', '', display_name)
room_id_str = f"Voxbento-{event.slug}-{clean_name}"
jitsi_url = _make_jitsi_url(effective_jitsi_base_url, room_id_str)
```

### Critical: Jitsi is monitoring only
Never use Jitsi's audio as the broadcast source. Interpreter mic → WHIP → MediaMTX is the only valid ingest path.

---

## OpenAI (Transcription)

- whisper-1: `POST https://api.openai.com/v1/audio/transcriptions` — WAV file upload per 3-second chunk.
- gpt-4o-realtime: `wss://api.openai.com/v1/realtime?model={model}` — WebSocket streaming with PCM frames.
- Auth: `Authorization: Bearer {api_key}` header.
- API key: stored encrypted as `events.openai_api_key`; requires `event.transcription_api_enabled = True`.
- Retry: tenacity, exponential backoff 2–10s, max 3 attempts on 429/5xx.
- HTTP client: `portal.transcription.shared_http_client` (shared `httpx.AsyncClient`).

---

## Deepgram (Transcription)

- `wss://api.deepgram.com/v1/listen?model=nova-2&language={lang}&encoding=linear16&sample_rate=16000&channels=1&interim_results=true&keepalive=true&endpointing=2000&smart_format=true&punctuate=true`
- Auth: `Authorization: Token {api_key}`.
- Protocol: WebSocket streaming — send raw PCM, receive JSON transcripts.
- KeepAlive: sends `{"type":"KeepAlive"}` every 5 seconds if no audio.
- API key: `events.deepgram_api_key`.

---

## NVIDIA Riva (Transcription)

- Parakeet-RNNT / Parakeet-CTC via Riva gRPC.
- Requires optional dependency: `uv add eventyay-interpretation-portal[nvidia]`.
- Function ID configured via `settings.nvidia_function_id` (from `NVIDIA_FUNCTION_ID` env var).
- API key: `events.nvidia_api_key`.

---

## ElevenLabs (Transcription)

- scribe_v2 model.
- API key: `events.elevenlabs_api_key`.
- Implementation in `portal/transcription/providers/elevenlabs.py`.

---

## API Key Security Pattern

All external API keys follow this pattern:
1. Admin enters key via admin panel form.
2. Key encrypted via `portal.crypto.encrypt_val(key)` → stored in DB.
3. At transcription start: `get_api_key(event, ProviderEnum.X)` → `decrypt_val(encrypted_col)`.
4. `API_KEY_ENCRYPTION_KEY` must be set as env var (≥32 chars); fails loudly if default.
5. Key rotation: add new key as prefix to comma-separated `API_KEY_ENCRYPTION_KEY`; `MultiFernet` tries all keys.

---

## Future Eventyay Integration

Currently there is **no live API coupling** to Eventyay. The pattern mirrors Eventyay conventions only:
- `Event.slug` mirrors Eventyay event slug.
- `Room.eventyay_room_id` is a nullable foreign reference (not yet used).
- Role names match Eventyay role conventions.

When integrating: use `portal.*` imports; do not couple to `pretix.*`, `pretalx.*`, or `venueless.*`.
