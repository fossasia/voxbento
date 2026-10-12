# Program Stream Ingest (#690)

Keep streaming directly to YouTube; add VoxBento as a second destination.
VoxBento consumes the organizer's copy, not a YouTube watch/embed URL. It never
needs a YouTube stream key. This is not an OBS replacement or a YouTube relay.

## Transport and rollout

V1 supports **HTTPS WHIP with a bearer token**, Opus audio, and optional video.
Choose H.264 for broad encoder/browser compatibility. MediaMTX terminates WebRTC;
Caddy terminates HTTPS signalling. Media never flows through FastAPI. RTMP/SRT
encoder outputs are not supported in this version; use a WHIP-capable output or
external distribution service, or retain the Jitsi floor bot.

The integration is pinned to MediaMTX **1.18.2**. The opt-in native transport test
uses **FFmpeg 8.1.2** (with the WHIP muxer), which is the tested reference encoder
workflow. OBS Studio 30+ exposes a native WHIP destination; vMix and hardware
encoders vary by build, so do the acceptance rehearsal below with the exact encoder
and second-output mechanism before an event. A single OBS output does not by itself
produce two simultaneous destinations.

Operator rollout (not an automatic deployment):

1. Back up the database. Apply migration 025 (`uv run alembic upgrade head`).
2. Deploy the portal, MediaMTX config and Caddy config together. The authentication
   callback is `/internal/media-auth` on the Docker network only. Caddy must reject
   `/internal/*`. Bind host ports 8000, 8888, 8889, 9997 and 8554 to loopback as in
   Compose. Set `MEDIAMTX_AUTH_HOOK_SECRET` to the same strong random value for the
   portal and MediaMTX (`openssl rand -hex 32`); Compose wires the value to both.
   The portal refuses production startup without it. Never forward 9997/8554 at
   the firewall. No public RTMP/SRT listener.
3. Set `PUBLIC_BASE_URL` to the actual HTTPS origin and `MEDIAMTX_WHIP_BASE` to
   the same public origin. Set `PROGRAM_INGEST_ENABLED=true` after validating
   this boundary. Default is off. Set the reachable public IP/domain in MediaMTX's
   `webrtcAdditionalHosts`, and allow UDP 8189. HTTPS uses TCP 443. Use trusted
   certificates/DNS; never send bearer tokens over public HTTP.
4. Validate with `docker compose config --quiet` and Caddy's configuration validator.
   Reload services through your normal deployment process. Changing the Compose
   file alone does not close already-published Docker ports until recreation.
5. Use **one portal worker/replica**. Ownership and rate limits are process-local,
   consistent with the existing BoothRegistry. Horizontal scaling needs shared
   coordination; it is not supported by this feature.

The HTTP authentication policy changes only floor publishing: interpreter paths
retain their existing publishing behavior. This is **not a general authorization
redesign for interpreter WHIP**. Every public floor publish is room/token checked;
bot RTSP publishing is allowed only from private addresses while `jitsi_bot` owns
the floor. Read access remains the existing public listener model. Do not expose
the callback behind another proxy without equivalent blocking and private ports.

## Organizer setup

Open the event owner's room page (`/workspace/events/.../rooms/.../`).

1. Enable floor transcription, source language, and the provider/model. Configure
   the event API key/enablement when using a cloud STT provider. Configure desired
   translation languages and TTS provider/voice using the existing AI settings.
2. Enable Program Stream Ingest. This stops the bot and existing floor stream.
3. Copy the endpoint. Reveal/copy the one-time bearer token into the encoder's
   WHIP authorization field. In OBS choose the WHIP service. Do not put the token
   in a URL, screenshot, issue, browser storage, or log.
4. Keep the separate YouTube output running. Use a compatible multi-output
   capability/plugin or external distribution service if your encoder only has
   one stream output. VoxBento does not configure that third-party output for you.
5. Send Opus audio (silence is valid audio); video alone cannot be transcribed.
   The program feed reaches `{event_slug}/{room_id}/floor`. No bot launch is needed.

Tokens have 256 bits of randomness, a PBKDF2-HMAC-SHA256 storage digest with a
120,000-round work factor, and a 30-day expiry.
The digest is bound to `JWT_SECRET` (or its `SECRET_KEY` fallback). Rotating that
secret invalidates existing program-ingest tokens, so rotate each room's ingest
token after changing the server secret and before the next event.
Status/HTML never returns the token or digest. Rotation/revocation disconnects the
current session, including sessions still negotiating, and invalidates the old
token. Revocation is committed before media cleanup: if MediaMTX cannot confirm
the disconnect, the API returns `202` with `cleanup_pending: true`, the old token
remains invalid, and reconciliation retries cleanup. Revoke keeps program
ownership (it does not silently restart the bot).
“Use Jitsi floor bot” explicitly returns ownership; start the bot separately.
All source changes affect VoxBento only, not the independent YouTube output.

## Processing, recovery, and capacity

`WHIP → MediaMTX floor → FFmpeg (first audio track, PCM) → existing STT →
CaptionAggregator → stored final transcript → lazy translation → text then TTS`.

PCM is signed 16-bit mono, normally 16 kHz; the existing OpenAI worker uses
24 kHz. Existing interpreter paths and their publisher handoff are unchanged.
Only final text enters translation/TTS. Text broadcasts immediately after
translation, without waiting for synthesis. Failures remain isolated by language.

The reconciler polls every two seconds and reconstructs ownership from the DB
and live media state after restart. Pending publisher reservations count against
the default **10-program** limit; the existing **10-STT-worker** cap is shared
with interpreters. Tune `PROGRAM_INGEST_MAX_ROOMS`, CPU, bandwidth, provider quotas,
and available inference models together. A rejected second publisher never kicks
the first. Reconnects may wait for the **15-second disconnect grace** plus one poll
before their old reservation is released. Configure with
`PROGRAM_INGEST_DISCONNECT_GRACE_SECS`.

States: disabled, waiting, receiving (audio decoder starting), processing (PCM
progress observed), degraded (no supported audio, stalled decoder, missing STT
configuration, capacity/control failure), disconnected. Decoder/no-progress
detection defaults to 30 seconds (`PROGRAM_INGEST_STALL_SECS`). Silence with
continuing audio packets is not a stall. Worker restarts never restart YouTube.
The UI includes codec names, last connect/disconnect and credential expiry.
Logs include action/room ID or reconciliation failure only, never request bodies,
credentials, or upstream errors. Authentication failures are bounded to 10/IP/min
with a bounded process-local cache. Do not enable HTTP request-body/debug logging
on the media-auth route. The access-log filter redacts the hook key. Monitor
degraded/disconnected rooms and worker capacity.

## Synchronization

Program captions/TTS carry canonical segment IDs, a DB-backed room sequence,
STT-receive wall-clock timestamp, server send time, and an additional room delay
(0–120 seconds). This timestamp is a **receive-time estimate, not encoder PTS or
timecode**; current providers do not expose a common source timestamp. Translation
tasks preserve that metadata into both text and audio stages. The browser applies
remaining delay relative to server time so client clock skew does not add delay.
STT inference and saving are not delayed. Source sequence persists through worker
restarts. Late speech cannot be made earlier; failed/missing clips are skipped
after a bounded wait. Buffered updates are discarded on source/language changes.

Before going live, send a visible clap plus spoken count to both destinations.
Compare YouTube video with VoxBento captions/speech, increase the **program
caption/TTS delay**, reload the listener, and repeat. The pre-existing room audio
delay controls original/interpreter audio independently. YouTube latency can drift
and differs by viewer/network; this is manual calibration, not frame-accurate sync.

## Verification / event rehearsal

- Unit/integration: `uv run pytest tests/test_program_ingest.py tests/test_caption_aggregator.py tests/test_translation_pipeline.py`.
- Native real-media transport: set `MEDIAMTX_TEST_BINARY` to a 1.18.2 binary and run
  `uv run pytest tests/test_program_ingest_media.py -v`. Requires FFmpeg with WHIP.
  It starts isolated local services, publishes generated audio, decodes PCM, and
  tests invalid/revoked credentials. It does not access a real event or cloud STT.
- Start two rooms with different tokens: attempt wrong-room, anonymous, duplicate,
  expired and revoked publishes; verify rejection and no credential leakage.
- Rehearse the exact encoder video+Opus profile, video-only degradation, encoder
  reconnect, portal restart, decoder/provider outage, translation-only and TTS
  listeners in at least two languages. Confirm other rooms and interpreters remain live.
- Calibrate with the sync cue. Confirm text precedes speech, captions persist in
  transcript export, and changing room/language cannot play an old buffered clip.
- With an **unlisted test YouTube broadcast**, deliberately stop VoxBento while
  keeping the independent encoder output running. Confirm YouTube remains live.
  This last external/encoder rehearsal is required before production sign-off;
  an automated local test cannot establish it.

Rollback: disable/revoke room ingest and explicitly select/start the Jitsi bot,
then set the feature flag false. Keep the floor authentication boundary and
private ports; do not restore anonymous public floor publishing. Migration 025
supports downgrade, but back up first: downgrade removes program configuration
and sequence data.
