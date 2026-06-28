    

# Supertonic TTS Integration — Self-Hosted Provider for VoxBento (v3)

## Background

VoxBento currently has a single TTS provider: **Deepgram Aura** (cloud-based, requires API key). The pipeline:

```
Final transcript → LLM translation → TTSWorker → Deepgram Aura WebSocket → PCM audio → /ws/tts → listener
```

**Supertonic** is a 99M-parameter ONNX TTS engine supporting 31 languages. Goal: add it as a **second provider option** alongside Deepgram — self-hosted, no API key, no cloud.

---

## Key Insight from susi_translator PR #88

After reviewing the actual Supertonic integration in [susi_translator/flask/transcribe_server.py](file:///Users/arnavangarkar/Desktop/susi_translator/flask/transcribe_server.py), here's how they did it:

```python
# In-process — no sidecar, no HTTP server
from supertonic import TTS

_supertonic_tts = TTS(auto_download=True)
voice_style = _supertonic_tts.get_voice_style(voice_name="M1")

with tts_inference_lock:
    wav, duration = _supertonic_tts.synthesize(
        text=text, lang="en", voice_style=voice_style,
        total_steps=8, speed=1.0
    )

# Convert numpy → WAV bytes → base64 → piggyback on SSE
buf = io.BytesIO()
sf.write(buf, wav.squeeze(), 44100, format='WAV', subtype='PCM_16')
audio_b64 = base64.b64encode(buf.getvalue()).decode('utf-8')

# Sent inside the SSE JSON payload alongside transcript/translation
payload["audio_b64"] = audio_b64
```

**Critical takeaways:**

1. **No sidecar needed** — Supertonic runs **in-process** as a Python import. No separate Docker container, no HTTP overhead.
2. **Thread-safe with a lock** — ONNX Runtime sessions aren't thread-safe. susi_translator uses `tts_inference_lock` (mutex). Supertonic's own serve code uses `synth_lock` the same way.
3. **TTS.synthesize() is synchronous** — Must run in a thread pool to avoid blocking the async event loop.
4. **Model auto-downloads on first use** — `TTS(auto_download=True)` fetches ~400MB from HuggingFace on first init, then caches.

### What VoxBento does differently (better)

| Aspect         | susi_translator                            | VoxBento (planned)                                        |
| -------------- | ------------------------------------------ | --------------------------------------------------------- |
| Audio delivery | Base64 WAV piggybacked on SSE JSON         | Raw PCM binary over dedicated`/ws/tts` WebSocket        |
| Audio format   | Full WAV file per chunk (~100KB base64)    | Streamed PCM s16le chunks (~few KB each)                  |
| Bandwidth      | ~2x overhead (base64 encoding)             | Raw binary, no encoding overhead                          |
| Playback       | HTML5`Audio()` element, sequential queue | `AudioContext` + `BufferSource`, gapless scheduling   |
| Latency        | Waits for full WAV, then queues            | Streams PCM chunks, schedules ahead with`nextStartTime` |

VoxBento's existing WebSocket + AudioContext approach is already superior. We keep it.

---

## User Review Required

> [!IMPORTANT]
> **In-process vs. sidecar**: The plan now uses **in-process** Supertonic (like susi_translator), adding `supertonic` as a Python dependency. This is simpler and faster than the sidecar approach. The Docker sidecar (`supertonic serve`) becomes **optional** — only useful if we want the Voice Builder `/v1/styles/import` API endpoint. For MVP, in-process is the way to go.

> [!WARNING]
> **New Python dependency**: Adding `supertonic` to `pyproject.toml` pulls in `onnxruntime`, `numpy` (already a dep), and `soundfile`. ONNX Runtime is ~50MB. The model weights (~400MB) download on first `TTS()` init and cache under `~/.cache/supertonic-3/`. The portal container needs internet access on first startup (or we pre-bake models in the Dockerfile).

> [!IMPORTANT]
> **Thread pool sizing**: ONNX inference is CPU-bound (~50-200ms per sentence). We run it in `asyncio.to_thread()` with a dedicated `ThreadPoolExecutor(max_workers=1)` + inference lock (same pattern as susi_translator and Supertonic's own server). This prevents concurrent ONNX calls while keeping the event loop free.

---

## Open Questions

> [!IMPORTANT]
> **Voice selection**: Configurable default voice per room (admin dropdown of M1–M5, F1–F5), with `M1` as the initial default. susi_translator maps voices per-language (en→M1, fr→F1, etc.) — should we do the same as a fallback?
> sure

---

## Proposed Changes

### 1. TTS Provider Abstraction Layer

#### [NEW] [\_\_init\_\_.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/tts/providers/__init__.py)

New subpackage `portal/tts/providers/`.

#### [NEW] [base.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/tts/providers/base.py)

- ABC `TTSProvider` with method:
  - `async def synthesize_sentence(self, text: str, language_code: str, voice: str) -> bytes` — returns raw PCM 24kHz s16le bytes
- `TTSProviderEnum` enum: `deepgram`, `supertonic`
- Factory function `get_tts_provider(provider: str) -> TTSProvider`

#### [NEW] [deepgram.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/tts/providers/deepgram.py)

- Extract existing Deepgram WebSocket logic from `TTSWorker._stream_llm_to_tts()` into `DeepgramTTSProvider`
- Wraps the existing Deepgram Aura WebSocket streaming flow

#### [NEW] [supertonic.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/tts/providers/supertonic.py)

**In-process** implementation (no HTTP, no sidecar):

```python
from supertonic import TTS
import threading
import asyncio
import numpy as np

class SupertonicTTSProvider(TTSProvider):
    _tts: TTS | None = None
    _lock = threading.Lock()           # Init lock (double-checked locking)
    _inference_lock = threading.Lock() # ONNX session is not thread-safe

    @classmethod
    def _get_engine(cls) -> TTS:
        if cls._tts is None:
            with cls._lock:
                if cls._tts is None:
                    cls._tts = TTS(auto_download=True)
        return cls._tts

    async def synthesize_sentence(self, text: str, lang: str, voice: str) -> bytes:
        """Run Supertonic in a thread to avoid blocking the event loop."""
        return await asyncio.to_thread(self._synthesize_sync, text, lang, voice)

    def _synthesize_sync(self, text: str, lang: str, voice: str) -> bytes:
        engine = self._get_engine()
        style = engine.get_voice_style(voice_name=voice)
        lang_tag = lang if lang in SUPERTONIC_SUPPORTED_LANGS else "na"

        with self._inference_lock:
            wav, _ = engine.synthesize(
                text=text, lang=lang_tag, voice_style=style,
                total_steps=8, speed=1.0,
            )

        # wav is numpy float32 at 44100 Hz — convert to PCM s16le at 24000 Hz
        return self._resample_to_pcm(wav.squeeze(), 44100, 24000)

    @staticmethod
    def _resample_to_pcm(samples: np.ndarray, src_rate: int, dst_rate: int) -> bytes:
        """Resample float32 array and convert to 16-bit PCM bytes."""
        # Linear interpolation resample
        ratio = dst_rate / src_rate
        new_len = int(len(samples) * ratio)
        indices = np.linspace(0, len(samples) - 1, new_len)
        resampled = np.interp(indices, np.arange(len(samples)), samples)
        # Float32 [-1, 1] → Int16
        pcm = (resampled * 32767).clip(-32768, 32767).astype(np.int16)
        return pcm.tobytes()
```

Key points:

- **Single TTS instance** shared across all workers (class-level `_tts`)
- **Inference lock** prevents concurrent ONNX calls (same as susi_translator)
- **`asyncio.to_thread()`** offloads CPU-bound synthesis without blocking the event loop
- **Direct numpy resampling** 44.1kHz → 24kHz, no WAV encoding/decoding step, no `soundfile` needed for this path
- Output is raw PCM s16le bytes — goes straight to `broadcast_audio_callback` → `/ws/tts` → listener `AudioContext`

---

### 2. Database Schema Changes

#### [MODIFY] [models.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/models.py)

Add to `Room` model:

```python
# TTS Provider Settings
floor_tts_provider: Mapped[str] = mapped_column(
    String(20), default="deepgram", server_default=sa.text("'deepgram'")
)
floor_tts_voice: Mapped[str] = mapped_column(
    String(50), default="M1", server_default=sa.text("'M1'")
)
```

> [!NOTE]
> `floor_tts_voice` is per-room from day one. Per-room voice selection works immediately — each room stores its own voice. The initial UI scope is per-event selection (same dropdown), but the schema already supports per-room without migration.

#### [NEW] [009_add_tts_provider_fields.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/alembic/versions/009_add_tts_provider_fields.py)

- Alembic migration adding `floor_tts_provider` and `floor_tts_voice` columns to `rooms` table

---

### 3. Dependencies

#### [MODIFY] [pyproject.toml](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/pyproject.toml)

Add `supertonic` as an optional dependency (same pattern as `nvidia`):

```toml
[project.optional-dependencies]
nvidia = ["nvidia-riva-client>=2.21.1"]
supertonic = ["supertonic>=1.3.0"]
```

And in the main dependencies, **no change** — `numpy` is already there. The `supertonic` extra pulls in `onnxruntime` and `soundfile` automatically.

In the Dockerfile, install with `uv sync --extra supertonic` when Supertonic TTS is desired.

---

### 4. Configuration

#### [MODIFY] [config.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/config.py)

Add settings:

```python
# Supertonic TTS — optional sidecar URL for Voice Builder import API
# Leave empty to use in-process TTS (no sidecar needed)
supertonic_base_url: str = ""
```

---

### 5. TTS Worker Refactor

#### [MODIFY] [worker.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/tts/worker.py)

Major refactor:

- Read `room.floor_tts_provider` and `room.floor_tts_voice` from DB
- Route to the correct provider:
  - `"deepgram"` → `DeepgramTTSProvider` (existing WebSocket flow, extracted)
  - `"supertonic"` → `SupertonicTTSProvider` (in-process ONNX)
- The LLM streaming + sentence buffering logic stays in `TTSWorker`
- For **Supertonic**: buffer sentences from LLM stream, call `synthesize_sentence()` per sentence, broadcast immediately
- For **Deepgram**: existing WebSocket flow (unchanged, just extracted)

#### [MODIFY] [constants.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/tts/constants.py)

Add:

```python
SUPERTONIC_PRESET_VOICES = ["M1", "M2", "M3", "M4", "M5", "F1", "F2", "F3", "F4", "F5"]
SUPERTONIC_DEFAULT_VOICE = "M1"

# Language-to-voice mapping (same pattern as susi_translator)
SUPERTONIC_VOICE_BY_LANG: dict[str, str] = {
    "en": "M1", "de": "M2", "fr": "F1", "es": "F2",
    "hi": "M3", "ar": "M4", "pt": "F3", "ru": "F4",
    "ja": "F5", "ko": "M5", "it": "M1",
}

SUPERTONIC_SUPPORTED_LANGS = {
    "ar", "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr",
    "de", "el", "hi", "hu", "id", "it", "ja", "ko", "lv", "lt",
    "pl", "pt", "ro", "ru", "sk", "sl", "es", "sv", "tr", "uk", "vi",
}
```

---

### 6. Docker Changes

#### [MODIFY] [docker-compose.yml](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/docker-compose.yml)

**No new sidecar service needed for MVP!** Just:

- Add `supertonic-cache` volume mount to portal service: `- supertonic-cache:/root/.cache`
- This persists the model weights across container restarts

```yaml
portal:
  volumes:
    - .:/app
    - /app/.venv
    - portal-data:/data
    - supertonic-cache:/root/.cache   # Supertonic model cache
```

Add to volumes:

```yaml
supertonic-cache:
```

#### [MODIFY] [Dockerfile](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/Dockerfile)

Add `--extra supertonic` to the `uv sync` command to install the optional dependency.

> [!NOTE]
> First `TTS(auto_download=True)` call downloads ~400MB model from HuggingFace. The `supertonic-cache` volume persists this. Alternatively, add a `RUN` step in the Dockerfile to pre-download the model during build.

---

### 7. Admin UI

#### [MODIFY] Admin room edit template

- **TTS Provider** dropdown: `Deepgram Aura (Cloud)` | `Supertonic (Self-Hosted)`
- **Voice** dropdown (shown when Supertonic is selected): `M1`–`M5`, `F1`–`F5`
- Deepgram continues using language-based auto-mapping (existing `DEEPGRAM_VOICE_MAPPING`)

#### [MODIFY] [admin.py](file:///Users/arnavangarkar/Desktop/Eventyay_Interpretation/eventyay/voxbento/portal/routers/admin.py)

Update `admin_edit_room` handler to read and persist `floor_tts_provider` and `floor_tts_voice`.

---

### 8. Voice Builder JSON Upload (Per-Event, Phase 2)

Per-event upload. Admins download JSON from [Voice Builder](https://supertonic.supertone.ai/voice-builder).

**Approach**: Store uploaded JSON on the `portal-data` volume at `/data/voice_profiles/{event_id}/{voice_name}.json`. Load custom voices via `TTS.get_voice_style_from_path(path)` at synthesis time. No sidecar needed.

**Per-room migration path**: `floor_tts_voice` is already per-room. Just change the admin UI to scope voice selection per-room instead of per-event.

> [!NOTE]
> This is scaffolded in this PR — admin UI shows upload form, stores file, loads custom voice. Full implementation in a follow-up.

---

## Architecture After Changes

```
Final transcript segment
        │
        ▼
   LLM Translation streams sentence-by-sentence
        │
        ▼
   TTSWorker.handle_tts(room_id, text)
        │
        ├─ room.floor_tts_provider == "deepgram"
        │       │
        │       ▼
        │  DeepgramTTSProvider (extracted from existing code)
        │  Opens WS → sends Speak per sentence → receives PCM
        │       │
        │       ▼
        │  broadcast_audio_callback → /ws/tts (raw binary)
        │
        └─ room.floor_tts_provider == "supertonic"
                │
                ▼
           SupertonicTTSProvider (in-process)
           For each sentence:
             asyncio.to_thread(TTS.synthesize()) → numpy float32
                │
                ▼
             resample 44.1kHz → 24kHz, float32 → int16 PCM
                │
                ▼
           broadcast_audio_callback → /ws/tts (raw binary)
```

**Listener receives exactly the same format regardless of provider** — raw PCM s16le 24kHz over WebSocket binary frames. No client changes needed.

---

## Comparison: susi_translator vs VoxBento approach

|                    | susi_translator (PR#88)              | VoxBento (this plan)                          |
| ------------------ | ------------------------------------ | --------------------------------------------- |
| TTS engine         | In-process`TTS()` ✓               | In-process`TTS()` ✓                        |
| Thread safety      | `tts_inference_lock`               | `_inference_lock` + `asyncio.to_thread()` |
| Audio transport    | Base64 WAV in SSE JSON               | Raw PCM binary over WebSocket                 |
| Encoding overhead  | ~33% base64 bloat                    | Zero — raw binary                            |
| Playback           | HTML5`Audio()` element             | `AudioContext` + `BufferSource` (gapless) |
| Sentence streaming | TTS fires per-chunk, SSE polls 200ms | TTS fires per-sentence from LLM stream        |
| Model init         | On first call, auto_download         | Same —`TTS(auto_download=True)`            |
| Voice selection    | Per-language map                     | Per-room + per-language fallback              |

---

## File Summary

| Action           | File                                                | Description                                            |
| ---------------- | --------------------------------------------------- | ------------------------------------------------------ |
| **NEW**    | `portal/tts/providers/__init__.py`                | Provider package init                                  |
| **NEW**    | `portal/tts/providers/base.py`                    | ABC + enum + factory                                   |
| **NEW**    | `portal/tts/providers/deepgram.py`                | Extracted Deepgram provider                            |
| **NEW**    | `portal/tts/providers/supertonic.py`              | In-process ONNX provider + resample                    |
| **MODIFY** | `portal/tts/worker.py`                            | Route to provider based on room config                 |
| **MODIFY** | `portal/tts/constants.py`                         | Add Supertonic voices, lang map, supported langs       |
| **MODIFY** | `portal/models.py`                                | Add`floor_tts_provider`, `floor_tts_voice` to Room |
| **NEW**    | `alembic/versions/009_add_tts_provider_fields.py` | Migration                                              |
| **MODIFY** | `pyproject.toml`                                  | Add`supertonic` optional dependency                  |
| **MODIFY** | `portal/config.py`                                | Add`supertonic_base_url` (optional)                  |
| **MODIFY** | `docker-compose.yml`                              | Add cache volume mount                                 |
| **MODIFY** | `Dockerfile`                                      | Add`--extra supertonic`                              |
| **MODIFY** | `portal/routers/admin.py`                         | Handle new form fields                                 |
| **MODIFY** | Room edit template                                  | TTS provider dropdown + voice picker                   |

---

## Verification Plan

### Automated Tests

```bash
uv sync --python 3.13 --dev --extra supertonic
uv run pytest tests/ -v
node --check static/js/interpreter-booth.js
node --check static/js/whep-listener.js
uv run alembic upgrade head
```

### Manual Verification

1. **Model loads**: First startup logs `Loaded unicode indexer...` and model download progress
2. **Admin UI**: Room edit page shows TTS provider dropdown; selecting "Supertonic" reveals voice picker
3. **End-to-end live TTS**: Enable floor transcription + translation + TTS (Supertonic) → listener page receives progressive audio via `/ws/tts` WebSocket
4. **Deepgram regression**: Switch provider back to Deepgram → still works identically
5. **Voice selection**: Changing voice (M1→F3) produces noticeably different audio timbre
6. **Latency check**: First audio chunk arrives within ~500ms of transcript finalization
7. **No listener changes**: Verify `/ws/tts` delivers same PCM format (24kHz s16le) for both providers
