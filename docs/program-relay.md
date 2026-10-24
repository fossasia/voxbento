# RTMPS Ingest with YouTube Relay (#749)

One encoder output, two results: the organizer's encoder sends a single **RTMPS**
stream to VoxBento; VoxBento relays it unchanged to YouTube and feeds its audio
to the room's floor captions, translation and TTS.

> **Relay mode puts VoxBento in the YouTube delivery path.** If VoxBento, MediaMTX
> or the relay gateway fails, the YouTube broadcast is interrupted. When failure
> isolation matters more than a single output, keep the default **independent
> outputs** mode ([Program Stream Ingest](program-stream-ingest.md)), where the
> encoder streams to YouTube itself and sends VoxBento a second WHIP output.

## Architecture

```
OBS / vMix / Wirecast / hardware
        │  RTMPS :1936  (H.264 + AAC, stream key "floor?key=<room secret>")
        ▼
MediaMTX ──auth hook──▶ portal /internal/media-auth   (room/key/mode check only)
   │  canonical path {event_slug}/{room_id}/floor
   ├─RTSP (private)──▶ FFmpeg ▶ STT ▶ captions ▶ translation ▶ TTS   (existing pipeline)
   └─RTSP (private)──▶ program-relay ── ffmpeg -c copy ──▶ rtmps://a.rtmps.youtube.com/live2/<key>
                         ▲
                         └── portal reconciler: desired relays over an authenticated control API
```

- **Media never traverses FastAPI.** MediaMTX terminates the contribution feed; the
  dedicated `program-relay` container (stdlib Python + FFmpeg) pulls the floor path
  over private RTSP and pushes it with `-c copy` (first video + first audio track, no
  re-encode). The portal owns configuration, authorization and status only.
- **One floor owner.** Relay mode is a `program_ingest` source. It replaces the Jitsi
  agent; no second OBS output is needed for captions/translation/TTS.
- **Independent health.** The room page reports three stages separately:
  *Contribution ingest* (authenticated feed present, codecs, stalls),
  *YouTube relay* (`idle`, `unconfigured`, `starting`, `live`, `retrying`, `stalled`,
  `capacity`, `degraded`) and *Language processing* (STT worker). Captions being off or
  an STT provider outage never stops the relay; a relay failure never stops captions.

## Input contract (v1)

| | Required |
|---|---|
| Transport | RTMPS (TLS). Plain RTMP is closed (`rtmpEncryption: strict`). |
| Video | H.264 (YouTube-compatible profile, keyframe every 2 s) |
| Audio | AAC-LC, 44.1 or 48 kHz, stereo or mono |
| Bitrate | What YouTube accepts for the chosen resolution; VoxBento does not transcode |

Feeds without H.264 + AAC are accepted for captions when they carry decodable audio,
but are **not relayed** — relay health shows *Needs attention* with the reason.
Codec conversion and SRT are follow-up work.

## Organizer setup

1. **Room AI settings:** enable floor transcription, pick the source language and
   provider, and configure translation/TTS as usual.
2. **Room page → Program stream ingest → Delivery mode:** choose
   *VoxBento relays to YouTube* and read the warning. Click **Enable relay ingest and
   create stream key**. This stops the Jitsi agent/any existing floor publisher.
3. Copy the **RTMPS server** (`rtmps://<host>:1936/<event>/<room>`) and the one-time
   **Stream key** (`floor?key=…`). It is shown once and expires in 30 days.
4. **YouTube destination:** in YouTube Studio create an **unlisted** event, copy its
   RTMPS server URL (`rtmps://a.rtmps.youtube.com/live2`) and stream key, paste both and
   **Save destination**. The key is write-only: it is never displayed again.
5. **OBS:** *Settings → Stream → Service: Custom…*, paste Server and Stream Key.
   *Output → Streaming:* x264 or hardware H.264, keyframe interval 2 s, AAC audio.
   vMix/Wirecast/hardware encoders: use their custom RTMP(S) destination with the same
   two values (some encoders need the full URL `<server>/<stream key>`).
6. Start streaming. *Contribution ingest* → Connected, *YouTube relay* → Live,
   *Language processing* → Processing. Confirm the YouTube preview before going public.

**Rotate:** *Rotate token* issues a new stream key and disconnects the encoder (update
OBS). Saving a new YouTube key restarts only the relay. **Revoke:** *Revoke token* kills
the encoder session and the relay and rejects the old key; *Remove destination* stops
the relay and deletes the stored YouTube key. **Switch:** choosing *Independent outputs*
and enabling again, or *Switch to Jitsi bot*, stops the relay and rebinds the credential.

## Security

- **Publisher authorization (per room).** MediaMTX calls `/internal/media-auth` for every
  publish. RTMP(S) is accepted only when the operator enabled relay mode, the room's
  mode is `rtmps_relay`, the path is that room's canonical floor path, and the query is
  exactly `key=<secret>` matching the room's PBKDF2 digest before expiry. Anonymous,
  wrong-room, expired, revoked, extra-parameter and wrong-transport attempts get 401.
  The key is bound to the mode's transport: a relay key cannot publish over WHIP and a
  WHIP token cannot publish over RTMPS. Failures share the 10/IP/min throttle.
- **TLS only.** The hook sees `protocol: "rtmp"` for both RTMP and RTMPS; the guarantee
  that it is TLS comes from MediaMTX `rtmpEncryption: strict` (no plaintext listener).
  Never set it to `optional`.
- **YouTube key at rest.** Fernet-encrypted with `API_KEY_ENCRYPTION_KEY`
  (`rooms.program_relay_key_enc`), write-only via the API, never logged, never in status
  JSON. It is decrypted only to hand the relay its destination, and only when that
  destination changes (a keyed fingerprint detects changes without resending the key).
- **Destination allow-list (SSRF).** Only `rtmps://` to `PROGRAM_RELAY_ALLOWED_HOSTS`
  (default `a.rtmps.youtube.com,b.rtmps.youtube.com`), without credentials or query, is
  accepted — checked by both the portal and the relay service.
- **Verified upstream TLS.** The relay always pushes with `-tls_verify 1` against the
  image's CA bundle (`RELAY_CA_FILE`, default Debian `ca-certificates`). FFmpeg builds
  before 8.0 do not verify by default, which would expose the YouTube key to an
  interceptor; there is no switch to disable verification in the service.
- **Redaction.** The relay never forwards FFmpeg error text: failures are classified
  (*Destination rejected the stream key*, *Destination unreachable*, …) and any retained
  line has the destination and key replaced with `[redacted]`. The control API never
  returns the destination.
- **Least-privilege network.** Public: TCP 1936 (RTMPS) only. `program-relay` publishes
  no ports; its control API requires `Authorization: Bearer $PROGRAM_RELAY_CONTROL_SECRET`
  and it runs read-only, as `nobody`, with all capabilities dropped. 8554/9997 stay on
  loopback; `/internal/*` stays blocked at Caddy.
- The YouTube key is visible in the relay container's process list (FFmpeg argv). Treat
  host shell access to that container as access to the keys.

## Deployment

1. Back up the database. Apply migration 028 (`uv run alembic upgrade head`).
2. Obtain a certificate for the public RTMPS hostname. Caddy cannot proxy RTMPS; MediaMTX
   terminates TLS itself. Copy (or bind) the hostname's certificate and key into a
   directory as `rtmps.crt` / `rtmps.key` (e.g. from Caddy's certificate storage) and
   renew alongside it.
3. Set in `.env`: `PROGRAM_INGEST_ENABLED=true`, `MEDIAMTX_AUTH_HOOK_SECRET`,
   `PROGRAM_RELAY_CONTROL_SECRET` (`openssl rand -hex 32`),
   `PROGRAM_RTMPS_PUBLIC_BASE=rtmps://voxbento.com:1936`, `PROGRAM_RTMPS_CERT_DIR`.
   Optional: `PROGRAM_RELAY_MAX`, `PROGRAM_RELAY_ALLOWED_HOSTS`.
4. Validate, then deploy with the override (the portal refuses production startup with
   relay enabled and no control secret):

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.relay.yml config --quiet
   docker compose -f docker-compose.yml -f docker-compose.relay.yml up -d --build program-relay
   docker compose -f docker-compose.yml -f docker-compose.relay.yml up -d portal mediamtx
   ```
5. Open TCP 1936 at the firewall. Nothing else new is public.
6. Keep **one portal replica** (ownership and reconciliation are process-local).

## Capacity

Each relay is one FFmpeg `-c copy` process: negligible CPU, but **egress equals the
contribution bitrate** per room (plus the same ingress). Size the uplink for
`rooms × encoder bitrate × 2`. `PROGRAM_RELAY_MAX` (default 4) is enforced by both the
portal and the relay service and **fails closed**: an extra room shows *At capacity* and
YouTube receives nothing for it; an already-live relay is never displaced. Relay slots
also count toward `PROGRAM_INGEST_MAX_ROOMS`, and STT workers share the existing cap.

## Recovery

The portal reconciler (every 2 s) converges the relay service on the desired set:
relay wanted ⇔ relay mode on, floor owned by program ingest, valid credential, live
H.264+AAC feed, destination configured. It restarts relays on destination change, stops
relays for rooms that no longer qualify, and stops orphans left by a portal restart.
The relay service retries a failed push with back-off (1 s → 30 s) while it is wanted.
If the relay service is unreachable, relay health shows *Needs attention*; ingest and
captions continue.

**Logs during an outage.** The reconciler keeps retrying every 2 s but logs the outage
once, not once per tick (#964): `program_relay list_unavailable` when it starts, a
`... ongoing failures=N duration_s=S` reminder every 5 minutes while it lasts, and
`... recovered failures=N duration_s=S` when the service answers again. A reminder that
never ends usually means `PROGRAM_RELAY_ENABLED=true` but the `program-relay` service from
`docker-compose.relay.yml` is not running.

**What "Live" means.** The relay reports *Live* only while the bytes FFmpeg writes to the
destination keep growing (`total_size` from FFmpeg's progress output), not merely while the
FFmpeg process runs. Each run starts from zero, so a reconnect must forward again before it
is reported live. Two guards keep a broken destination from looking healthy:

- **Stalled destination.** If output bytes stop growing for 15 s (the destination accepted
  the session but stopped reading, so writes block), a watchdog kills that FFmpeg run and
  the relay reconnects. Health shows *Stalled*, then *Retrying* with "Destination stopped
  accepting the stream".
- **Startup timeout.** A new run must connect to the source, wait for a keyframe and
  finish the TLS and RTMP handshakes before media flows, so the stall window does not
  apply until it has forwarded media. A run that forwards nothing within 30 s is killed
  and retried ("No media reached the destination").
- **I/O timeouts.** FFmpeg runs with a 10 s socket I/O timeout on both the RTSP source
  (`-timeout`) and the RTMPS destination (`-rw_timeout`), so a peer that stops answering
  fails the run instead of blocking forever.

A destination that closes or rejects an established session ends the FFmpeg run. The
relay classifies the cause from FFmpeg's error output (key rejected, unreachable, TLS
failure, …), shows *Retrying*, and is reported *Live* again only after the new run
forwards bytes.

**One process per room.** Replacing a destination or stopping a relay returns only after
the previous supervisor has reaped its FFmpeg run, including a run that was still being
spawned. No old process outlives a successful stop or replacement.

## Rollback

1. In each relay room choose *Independent outputs* (or *Switch to Jitsi bot*) and
   reconfigure the encoder to stream to YouTube directly.
2. Redeploy without `docker-compose.relay.yml` and recreate `mediamtx` so port 1936
   closes; set `PROGRAM_RELAY_ENABLED` unset/false. RTMP publishes are then refused even
   for rooms still in relay mode.
3. Migration 028 supports downgrade (back up first): it drops the mode and the stored
   destinations.

## Verification / rehearsal

CI gates (`.github/workflows/tests.yml`):

- **`unit-tests`** runs `tests/test_program_relay.py` (portal control plane: auth isolation,
  secret handling, lifecycle, capacity, health separation) and
  `tests/test_program_relay_service.py` (relay service: control API auth, validation,
  forwarding detection with a stand-in FFmpeg, stalled/zero-byte destinations, and
  subprocess races: stop during startup, destination change during startup, overlapping
  stop/start with exactly one owned process).
- **`program-relay-media (MediaMTX 1.18.2)`** runs `tests/test_program_relay_media.py`
  against the pinned MediaMTX binary and real FFmpeg. Every publish goes through the real
  MediaMTX → `/internal/media-auth` callback. It checks:
  - anonymous, wrong-room, wrong-key and expired keys are rejected;
  - revoke kills the active RTMPS connection, and the old key cannot reconnect;
  - H.264/AAC is relayed to a local RTMPS sink while the audio branch decodes;
  - a sink that drops an established session is reported, retried and recovered;
  - a sink that stops reading mid-session is not reported live, and the relay recovers.

  The job sets `PROGRAM_RELAY_MEDIA_REQUIRED=1`, so a missing binary fails the job
  instead of skipping the tests. Locally:
  `MEDIAMTX_TEST_BINARY=/path/to/mediamtx uv run pytest tests/test_program_relay_media.py -v`.
- **`program-relay-smoke`** builds the `program-relay` image and runs it read-only, as
  non-root, with all capabilities dropped and `no-new-privileges`, as in
  `docker-compose.relay.yml`. It runs `tests/program_relay_container_smoke.py` inside the
  container and checks:
  - FFmpeg is present with RTMPS/TLS;
  - the CA bundle verifies YouTube's real RTMPS certificate and rejects a self-signed peer;
  - a relay forwards H.264/AAC over verified RTMPS to a sink signed by a trusted CA;
  - an untrusted sink is refused and the refusal is classified;
  - stopping relays leaves no push process.

**Production gate:** the unlisted-YouTube rehearsal in
[program-relay-rehearsal.md](program-relay-rehearsal.md) must be recorded for the
deployment before relay mode is used for an event. Local sinks cannot validate YouTube's
acceptance of the stream, the key or event behaviour.
