# RTMPS Relay — Unlisted YouTube Rehearsal (production gate)

Relay mode (#749) is **not production-ready until this rehearsal has been recorded** for the
deployment that will carry the event. Automated tests (`program-relay-media`,
`program-relay-smoke`) prove authentication, revocation, forwarding, TLS verification and
failure handling against a local RTMPS sink; they cannot prove that YouTube accepts the
relayed stream, its stream key and event behaviour. Only a rehearsal against a real
**unlisted** YouTube event can.

Run it on the production (or an identical staging) deployment, with the encoder and network
that will be used on the day. Record the evidence below in the PR or rollout ticket. Never
paste stream keys, VoxBento credentials or the full YouTube ingest URL into the record.

## Prerequisites

- `scripts/setup-youtube-relay.sh` run (relay checks passed); TCP 1936 reachable with a trusted
  certificate for the RTMPS hostname; migration 028 applied.
- A YouTube channel with live streaming enabled and a **new unlisted** live event
  (YouTube Studio → Create → Go live → Stream, visibility *Unlisted*, low latency).
- One room with floor transcription configured (provider, source language) and at least one
  translation/TTS language, so captions are observable.

## Steps

| # | Step | Expected |
|---|------|----------|
| 1 | Room page → *VoxBento forwards to YouTube* → step 1: paste the YouTube stream key → **Save stream key** | "Saved … for rtmps://a.rtmps.youtube.com/live2"; key not shown again |
| 2 | Step 2 → **Create stream key** | RTMPS server and one-time stream key shown |
| 3 | OBS: Custom, Server + Stream Key from step 2, x264/NVENC H.264, keyframe 2 s, AAC 128 kb/s, start streaming | *Encoder → VoxBento* **Connected** (H264, MPEG-4 Audio) |
| 4 | Wait 30 s | *VoxBento → YouTube* **Live**, badge *Live on YouTube*; YouTube Studio shows *Excellent/Good* stream health and the preview plays |
| 5 | Stream for **10 minutes** with speech | No relay restarts; YouTube health stays good; captions/translation/TTS work for listeners |
| 6 | Check the unlisted watch page from a second device | Picture and audio match the encoder output |
| 7 | **Replace stream key** with a new YouTube key (create a second unlisted event or reset the key) | Old event stops receiving video; relay restarts once and is Live on the new event. End the old event in YouTube Studio (with Auto-stop off it does not end by itself) |
| 8 | `docker compose restart portal` | Relay keeps running (or resumes within one reconcile); no duplicate relay |
| 9 | `docker compose restart program-relay` | YouTube shows a short interruption; relay health reports it and returns to Live |
| 10 | Stop OBS for 30 s, then start again | Ingest Disconnected → Connected; relay resumes; YouTube resumes |
| 11 | **Revoke stream key** | OBS disconnects immediately; relay stops; OBS reconnect with the old key fails |
| 12 | Step 1 → **Remove** | Stored key cleared; *VoxBento → YouTube* *Not configured* |
| 13 | Review logs (`portal`, `mediamtx`, `program-relay`) | No YouTube key, no `floor?key=`, no full ingest URL anywhere |
| 14 | End the YouTube event | — |

## Evidence record

Copy this into the PR / rollout ticket and fill it in.

```text
Rehearsal date (UTC):
Operator:
Deployment / commit:            (e.g. voxbento.com @ <sha>)
Encoder + version + settings:   (e.g. OBS 31.0, x264 veryfast 4500 kb/s 1080p30, AAC 128 kb/s)
YouTube event:                  unlisted, low latency (do not paste the key or ingest URL)

Step 4  relay Live within (s):
Step 5  10 min stable (yes/no), relay restarts, YouTube health:
Step 6  playback verified on second device (yes/no):
Step 7  key rotation recovered (yes/no, seconds):
Step 8  portal restart, relay unaffected (yes/no):
Step 9  relay restart, interruption length (s), recovered (yes/no):
Step 10 encoder reconnect recovered (yes/no):
Step 11 revoke kicked encoder + stopped relay; old key refused (yes/no):
Step 12 destination removed (yes/no):
Step 13 logs free of keys/URLs (yes/no):
Screenshots: room page (three health stages), YouTube Studio stream health
Outcome: PASS / FAIL (+ follow-ups)
```

A failed step blocks production use of relay mode for that deployment; the independent WHIP
mode remains available. Repeat the rehearsal after any change to the relay service, MediaMTX
version, certificate setup or encoder profile.
