# Skill: Floor Bot Analysis

This skill explains how to debug and analyze the Floor Bot feature in VoxBento, which bridges the Jitsi WebRTC audio floor track to the RTSP/MediaMTX infrastructure for transcription.

## Overview of Floor Bot

The floor-bot is a headless Playwright/Chromium container that joins the main Jitsi conference room (monitoring the floor channel). It captures the audio and sends it over a PulseAudio socket directly to FFmpeg. FFmpeg then pushes this audio via RTSP to MediaMTX, where the transcription worker pulls it and generates captions.

### Key Components

- **floor-bot container**: Runs `bot.py` via an HTTP server. It listens on port 8080 for `/start` and `/stop` commands.
- **PulseAudio Socket**: `/tmp/pulse-{event_slug}.sock` acts as the bridge. Chromium outputs to this socket, and FFmpeg reads from it.
- **MediaMTX RTSP Path**: Audio is pushed to `rtsp://mediamtx:8554/{event_slug}/floor`.
- **FastAPI Backend**: Admin routes `/api/rooms/{room_id}/floor-transcription/start` trigger the bot to join the Jitsi room and immediately spawn the transcription worker pointing to the floor channel.

## Local Development vs. Production

When deploying this architecture, understanding the network topology is critical.

### 1. Jitsi Internal URL (`JITSI_INTERNAL_BASE`)

- **Local**: In Docker Compose, the Jitsi service is named `jitsi-web`. To bypass WebRTC HTTP origin blocking for getUserMedia, the backend automatically replaces `localhost` or `jitsi.voxbento.com` with the Docker-internal HTTPS URL: `https://jitsi-web`. The bot explicitly uses `--ignore-certificate-errors` and `--unsafely-treat-insecure-origin-as-secure=https://jitsi-web`.
- **Production**: If Jitsi is deployed *outside* of the docker-compose network (e.g., a standalone public Jitsi cluster), the internal `https://jitsi-web` hostname will fail. You **must** set `JITSI_INTERNAL_BASE` to the public Jitsi URL (e.g., `https://jitsi.eventyay.com`) in your `.env`.

### 2. MediaMTX RTSP Base (`MEDIAMTX_RTSP_BASE`)

- **Local**: Defaults to `rtsp://mediamtx:8554` (the container name inside Docker Compose).
- **Production**: If MediaMTX is hosted elsewhere, override this via the `MEDIAMTX_RTSP_BASE` environment variable. Both the floor-bot FFmpeg ingest and the backend transcription worker use this URL.

### 3. Floor Bot API (`FLOOR_BOT_BASE`)

- **Local**: Defaults to `http://floor-bot:8080`.
- **Production**: If deployed on a different orchestration layer (like Kubernetes), override `FLOOR_BOT_BASE` to point to the correct pod/service DNS.

## Common Debugging Steps

### 1. Bot is Silent / No Audio
Check if Playwright is muting the browser. Ensure `--ignore_default_args=["--mute-audio"]` is being passed. Also verify if the Jitsi UI is trying to acquire mic permissions and failing: Ensure `--use-fake-device-for-media-stream` is used.

### 2. Name is "Fellow Jitster"
Jitsi expects URL parameters (like `userInfo.displayName`) to be valid JSON strings. Ensure the display name is properly quote-wrapped and URL encoded:
`urllib.parse.quote('"VoxBento FloorBot"')` -> `%22VoxBento%20FloorBot%22`.

### 3. Connection Refused to Pulse Socket
Ensure `PULSE_SERVER="unix:/tmp/pulse-{event_slug}.sock"` is properly passed to the Playwright chromium browser using the `env=` dictionary.

### 4. FFmpeg Demuxing/DTS Loops
If FFmpeg reports loop/sync errors (`Non-monotonous DTS`), check the sample rate constraints in Jitsi vs Chromium vs FFmpeg.

## Debugging Commands

**Tail the Floor Bot Logs:**
`docker compose logs -f floor-bot`

**Check PulseAudio Connections inside Floor Bot:**
`docker compose exec floor-bot sh -c 'PULSE_SERVER="unix:/tmp/pulse-{event_slug}.sock" pactl list sink-inputs'`
*(If the browser is outputting audio correctly, you will see 'Chromium' listed as a client).*

**Check FFmpeg/MediaMTX Streaming:**
`docker compose logs -f mediamtx`
*(Look for: `[path {event_slug}/floor] stream is online`)*
