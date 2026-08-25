# ISSUE: Low-Latency Playback Choppiness due to Discrete Sequential Translation & Synthesis Pipeline

## 1. Summary & Problem Description
The current AI-driven translation and text-to-speech (TTS) pipeline in VoxBento operates as a **discrete, batch-based sequential workflow**. Because of this, listeners experience a highly disjointed, "bursty" audio playback where they hear a translated sentence, followed by a substantial period of silence before the next block is fully processed and played back.

This "broken parts" behavior is a known flaw of sequential translation architectures, and it erodes the live simultaneous interpretation experience which should ideally flow continuously like a real human voice.

---

## 2. Technical Root Cause & Code Analysis
Based on the VoxBento codebase, live audio flows through the following path:

1. **Sentence Boundary Waiting (`portal/transcription/aggregator.py`):**
   The `CaptionAggregator` receives rolling "partial" text chunks from the STT provider. It holds onto these partials and only marks the sentence as `"final"` once a punctuation boundary (like a period, exclamation point, or question mark) is detected, or if a hard limit of 50 words / 15 seconds is reached.
2. **Translation Trigger (`portal/translations/worker.py`):**
   The `TranslationWorker` queries the database for newly finalized transcript segments. Once a segment is marked `"final"`, it dispatches concurrent API requests to the chosen LLM provider (Groq, Anthropic, Gemini, OpenAI) to perform the translation. This requires waiting for the LLM's full text output.
3. **Punctuation-Based TTS Buffering (`portal/tts/worker.py`):**
   The `TTSWorker` retrieves the finalized translation. Although some cloud providers return audio quickly, the worker buffers the text until it hits punctuation (commas, periods) to ensure proper prosody and vocal inflection before submitting it to the TTS provider (Deepgram Aura or self-hosted Supertonic ONNX).
4. **Instant Client-Side Playback (`static/js/whep-listener.js`):**
   The generated PCM audio is sent to attendees as binary chunks over the `/ws/tts/{room_id}` WebSocket. The listener client receives these chunks and plays them instantly using the Web Audio API (`AudioContext`). Since there is no coordination or queueing, if a block is finished playing before the next block arrives, playback stops abruptly, resulting in choppy audio.

### The Bottleneck Diagram:
```
[Interpreter Audio] ──> [STT Worker] ──> [CaptionAggregator] (Wait for sentence end)
                                               │
                                               ▼ (Sentence is "final")
[WHEP Listener] <── (Audio Plays) <── [whep-listener.js] <── [TTSWorker] <── [TranslationWorker]
                                        (Plays instantly,     (Synthesizes fully)   (LLM batch translation)
                                         creates gaps)
```

---

## 3. Steps to Reproduce & Experience
1. Set up a room in the Admin Dashboard with automated translation and TTS enabled (using either Deepgram/Groq/Aura or fully local `faster-whisper` + local translation + Supertonic ONNX).
2. Open the Interpreter Booth page (`/interpreter/{event_slug}/{language_code}`) and click **"Go Live"** to start broadcasting.
3. Open the Listener page (`/listener/{event_slug}`) in another browser window to hear the translated AI feed.
4. Speak a series of continuous sentences into the interpreter's microphone.
5. **Observed Behavior on Listener Page:** 
   * The listener hears the first sentence spoken in the translated language.
   * Playback abruptly stops.
   * There is a 2-4 second gap of absolute silence.
   * The second translated sentence plays all at once.
   * Repeat.

---

## 4. Proposed Solution: A Streaming & Queue-Scheduled Architecture
To solve this, we must transition the entire AI pipeline from **batch/discrete boundaries** to a **continuous streaming flow**. This can be achieved through three coordinated enhancements:

### A. Word-by-Word Streaming LLM Translation
Instead of waiting for a sentence to finalize, the translation engine should consume the live STT stream as it is written.
* Integrate sliding-window or rolling-prompt translation. 
* Trigger LLM calls with `stream=True` over WebSockets or SSE so that translated tokens are output incrementally in real-time as the interpreter speaks, rather than waiting for a full period.

### B. Streaming Text-to-Speech (TTS) Ingest
Instead of the `TTSWorker` waiting for full sentence translations:
* Connect to a streaming TTS WebSocket (such as Deepgram Aura's streaming endpoint).
* As translation tokens stream back from the LLM, pipe them immediately into the TTS WebSocket word-by-word. This reduces TTS turnaround latency from several seconds to under 200ms.

### C. Client-Side Playback Scheduling (The Jitter Buffer Queue)
On the attendee client (`static/js/whep-listener.js`), implement a scheduled audio playback queue using the browser’s Web Audio API. Instead of playing incoming binary chunks instantly, schedule each buffer to play exactly when the previous buffer is scheduled to end.

#### Implementation Draft for `whep-listener.js`:
```javascript
// Keep track of the exact end-time of the scheduled queue
let nextStartTime = 0;
const JITTER_BUFFER_SEC = 0.25; // 250ms buffer to absorb network & API variance

function playAudioChunk(arrayBuffer) {
    audioCtx.decodeAudioData(arrayBuffer, (audioBuffer) => {
        const source = audioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(audioCtx.destination);

        const now = audioCtx.currentTime;

        // Determine the scheduled start time
        let startTime;
        if (nextStartTime < now) {
            // Queue is empty or stalled; start playing after a small safety jitter buffer
            startTime = now + JITTER_BUFFER_SEC;
        } else {
            // Schedule immediately after the last queued audio block ends
            startTime = nextStartTime;
        }

        source.start(startTime);

        // Update the pointer for the next chunk
        nextStartTime = startTime + audioBuffer.duration;
    }, (err) => {
        console.error("Error decoding audio chunk", err);
    });
}
```

---

## 5. Acceptance Criteria
- [ ] **Continuous Playback:** Translated audio plays back in a steady, natural cadence without jarring gaps of dead silence between sentences.
- [ ] **Lower End-to-End Latency:** The delay between the interpreter speaking a phrase and the attendee hearing the synthesized translation is reduced.
- [ ] **No Overlap/Jitter:** Under fluctuating network conditions, the scheduled playback queue gracefully schedules audio without overlapping packets or dropped frames.
- [ ] **Fallback Room Tone:** (Optional) If the queue does empty due to the speaker pausing, the player fades in a subtle "room tone" comfort noise rather than hitting absolute zero-amplitude digital silence.
