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
  *Encoder → VoxBento* (authenticated feed present, codecs, stalls),
  *VoxBento → YouTube* (`idle`, `unconfigured`, `starting`, `live`, `retrying`, `stalled`,
  `capacity`, `degraded`) and *Captions and translation* (STT worker). Captions being off or
  an STT provider outage never stops the relay; a relay failure never stops captions.

## Input contract (v1)

| | Required |
|---|---|
| Transport | RTMPS (TLS). Plain RTMP is closed (`rtmpEncryption: strict`). |
| Video | H.264 (YouTube-compatible profile, keyframe every 2 s) |
| Audio | AAC-LC, 44.1 or 48 kHz, stereo or mono |
| Bitrate | What YouTube accepts for the chosen resolution; VoxBento does not transcode |

Feeds without H.264 + AAC are accepted for captions when they carry decodable audio,
but are **not relayed**; relay health shows *Needs attention* with the reason.
Codec conversion and SRT are follow-up work.

## Organizer setup (step by step)

Before you start, the server operator must have turned relay mode on once
([Server setup](#server-setup-operator)). If the room page says *Not switched on for
this server yet* under **VoxBento forwards to YouTube**, ask them to do that first.

You need: YouTube Studio access for the channel (live streaming enabled; new channels
wait up to 24 h after enabling), OBS (or vMix, Wirecast, a hardware encoder with RTMPS)
and the room page in VoxBento.

**0. Captions settings (optional, can be done later).** Room page → AI settings: turn on
floor transcription, pick the spoken language and a provider. YouTube forwarding works
without captions; the *Captions and translation* row just stays off.

**1. Get the YouTube stream key.**
1. YouTube Studio → **Create → Go live → Stream**. For the first run create an
   **Unlisted** stream.
2. In *Stream settings*, keep *Default stream key* (or pick a reusable key) and copy the
   **Stream key**. You do not need the Stream URL.
3. Recommended: turn **Auto-start off**, so you can check the preview before going public
   (with it on, YouTube goes live as soon as video arrives), and **Auto-stop off**, so a
   short reconnect does not end the broadcast.

**2. Room page → Room audio → From streaming software.**
1. Under *How does your stream reach YouTube?* choose **VoxBento forwards to YouTube**.
2. Step **1 — Add your YouTube stream key:** paste the key and click **Save stream key**.
   You can also paste the full URL (`rtmp://a.rtmp.youtube.com/live2/<key>`); VoxBento
   splits it, and sends to YouTube over RTMPS either way. The key is encrypted and never
   shown again. To change it, paste a new one and click **Replace stream key**.
3. Step **2 — Connect OBS:** click **Create stream key** and confirm. This makes
   VoxBento the room's floor source (it stops the Jitsi bot). Copy the **Server**
   (`rtmps://<host>:1936/<event>/<room>`) and the **Stream key** (`floor?key=…`). The key is
   **shown only once** and lasts 30 days. If you lose it, click **Create a new stream key**.

**3. OBS.**
1. *Settings → Stream:* Service **Custom…**, paste **Server** and **Stream Key**. Leave
   *Use authentication* off.
2. *Settings → Output → Streaming:* video encoder **x264** or a hardware **H.264**
   encoder (NVENC H.264, QuickSync H.264, Apple VT H.264), **not HEVC or AV1**. Rate
   control CBR at YouTube's recommended bitrate (for example 4500–6000 kb/s for 1080p30),
   **keyframe interval 2 s**.
3. *Audio:* AAC (the default), 128–160 kb/s, sample rate 48 kHz (*Settings → Audio*).
4. vMix, Wirecast or hardware encoders with one URL field: use **Copy full URL** below
   the stream key (`<server>/floor?key=…`).

**4. Go live and check.** Click **Start Streaming** in OBS. On the room page, step 3
shows three rows:

| Row | Expected | Meaning |
|---|---|---|
| Encoder → VoxBento | **Connected** | OBS reached VoxBento with a valid key |
| VoxBento → YouTube | **Live** (within ~10 s) | YouTube is receiving the stream |
| Captions and translation | **Processing** | floor audio is being transcribed |

The badge at the top reads **Live on YouTube**. Check the preview in YouTube Studio
(stream health *Good/Excellent*), then click **Go live** in Studio. Use
**Sync with the YouTube picture** to align caption/speech delay.

**After the event:** stop streaming in OBS, then click **End stream** in YouTube Studio
(with Auto-stop off, YouTube does not end it for you). The stored keys stay for the next session.

**Change or stop things.**
- *New YouTube stream / key:* paste it in step 1 → **Replace stream key**. Only the
  forwarding restarts; OBS stays connected.
- *Lost the VoxBento key:* **New stream key** (bottom of the card); paste it into OBS.
- *Stop forwarding to YouTube:* step 1 → **Remove**. OBS stays connected; captions continue.
- *Lock out the encoder:* **Revoke stream key** disconnects OBS immediately; the old key
  stops working.
- *Leave relay mode:* choose **OBS streams to YouTube itself** and click **Switch…**, or
  **Switch to the online meeting**. Both stop forwarding and replace the credential.

## Troubleshooting

| What you see | Cause | Fix |
|---|---|---|
| OBS: *Failed to connect to server* | Port 1936 closed or wrong server | Copy the **Server** again; the operator opens TCP 1936 |
| OBS connects then drops immediately | Wrong or expired stream key, or the key belongs to another room | Click **New stream key** and paste it into OBS |
| *Encoder → VoxBento: Needs attention — No supported audio track* | OBS audio is not AAC | Use AAC audio (the OBS default) |
| *VoxBento → YouTube: Needs attention — needs H.264 video and AAC audio* | HEVC/AV1 encoder selected | Choose x264 or an H.264 hardware encoder |
| *VoxBento → YouTube: Not configured* | No YouTube key saved | Step 1: paste the key, **Save stream key** |
| *Retrying — Destination rejected the stream key* | YouTube key wrong, reset, or the stream was deleted | Copy the current key from YouTube Studio → **Replace stream key** |
| *Live* here, but YouTube Studio shows *No data* | Studio is showing a different stream/key | Open the stream whose key you saved |
| YouTube preview plays but never goes public | Auto-start is off (recommended) | Click **Go live** in YouTube Studio |
| *The YouTube relay service on the server is not responding* | `program-relay` container not running | Operator: `docker compose ps program-relay`, `docker compose logs program-relay` |
| *Not switched on for this server yet* | Relay mode not deployed | Operator: run `scripts/setup-youtube-relay.sh` |
| *At capacity* | `PROGRAM_RELAY_MAX` rooms already forwarding | Stop another room's forwarding, or the operator raises the limit |

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
  stored, and both the portal and the relay service check it. With no server URL the
  portal uses `rtmps://a.rtmps.youtube.com/live2`. YouTube Studio's plain
  `rtmp://a.rtmp.youtube.com/live2` (or `b.`) is rewritten to the RTMPS host for the same
  ingest; no other `rtmp://` URL is accepted, and nothing is ever pushed over plain RTMP.
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

## Server setup (operator)

Prerequisites: the standard production deployment (host Caddy with a certificate for the
public hostname, `.env` with `MEDIAMTX_AUTH_HOOK_SECRET` and the other production
secrets), migrations applied (028 adds relay mode), and a database backup.

Run once on the server, from the checkout:

```bash
scripts/setup-youtube-relay.sh            # --host, --cert-dir/--cert-name, --yes are optional
```

It is safe to re-run. It:

1. derives the hostname from `PUBLIC_BASE_URL` and sets `PROGRAM_RTMPS_PUBLIC_BASE`
   (`rtmps://<host>:1936`);
2. generates `PROGRAM_RELAY_CONTROL_SECRET` (and `MEDIAMTX_AUTH_HOOK_SECRET` if missing) and
   sets `PROGRAM_INGEST_ENABLED=true`, never printing a secret;
3. finds the certificate Caddy already manages for the host
   (`/var/lib/caddy/.local/share/caddy/certificates/*/<host>/`) and sets
   `PROGRAM_RTMPS_CERT_DIR` / `PROGRAM_RTMPS_CERT_NAME`. MediaMTX terminates RTMPS itself
   (Caddy cannot proxy it) and reloads the files when Caddy renews them, so there is no
   copy step and no renewal job;
4. adds `COMPOSE_FILE=docker-compose.yml:docker-compose.relay.yml` to `.env`, so every
   `docker compose` command **and the production deploy workflow** include relay mode;
5. validates the configuration, then (after confirmation) builds and starts
   `program-relay`, recreates `mediamtx` and `portal`, and checks that the portal reaches the
   relay and that port 1936 presents a valid certificate.

Then open **TCP 1936** in the firewall / cloud security group. Nothing else becomes public.
For EC2, select the instance's security group → **Inbound rules → Edit inbound rules**
and add **Custom TCP**, port **1936**, source the encoder's public IP (or
`0.0.0.0/0` for organizers connecting from varying networks). Save the rule.

### Websites behind Cloudflare

Cloudflare's standard web proxy does not carry RTMPS on port 1936. Use a separate
**DNS-only** A record pointing to the server. The production Caddyfile includes
`rtmps.voxbento.com` so Caddy obtains and renews its certificate; Caddy does not proxy
the RTMPS stream. Deploy/reload that Caddyfile and wait for certificate issuance first.

For the voxbento.com deployment, set this value in `.env` (the setup script preserves
an existing public base, even when `--host` is supplied):

```dotenv
PROGRAM_RTMPS_PUBLIC_BASE=rtmps://rtmps.voxbento.com:1936
```

Then select the matching managed certificate explicitly, replacing an old certificate
directory setting if necessary:

```bash
scripts/setup-youtube-relay.sh --host rtmps.voxbento.com \
  --cert-dir /var/lib/caddy/.local/share/caddy/certificates/acme-v02.api.letsencrypt.org-directory/rtmps.voxbento.com \
  --cert-name rtmps.voxbento.com
```

Use the actual certificate directory if Caddy uses another issuer. Other deployments
should substitute their own DNS-only hostname in both Caddy and the relay settings.
Check port 1936 **from another machine**: the setup script's loopback TLS check cannot
detect a blocked cloud security group or a proxied DNS record. For example:

```bash
openssl s_client -connect rtmps.voxbento.com:1936 -servername rtmps.voxbento.com \
  -verify_hostname rtmps.voxbento.com -verify_return_error </dev/null
```

Keep **one portal replica** (ownership and reconciliation are process-local). Optional
`.env` settings: `PROGRAM_RELAY_MAX` (default 4), `PROGRAM_RELAY_ALLOWED_HOSTS`.

**Deploys.** `deploy.yml` copies `.env`, so `COMPOSE_FILE` keeps the relay in every
deploy. Without it, `docker compose up --remove-orphans` would delete `program-relay`
and close port 1936. The deploy therefore **refuses to continue** when `program-relay`
is running but the new configuration would not include it.

Manual equivalent: set the variables above in `.env`, then

```bash
docker compose config --quiet
docker compose up -d --build program-relay && docker compose up -d mediamtx portal
```

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

1. In each relay room choose *OBS streams to YouTube itself* (or *Switch to the online meeting*)
   and reconfigure the encoder to stream to YouTube directly.
2. Remove `COMPOSE_FILE` from `.env` (or drop `docker-compose.relay.yml` from it) and run
   `docker compose up -d --remove-orphans`: `program-relay` is removed and port 1936
   closes. RTMP publishes are then refused even for rooms still in relay mode.
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
