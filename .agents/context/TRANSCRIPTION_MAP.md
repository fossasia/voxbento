# VoxBento — Transcription Map

> Derived from `portal/transcription/`. All facts from live implementation.

---

## Architecture Overview

There are two primary audio capture pathways: **Booth Audio** (interpreters) and **Floor Audio** (the main conference).

### 1. Booth Audio Pipeline
```
Active interpreter Go Live
        │
        ▼
   MediaMTX (RTSP port 8554)
   rtsp://mediamtx:8554/{event_slug}/{language_code}
        │
        ▼
   ffmpeg (spawned by transcription worker)
   -rtsp_transport tcp → PCM s16le 16kHz mono → stdout pipe
```

### 2. Floor Audio Pipeline
```
Jitsi Meet Floor Conference
        │
        ▼
   floor-bot (Headless Chromium)
   Joins as "VoxBento FloorBot", captures audio via PulseAudio
        │
        ▼
   ffmpeg (spawned by floor-bot)
   Encodes to Opus → RTSP tcp push to MediaMTX:8554/{event_slug}/floor
```

### 3. Transcription & Translation Flow
```
   RTSP Stream (Floor or Booth)
        │
        ▼
   TranscriptionProvider.run_stream()
        │
        ▼
   CaptionAggregator (aggregator.py)
   partial / final / chunk events
        ├───────────────────────────────────────────────────────┐
        ▼                                                       ▼
   Database (TranscriptSegment)                            broadcast_transcription()
        │                                                  (WebSocket /ws/captions)
        ▼
   Translation Worker (translations/worker.py)
   Reads 'final' segments → _translate_and_broadcast()
        │
        ▼
   Database (TranscriptSegment target_language)
        │
   broadcast_translation() (WebSocket /ws/translations)
        │
        ▼
   TTS Worker (tts/worker.py)
   Reads 'final' translated segments → buffers punctuation → calls Deepgram Aura API
        │
        ▼
   broadcast_tts() (WebSocket /ws/tts/{room_id}) -> Listener Web Audio API
```

---

## Module Index

| File | Responsibility |
|---|---|
| `portal/transcription/__init__.py` | Re-exports public API; holds `shared_http_client` (AsyncClient) |
| `portal/transcription/constants.py` | `ProviderEnum`, `ALLOWED_MODELS` dict |
| `portal/transcription/worker.py` | `transcription_worker`, `start_transcription_worker`, `stop_transcription_worker`; `active_workers`/`active_processes` dicts |
| `portal/transcription/aggregator.py` | `CaptionAggregator`, `CaptionState` — partial/final merging, forced finalization |
| `portal/transcription/providers/base.py` | `TranscriptionProvider` ABC, `ProviderConfig`, `BoothTranscriptionState`, `pcm_to_wav`, `get_api_key` |
| `portal/transcription/providers/local.py` | `LocalProvider` — faster-whisper CPU; model cache + LRU eviction |
| `portal/transcription/providers/openai.py` | `OpenAIProvider` — whisper-1 (REST) + gpt-4o-realtime (WebSocket) |
| `portal/transcription/providers/deepgram.py` | `DeepgramProvider` — nova-2 WebSocket streaming |
| `portal/transcription/providers/nvidia.py` | `NVIDIAProvider` — Parakeet-RNNT/CTC via Riva gRPC |
| `portal/transcription/providers/elevenlabs.py` | `ElevenLabsProvider` — scribe_v2 |
| `portal/translations/worker.py` | `start_translation_worker()`, LLM translation loop, multithreaded translation dispatch |
| `portal/translations/constants.py` | Translation models enum mapped to Anthropic, OpenAI, Groq, Gemini, etc. |
| `portal/tts/worker.py` | `TTSWorker`, buffers punctuation, chunks text, calls Deepgram Aura WebSocket API, pushes raw PCM binary to `tts_manager` |
| `portal/tts/constants.py` | `TTS_VOICE_MAP` (language code to Deepgram voice ID mapping) |

---

## Providers

| `ProviderEnum` value | Class | Models | Auth | Protocol |
|---|---|---|---|---|
| `local` | `LocalProvider` | `tiny`, `base`, `small`, `medium`, `large-v2`, `large-v3` | None | CPU inference via `faster-whisper` (in thread pool) |
| `openai` | `OpenAIProvider` | `whisper-1`, `gpt-4o-realtime-preview`, `gpt-4o-mini-realtime-preview` | `openai_api_key` | REST (whisper-1) or WebSocket realtime |
| `deepgram` | `DeepgramProvider` | `nova-2` | `deepgram_api_key` | WebSocket streaming |
| `nvidia` | `NVIDIAProvider` | `parakeet-rnnt`, `parakeet-ctc` | `nvidia_api_key` | Riva gRPC |
| `elevenlabs` | `ElevenLabsProvider` | `scribe_v2` | `elevenlabs_api_key` | — |

API keys are stored encrypted on the `events` table (`encrypted_*_api_key` columns).
`get_api_key(event, ProviderEnum.X)` decrypts via `portal.crypto.decrypt_val`.

External provider use requires `event.transcription_api_enabled = True`.

---

## Worker Lifecycle

### `start_transcription_worker(event_slug, language_code, booth_id, broadcast_callback, provider, model_size, config)`

1. Acquires `active_workers_lock`.
2. Returns immediately if worker already running for this `booth_id`.
3. Returns immediately if `len(active_workers) >= MAX_TOTAL_WORKERS` (10).
4. Creates asyncio Task running `transcription_worker(...)`.
5. Stores in `active_workers[booth_id]` = `{task, provider, stderr_task}`.

### `transcription_worker(...)` (internal)

1. Builds RTSP URL: `rtsp://mediamtx:8554/{event_slug}/{language_code}`
2. Spawns ffmpeg: PCM s16le 16kHz mono via subprocess pipe.
3. Starts `stderr_task` to drain ffmpeg stderr (debug log only).
4. Calls `provider.run_stream(process, ...)` — blocks until stream ends.
5. `finally`: kills ffmpeg, removes from `active_workers`/`active_processes`.

### `stop_transcription_worker(booth_id)`

Cancels the asyncio Task and awaits it; removes from state dicts.

---

## Audio Chunking (default `run_stream` in `base.py`)

- Chunk size: `16000 * 2 * 3 = 96000 bytes` = 3 seconds of PCM.
- Reader queues chunks into `asyncio.Queue(maxsize=2)`.
- Queue full → oldest chunk dropped; `booth_state.chunks_dropped_total` incremented.
- `consecutive_drops >= 3` → sends `[Server overloaded…]` error.
- Processor task calls `provider.process_chunk(chunk, …)` → text → `CaptionAggregator.handle_chunk(booth_id, text)`.

**`LocalProvider`** extends this with a 1-second overlap buffer (appends last 32000 bytes of previous chunk) and runs inference in `asyncio.to_thread`.

---

## CaptionAggregator Logic

| Method | Input | Behavior |
|---|---|---|
| `handle_partial(booth_id, text)` | streaming partial | Broadcasts `{type:"caption", status:"partial", text}`. Force-finalizes if ≥50 words or ≥15 seconds in same utterance |
| `handle_final(booth_id, text)` | completed utterance | Broadcasts `{type:"caption", status:"final", text}`; resets state |
| `handle_chunk(booth_id, text)` | Whisper text chunk | Appends to buffer; splits on sentence boundary (`[.?!]+`); broadcasts `final` for each sentence, `partial` for remainder |
| `handle_clear(booth_id)` | silence/endpoint | Broadcasts `{type:"caption", status:"clear"}` |

---

## Local Model Cache (`providers/local.py`)

- `_loaded_models: dict[str, ModelEntry]` — shared across all booths (process-global).
- `_active_booths_per_model` — ref count per model size.
- Eviction loop: runs every 15 minutes; evicts model if `refs == 0` and `last_used > 1 hour ago`.
- `WhisperModel(model_size, device="cpu", compute_type="int8")` — uses int8 quantisation.

---

## Admin Integration

Transcription is configured per-booth via:
- `POST /admin/events/{event_id}/rooms/{room_id}/booths/{booth_id}/transcription-settings`

If the booth is currently live (`state.active_interpreter_id is not None`), the worker is stopped and restarted immediately with the new config.

---

## Caption WebSocket

Clients subscribe to `/ws/captions/{booth_id}` (no auth).
Receives: `booth:state`, `caption {status: partial|final|clear, text}`.
Used by: listener pages, subtitling overlays.
