# Program Relay & Contribution Rehearsal (Production Gate)

Relay mode (#749) and SRT contribution ingest (#750) are **not production-ready until this rehearsal has been recorded** for the deployment that will carry the event. Automated tests prove authentication, credential lifecycles, forwarding, and failover mechanics against local sinks; only a rehearsal against a real **unlisted** YouTube event and physical/software encoders can validate end-to-end delivery under production conditions.

Run this rehearsal on the production (or identical staging) deployment, using the exact encoders, networks, and profiles planned for event day. Record evidence in the PR or rollout ticket. Never paste raw stream keys, plain passphrases, or internal credentials into records.

---

## 1. Encoder Configuration Profiles

VoxBento accepts program contribution via **SRT Caller** (recommended for low-latency, resilient transport) and **RTMPS**. MediaMTX acts as an **SRT Listener** on port `8890/udp` (enabled via `docker-compose.srt.yml`).

### OBS Studio (Software Encoder)
* **Service:** Custom...
* **Server:** `srt://<host>:8890?streamid=#!::r=<event_slug>/<room_id>/floor,m=publish&latency=500000`
  * *For Backup Feed:* `srt://<host>:8890?streamid=#!::r=<event_slug>/<room_id>/floor_backup,m=publish&latency=500000`
* **Stream Key / Passphrase:** Leave stream key blank in OBS; enter the room passphrase in the passphrase field (or append `&passphrase=<passphrase>` if using URI-only configuration).
* **Latency:** `500ms` (`latency=500000` in microseconds).
* **Video Output:**
  * **Encoder:** x264 or hardware H.264 (NVENC, QuickSync, Apple VideoToolbox)
  * **Rate Control:** CBR `5000 kbps`
  * **Keyframe Interval:** `2 s` (fixed GOP, 60 frames at 30 fps or 120 frames at 60 fps)
  * **Profile:** High / Main
  * **B-frames:** 2 (or 0 for ultra-low latency)
* **Audio Output:**
  * **Codec:** AAC
  * **Sample Rate:** `48 kHz`
  * **Bitrate:** `128 - 192 kbps` stereo

### vMix (Software Production Switcher)
* **Output Destination:** SRT
* **Type:** Caller
* **Hostname / Address:** `<host>`
* **Port:** `8890`
* **Latency:** `500 ms`
* **Passphrase:** Enter the room SRT passphrase (AES-128 / AES-256 enabled).
* **Stream ID:** `#!::r=<event_slug>/<room_id>/floor,m=publish`
  * *Backup feed:* `#!::r=<event_slug>/<room_id>/floor_backup,m=publish`
* **Video Encoding:** H.264, CBR `5000 kbps`, 1080p, Keyframe interval `2 seconds`.
* **Audio Encoding:** AAC `48 kHz`, `160 kbps` stereo.

### Wirecast (Software Switcher)
* **Destination:** SRT
* **Mode:** Caller
* **Address:** `srt://<host>:8890`
* **Latency:** `500 ms`
* **Passphrase:** Enter room SRT passphrase.
* **Stream ID:** `#!::r=<event_slug>/<room_id>/floor,m=publish` (or `floor_backup`)
* **Encoding Profile:** H.264 Main/High, CBR `5000 kbps`, Keyframe every `2 seconds`.
* **Audio Profile:** AAC `48 kHz` stereo, `128 - 192 kbps`.

### Haivision Makito X4 / AJA HELO (Hardware Encoders)
* **Protocol / Encapsulation:** TS over SRT (MPEG-TS container carrying H.264 video and AAC-LC audio)
* **SRT Mode / Role:** Caller
* **Destination Address / Host:** `<host>`
* **Destination Port:** `8890`
* **SRT Packet Size:** `1316 bytes` (exactly 7 × 188-byte MPEG-TS packets per SRT frame)
* **SRT Latency:** `500 ms`
* **Encryption:** AES-128 or AES-256 with the room SRT passphrase
* **Stream ID:** `#!::r=<event_slug>/<room_id>/floor,m=publish` (or `floor_backup`)
* **Video Parameters:** H.264, CBR `5000 kbps`, GOP length `60 frames at 30 fps` (2 seconds), closed GOP.
* **Audio Parameters:** AAC-LC, `48 kHz`, `128 kbps` stereo.

---

## 2. Validation Record Table

All profiles must be tested against a staging or production deployment prior to event execution. Entries are initialized to **Pending Validation** until verified by an operator.

| Tool | Version Tested | Tested By | Date | Outcome | Notes |
|---|---|---|---|---|---|
| OBS Studio | Pending Validation | — | — | Pending Validation | SRT Caller, streamid publish, 5000kbps CBR, 2s GOP |
| vMix | Pending Validation | — | — | Pending Validation | SRT Caller, port 8890, AES passphrase, 500ms latency |
| Wirecast | Pending Validation | — | — | Pending Validation | SRT Caller, 500ms latency, AAC 48kHz |
| Haivision Makito X4 | Pending Validation | — | — | Pending Validation | TS over SRT, packet size 1316, 60 frames GOP at 30fps |
| AJA HELO | Pending Validation | — | — | Pending Validation | Hardware TS/SRT, 1316 byte payload, AES passphrase |

---

## 3. Operator Rehearsal Procedure

### Prerequisites
1. Server deployed with `docker-compose.srt.yml` (and `docker-compose.relay.yml` if YouTube relaying is active). Port `8890/udp` (SRT) and/or `1936/tcp` (RTMPS) open in firewalls.
2. A YouTube channel with live streaming enabled and a **new unlisted** live event created (Auto-start: OFF, Auto-stop: OFF, Low Latency).
3. A test room in VoxBento with floor transcription and at least one translation/TTS target enabled.
4. Primary encoder and (if testing redundant contribution) secondary backup encoder ready.

### Execution Steps

| # | Step | Action | Expected Result |
|---|------|--------|-----------------|
| 1 | Configure Destination | Room page → *From streaming software* → paste YouTube stream key → **Save stream key** | Saved for `rtmps://a.rtmps.youtube.com/live2`; key write-only |
| 2 | Generate Credentials | Select SRT Contribution → **Create credentials** | SRT URL `srt://<host>:8890`, Stream ID `#!::r=...`, and Passphrase displayed |
| 3 | Start Primary Feed | Start primary encoder using the profile in Section 1 | Room page shows *Encoder → VoxBento* **Connected** (H264, MPEG-4 Audio) |
| 4 | Verify Relay & Health | Wait 15–30 seconds; monitor room page and YouTube Studio | *VoxBento → YouTube* **Live**; YouTube Studio shows *Good/Excellent* health and video preview |
| 5 | Audio & AI Pipeline | Speak into floor feed for 5 minutes | Transcription, translation, and TTS work concurrently without relay degradation |
| 6 | Secondary Feed (Backup) | If backup enabled: start secondary encoder pointing to `floor_backup` | Room page shows backup feed connected and in standby |
| 7 | Takeover Test | Disconnect primary encoder (or simulate stall) | Within 5s (`TAKEOVER_STALL_SECS`), room switches `program_active_feed = "backup"`; relay continues seamlessly |
| 8 | Anti-Flapping Check | Reconnect primary encoder | Primary connects in standby; active feed stays on backup (no automatic flip-back) |
| 9 | Manual Restore | Operator triggers `promote_primary_feed` | Active feed cleanly restores to primary |
| 10 | Encoder Revocation | Click **Revoke credentials** | Encoder kicked immediately; subsequent reconnects rejected with auth failure |
| 11 | Audit Logs | Inspect container logs (`mediamtx`, `portal`, `program-relay`) | No stream keys, tokens, or passphrases exposed in plain text |

### Recording Rehearsal Outcomes
After completing the rehearsal steps:
1. Update the **Validation Record Table** above with tool version, operator name, execution date, and outcome (`PASS` / `FAIL`).
2. Fill out the **Evidence Record** template below and attach it to the rollout PR or operational issue ticket.

---

## 4. Evidence Record Template

Copy and fill this into the release ticket:

```text
Rehearsal Date (UTC):
Operator:
Deployment Host / Git SHA:
Encoder(s) & Firmware/Version:
Network / ISP:

[ ] SRT Ingest Connected (:8890/udp)
[ ] Codec Contract Verified (H.264 5000kbps, 2s keyframe, AAC 48kHz)
[ ] YouTube Relay Live within 15s (Health: Good/Excellent)
[ ] 10-Minute Sustained Stability (Zero dropped frames/restarts)
[ ] Simultaneous Captions/TTS Verification (Active & Synchronized)
[ ] Backup Feed Connected & Standby Verified
[ ] Automatic Takeover on Primary Drop (< 5s)
[ ] Anti-Flapping Verified (Primary reconnect did not flap active feed)
[ ] Operator Manual Switchback Verified
[ ] Credential Revocation Verified
[ ] Log Hygiene Verified (Zero decrypted secrets in logs)

Final Rehearsal Outcome: PASS / FAIL
Sign-off Operator:
```
