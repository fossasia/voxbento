# PR #1062: real-system floor recovery demonstration

Validated on 17 November 2026 against PR head `7c7d468376aba9b132ba09af63d59ff9cee3ea67`, targeting `dev`.

[Watch the continuous recording with captured French audio](recovery-real-system.mp4).

This addresses the outstanding real-system demonstration request in [the mentor's review](https://github.com/fossasia/voxbento/pull/1062#pullrequestreview-5937620092). No further application code change was requested in that review.

## What actually ran

An isolated local Docker Compose project ran the current portal checkout, Jitsi Meet `stable-11248` (web, Prosody, Jicofo and JVB), the unchanged floor-bot, MediaMTX `1.21.3`, FFmpeg, Whisper `base`, the registry-pinned NLLB translation model, and Supertonic French speech synthesis. A real Chromium listener used the application's existing caption WebSocket, binary TTS parser and Web Audio scheduler. A second browser joined the actual Jitsi conference as the speaker.

The speaker's microphone input was a prerecorded English speech file generated with Windows SAPI. Jitsi transported that audio to FloorBot; PulseAudio and FFmpeg captured and published it to MediaMTX. No transcription messages, translation responses, synthesized PCM, database operations, caption sockets or listener playback APIs were mocked. The listener's real sound output was recorded from a separate PulseAudio sink and muxed into the video. A separate Whisper pass transcribed that recording as French; see [playback-transcript.json](playback-transcript.json).

The seeded account, database, model cache, ports and containers belonged only to this rehearsal. The regular local portal and its data were not modified. Python dependencies were synchronized from the checked-out `uv.lock` using `uv sync --python 3.13 --dev --frozen --extra supertonic`.

## Observed result

The same listener tab stayed on the same room and French throughout. The owner controls disconnected and reconnected the meeting agent, which stopped the existing transcription worker and started a replacement. The speaker remained in the Jitsi conference. No listener reload, room change, language change or player reset was used during recovery.

| Point | Final caption rows | Caption / synthesized audio sequences |
|---|---:|---|
| Before replacement | 2 | 1, 2 |
| After replacement | 7 | 1, 2, 3, 4, 5, 6, 7 |

The first two rows retained exactly the same IDs and original/translated text after recovery. All seven distinct segments produced nonempty real French PCM bundles, with the same sequence and segment UUID as their source caption. Five input phrases produced seven finalized segments because the real transcriber split two of the phrases.

Approximate video positions (the recording starts about three seconds before the speech-source clock):

- **00:31:** first French speech plays.
- **00:47:** second French speech plays.
- **00:53–00:59:** owner disconnect/reconnect; the listener remains attached.
- **01:30 onward:** French speech resumes with sequence 3, then continues through 7.
- **02:28:** final history comparison and passing result.

The recording is continuous; the silence between phrases is intentional. A temporary status banner reports the harness phase. It does not change application state. Temporary WHEP 404s during publisher startup/replacement were followed by automatic stream recovery.

This real-media run demonstrates **worker replacement**. The delayed/stale-audio, provider-retry, room-isolation, program-ingest and database-failure cases remain covered by the automated tests; this recording does not claim to demonstrate all those cases live. There were no external provider calls or production traffic.

## Evidence and checks

- [Observed caption/TTS metadata and before/after history](evidence.json)
- [Before replacement](before-replacement.png)
- [After replacement](after-replacement.png)
- [Independent transcription of actual listener sound](playback-transcript.json)
- Focused Python checks: **105 passed** (`test_floor_caption_sequences.py`, `test_caption_aggregator.py`, `test_translation_pipeline.py`, `test_youtube_captions.py`, `test_youtube_caption_hooks.py`).
- Real listener module Node checks: **50 passed**, including the delayed and stale audio regression.

The existing application source was mounted read-only. These evidence files are included in the PR's implementation branch under docs/validation/pr1062. No credentials, authentication cookies or local databases are included.

Created with GPT-6, run from Windows.
