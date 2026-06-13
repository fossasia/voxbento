# Skill: Transcription Analysis

> Use this skill for tasks involving transcription providers, caption streaming, or the audio pipeline.
> Reference: `portal/transcription/`, [TRANSCRIPTION_MAP.md](../../context/TRANSCRIPTION_MAP.md).

---

## Audio Pipeline (end-to-end)

```
Interpreter browser
  └── getUserMedia (mic, 16-bit PCM via WebRTC/WHIP)
        └── MediaMTX (RTSP port 8554, path: {event_slug}/{language_code})
              └── ffmpeg (spawned by worker.py)
                    -rtsp_transport tcp -i rtsp://mediamtx:8554/{path}
                    -f s16le -acodec pcm_s16le -ar 16000 -ac 1 -
                        └── TranscriptionProvider.run_stream()
                              └── 3-second PCM chunks
                                    └── process_chunk() → text
                                          └── CaptionAggregator
                                                └── broadcast_transcription()
                                                      ├── /ws/booth/{booth_id}  (interpreters)
                                                      └── /ws/captions/{booth_id}  (listeners)
```

---

## Worker State

- `active_workers: dict[str, dict]` — keyed by `booth_id`; value has `{task, provider, stderr_task}`
- `active_processes: dict[str, asyncio.subprocess.Process]` — the ffmpeg process per booth
- Both protected by `active_workers_lock: asyncio.Lock`
- `MAX_TOTAL_WORKERS = 10` — process-global hard limit

---

## Adding a New Provider (checklist)

1. Create `portal/transcription/providers/{name}.py`
2. Implement class inheriting `TranscriptionProvider` from `providers/base.py`
3. Implement `process_chunk(chunk, language_code, model_variant, config, booth_state) -> str`
4. For streaming (WebSocket-based): override `run_stream(process, language_code, model_variant, config, broadcast_callback, booth_id)` — see `deepgram.py` and `openai.py` as references
5. Add `ProviderEnum.{NAME} = "{name}"` in `constants.py`
6. Add `{name}: {allowed_models_set}` to `ALLOWED_MODELS`
7. Add to `PROVIDERS` dict in `worker.py`
8. If API key needed: add encrypted column to `Event` model (migration 008 pattern) + update `get_api_key` key_map in `base.py`
9. Update `admin_event_api_settings_post` in `fastapi_app.py` to handle new key
10. Update `templates/admin/api_settings.html`

---

## CaptionAggregator Usage

Providers call aggregator methods, not `broadcast_callback` directly:

```python
aggregator = CaptionAggregator(broadcast_callback)
await aggregator.handle_partial(booth_id, partial_text)   # streaming partial
await aggregator.handle_final(booth_id, final_text)       # completed utterance
await aggregator.handle_chunk(booth_id, whisper_chunk)    # Whisper-style chunks
await aggregator.handle_clear(booth_id)                   # silence endpoint
```

**Forced finalization:** if an utterance exceeds 50 words or 15 seconds, it auto-finalizes regardless of provider signal. This prevents endlessly growing partials.

---

## Local Provider Details

- **Model loading:** `get_model(model_size)` — thread-safe lazy loading. Model is loaded once and reused.
- **Overlap buffer:** 1 second (32 000 bytes) of previous chunk is prepended to current chunk → 4-second effective window per inference call.
- **Thread pool:** `asyncio.to_thread(self._run_inference, ...)` — each chunk is inferred in a thread.
- **VAD filter:** `vad_filter=True` in faster-whisper — reduces empty segment noise.
- **Model eviction:** `eviction_loop` checks every 15 min; evicts model if no active booths and last used > 1 hour ago.

---

## OpenAI Provider Details

- `whisper-1`: uses REST `POST /v1/audio/transcriptions` (WAV conversion via `pcm_to_wav`)
- `gpt-4o-realtime-*`: uses `wss://api.openai.com/v1/realtime` WebSocket; streams raw PCM
- Retry: `tenacity` with exponential backoff (2–10s, 3 attempts) on 429/5xx
- HTTP client: `portal.transcription.shared_http_client` — created in `lifespan` in `fastapi_app.py`

---

## Deepgram Provider Details

- Connects to `wss://api.deepgram.com/v1/listen?model=nova-2&...&interim_results=true`
- Runs concurrent `sender` (pipes PCM) and `receiver` (gets transcripts) tasks
- Handles `KeepAlive` (every 5s timeout to prevent WS close)
- `speech_final=True` or `is_final=True` → `handle_final`; otherwise → `handle_partial`

---

## NVIDIA Provider Details

- Uses Riva gRPC (requires `nvidia-riva-client` optional dependency: `uv add eventyay-interpretation-portal[nvidia]`)
- Models: `parakeet-rnnt`, `parakeet-ctc`
- Requires `NVIDIA_FUNCTION_ID` setting in `portal/config.py`

---

## Transcription Trigger (Admin → Worker)

1. Admin sets `transcription_enabled=True`, `provider`, `model` via booth detail form.
2. `POST /admin/.../booths/{id}/transcription-settings` handler:
   - Validates provider + model combination.
   - Verifies API key exists if non-local provider.
   - Saves settings to DB.
   - If booth is currently live (has `active_interpreter_id`): stops old worker, starts new worker.
3. On interpreter Go Live: worker is started if `transcription_enabled=True` on the DBBooth.
   - Look for this logic in `fastapi_app.py` (search for `start_transcription_worker`).

---

## Debugging Transcription

1. Check `active_workers` dict for the booth_id.
2. Check ffmpeg process is alive: `active_processes[booth_id].returncode is None`.
3. Check MediaMTX RTSP is reachable: `rtsp://mediamtx:8554/{event_slug}/{language_code}`.
4. Check API key: `get_api_key(event, ProviderEnum.X)` — returns `None` if not set.
5. Check `MAX_TOTAL_WORKERS` not exceeded.
6. Check `event.transcription_api_enabled` is `True` for external providers.
