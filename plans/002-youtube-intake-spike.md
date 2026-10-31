# Plan 002: Verify the four YouTube-intake assumptions before building

> **Executor instructions**: Follow this plan step by step. This is a
> measurement spike: you change **no source files**. You write exactly one
> results file. Run every verification and record the observed result. If a
> STOP condition occurs, stop and report. When done, update the status row for
> this plan in `plans/README.md`.
>
> **Drift check (run first)**: `git diff --stat 250aefde..HEAD -- mediamtx.yml docker-compose.yml docker-compose.relay.yml portal/program_ingest.py portal/static/js/program-sender.js`
> If any listed file changed, re-read the "Current state" facts below against the
> live code before proceeding; on a mismatch, STOP.

## Status

- **Priority**: P1
- **Effort**: S (about 1 day, needs a human with a Google account and Chrome)
- **Risk**: LOW (no code changes)
- **Depends on**: none
- **Category**: direction
- **Planned at**: commit `250aefde`, 2026-11-01
- **Issue**: https://github.com/fossasia/voxbento/issues/1087

## Why this matters

Plans 003–006 rest on four facts that can only be confirmed with real
YouTube/Chrome/third-party behaviour. If any is false, the dependent plan must
change before anyone writes code. The spike output is a short results file that
those plans read.

## Current state

- `portal/static/js/program-sender.js` publishes floor audio over authenticated
  WHIP using `navigator.mediaDevices.getUserMedia` (line ~116) and a short-lived
  session secret minted by `POST /admin/events/{id}/rooms/{id}/program-sender`.
- `mediamtx.yml` keeps `rtmp: no` (line ~81). RTMPS is opened only by
  `docker-compose.relay.yml` (`MTX_RTMP: "yes"`, `MTX_RTMPENCRYPTION: "strict"`,
  `MTX_RTMPSADDRESS: ":1936"`).
- RTMPS publish auth (`portal/program_ingest.py`, `presented_secret`) expects the
  encoder's stream key in the form `floor?key=<secret>` and the server URL
  `rtmps://<host>:1936/<event_slug>/<room_id>`. The query must be exactly one `key`
  parameter.
- No code talks to the YouTube Data API today.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Local stack | `docker compose -f docker-compose.yml -f docker-compose.relay.yml up -d` (needs `.env` from `scripts/setup-youtube-relay.sh` or a test cert) | services healthy |
| MediaMTX paths | `curl -s http://127.0.0.1:9997/v3/paths/list \| python3 -m json.tool` | JSON with `items` |
| Unit tests (sanity only) | `uv run pytest tests/test_program_sender.py tests/test_program_relay.py -q` | all pass |

## Scope

**In scope** (the only file you create): `plans/002-results.md`.

**Out of scope**: every source, template, test, config or doc file. Do not commit
any Google credentials, stream keys, VoxBento keys or caption ingestion URLs to the
results file. Write `<redacted>` instead.

## Git workflow

- No branch needed; the results file is a planning artifact. Do not commit unless
  the operator asks.

## Steps

### Step 1: Chrome tab audio → WHIP → MediaMTX

1. In Chrome (stable), open any page and run in DevTools:
   ```js
   const s = await navigator.mediaDevices.getDisplayMedia({video: true, audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: false, suppressLocalAudioPlayback: false}, selfBrowserSurface: 'exclude', surfaceSwitching: 'include', systemAudio: 'exclude'});
   s.getAudioTracks().length
   ```
   In the picker choose a **Chrome tab** playing a YouTube live stream and keep
   "Also share tab audio" ticked.
2. Record: number of audio tracks (expect 1); whether audio keeps playing locally
   (expect yes with `suppressLocalAudioPlayback: false`); whether stopping the video
   track (`s.getVideoTracks()[0].stop()`) keeps the audio track `readyState === 'live'`.
3. Repeat in Edge, Firefox and Safari. Record which give a tab audio track.
4. Background the shared tab for 10 minutes (switch to another tab, do not minimise
   the window). Record whether the audio track stays live and audible.
5. Click Chrome's "Stop sharing" bar. Record that the audio track fires `ended`.

**Verify**: results file has a table *Browser | tab audio track | survives video stop | survives 10 min background | ended fires*.

### Step 2: Third-party "Custom RTMP" destination → VoxBento RTMPS

Using a VoxBento instance with relay mode enabled (`docs/program-relay.md` →
Server setup) and a room in relay mode with **no YouTube key saved**:

1. Copy the room's **Server** (`rtmps://<host>:1936/<event>/<room>`) and **Stream key**
   (`floor?key=…`).
2. Add them as a "Custom RTMP" destination in each of: StreamYard, Restream,
   OBS with the "Multiple RTMP outputs" plugin, and vMix (if available).
3. Stream 2 minutes of speech. Record per platform: accepted `rtmps://`? accepted the
   `?` in the key? MediaMTX shows the path ready (`/v3/paths/list`)? Captions appeared?
4. If a platform rejects or rewrites `floor?key=…`, test two alternative key shapes by
   reading MediaMTX docs for RTMP credentials (`rtmps://user:pass@host/…` or a
   `?user=…&pass=…` query) and record which one the platform accepts. **Do not change code.**

**Verify**: results table *Platform | rtmps ok | key format ok | path ready | captions ok | notes*.

### Step 3: YouTube API fields with a real channel

1. Create a Google Cloud project, enable **YouTube Data API v3**, create an OAuth
   client (Web application), add yourself as a test user.
2. Use the OAuth 2.0 Playground (developers.google.com/oauthplayground) with your own
   client and scope `https://www.googleapis.com/auth/youtube.force-ssl`.
3. Create an **unlisted** live event in YouTube Studio. Call:
   - `GET https://www.googleapis.com/youtube/v3/liveBroadcasts?part=snippet,contentDetails,status&broadcastStatus=upcoming&broadcastType=all&maxResults=50`
   - `GET https://www.googleapis.com/youtube/v3/liveStreams?part=cdn,status,contentDetails&id=<contentDetails.boundStreamId>`
4. Record (redacted): that `cdn.ingestionInfo.streamName` is present and equals the
   Studio stream key; the `ingestionAddress` host (`a.rtmp.youtube.com`?);
   `contentDetails.closedCaptionsIngestionUrl` host and path shape (redact query);
   `isDefaultStream`; the quota cost shown in the Cloud console per call type.
5. Repeat call 3 with scope `youtube.readonly` only. Record whether `streamName` is still returned.
6. Try `liveBroadcasts.update?part=contentDetails` setting
   `closedCaptionsType=closedCaptionsHttpPost` (send the full current `contentDetails`
   back with only that field changed). Record success, and whether it also works while
   the broadcast is `live`.

**Verify**: results section *YouTube API* answers 4–6 explicitly with yes/no.

### Step 4: Caption POST format

1. With the broadcast from step 3 streaming (any encoder) and captions set to "POST
   captions to URL", POST to the caption ingestion URL with `&seq=1`, `&seq=2`, …:
   ```
   Content-Type: text/plain

   2026-11-01T10:00:00.000
   Hello from VoxBento
   ```
   (Use the current UTC time, millisecond precision, no `Z` if the help page says so;
   follow support.google.com/youtube/answer/6077032 and answer/3068031 exactly.)
2. Record: HTTP status and body; whether text appears in the YouTube player CC;
   observed delay; what happens with a timestamp 30 s in the past and 30 s in the future;
   whether non-ASCII text (e.g. Hindi, German umlauts) renders.

**Verify**: results section *Caption POST* with the exact working body template.

## Test plan

No automated tests. Run the sanity command from the table to confirm the tree is unchanged:
`uv run pytest tests/test_program_sender.py tests/test_program_relay.py -q` → all pass.

## Done criteria

- [ ] `plans/002-results.md` exists with the four sections above, each with a yes/no verdict.
- [ ] `grep -nE "key=[A-Za-z0-9_-]{20,}|ya29\.|1//0" plans/002-results.md` returns no matches (no secrets).
- [ ] `git status --short` shows no modified tracked files.
- [ ] `plans/README.md` status row for 002 updated.

## STOP conditions

- Chrome no longer returns a tab audio track from `getDisplayMedia` → STOP; plan 003 must be redesigned.
- No tested platform accepts any RTMPS key shape MediaMTX can authenticate → STOP; plan 004 must be redesigned.
- `streamName` is not returned with `youtube.force-ssl` → STOP; plan 005's automatic-key step is invalid.
- Caption POST never renders in the player → STOP; plan 006 is blocked.

## Maintenance notes

- Re-run step 1 after major Chrome releases if the tab-audio source regresses.
- Step 2's platform list is what plan 007's docs will name as "tested"; keep it current.
