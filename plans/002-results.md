# Plan 002 results: YouTube-intake assumptions

Measurements for [plan 002](002-youtube-intake-spike.md), issue #1087. No source files were
changed. Every stream key, OAuth token, client secret and caption ingestion URL is written
as `<redacted>`.

This file consolidates two drafts. Each observation names its operator, date and commit:

- @hazelmayank (#1096): Step 1 browser measurements, first Step 3 API observations.
- @SanketKokate712 (#1091): preflight at `4e14999`, the Step 2 reading of `portal/program_ingest.py`
  (code reading, not yet observed on a platform), and the PENDING scaffold.

Rows marked **PENDING** need a real browser, Google account or streaming platform and have not
been measured. They are not passes. Plan 002 stays IN PROGRESS until every step has a verdict.

## Summary

| Step | Assumption | Verdict | Gates |
|---|---|---|---|
| 1 | Chrome gives a live tab-audio track from `getDisplayMedia` that survives stopping video and backgrounding | PARTIAL: yes for a regular YouTube video in Chrome and Edge; Firefox gives no audio track; **live-stream case PENDING**; Safari untested | Plan 003 (merged in #1097) |
| 2 | At least one third-party platform publishes to VoxBento RTMPS with the `floor?key=<secret>` key | PENDING | Plan 004 |
| 3 | `liveStreams.list` returns `cdn.ingestionInfo.streamName` with `youtube.force-ssl` | PENDING (three observations recorded; `streamName` not yet seen) | Plan 005 |
| 4 | A caption POST to `closedCaptionsIngestionUrl` renders in the YouTube player | PENDING | Plan 006 |

## Preflight (historical)

These describe the commits that were tested. Current `dev` already contains tab capture
(plan 003, merged in #1097), so the `getDisplayMedia` observation below is no longer true
of `dev`.

- @hazelmayank, 2026-11-01, `dev` at `c17147c`: drift check
  `git diff --stat 250aefde..HEAD -- mediamtx.yml docker-compose.yml docker-compose.relay.yml portal/program_ingest.py portal/static/js/program-sender.js`
  printed nothing. `uv run pytest tests/test_program_sender.py tests/test_program_relay.py -q` -> 38 passed.
- @SanketKokate712, `dev` at `4e14999` (from #1091), same drift check, no drift. Facts re-read against the code, true at that commit:
  - `portal/static/js/program-sender.js:116` captured with `getUserMedia({audio: …})`; no `getDisplayMedia`.
  - `mediamtx.yml:81` is `rtmp: no`; `docker-compose.relay.yml` sets `MTX_RTMP: "yes"`,
    `MTX_RTMPENCRYPTION: "strict"`, `MTX_RTMPSADDRESS: ":1936"`.
  - `portal/program_ingest.py` `presented_secret` accepts RTMP only with relay enabled,
    no user/password, and a query that is exactly one `key` parameter.
  - No code calls the YouTube Data API (`grep -rn "youtube/v3" portal` is empty).

## Step 1: Chrome tab audio

Operator @hazelmayank, 2026-11-01, commit `c17147c`.

Source: a **regular YouTube video** (not a live stream) in a separate tab, "Also share tab
audio" ticked. The plan specifies a live stream, so the live-stream case is still PENDING.
The capture was started from the DevTools console of another tab, with the constraints
exactly as in the plan.

| Browser (version) | Tab audio track | Plays locally | Survives video stop | Track stays `live` for 10 min in background | Audio heard for the whole 10 min | `ended` fires on Stop sharing |
|---|---|---|---|---|---|---|
| Chrome 156.0.8078.74 (Official Build, 64-bit) | yes (1 track, label "Tab audio") | yes | yes (`readyState` stayed `live`) | yes (`live`, `muted: false` logged every minute, no mute events) | not recorded | yes (`ended` at 2026-11-01T07:14:21Z) |
| Edge 156.0.4314.50 (Official build, 64-bit) | yes (1 track, label "Tab audio") | yes | yes (`readyState` stayed `live`) | yes (`live`, `muted: false` logged every minute, no mute events) | yes (heard throughout) | yes (`ended` at 2026-11-01T07:30:56Z) |
| Firefox 159.0 (64-bit) | no (0 audio tracks, 1 video track; `displaySurface` not reported) | n/a | n/a | n/a | n/a | n/a |
| Safari | untested (no Mac available) | - | - | - | - | - |

Notes:

- Chrome and Edge returned one tab audio track and kept it `live` for 10 minutes with the shared tab in the background; `ended` fired when Stop sharing was clicked. Local playback with `suppressLocalAudioPlayback: false` was heard in both. The Chrome row records track liveness; audible audio for the full 10 minutes was only noted for Edge.
- Firefox 159.0 exposes `getDisplayMedia` but returned no audio track and does not report `displaySurface`. Chrome and Edge ran from the DevTools console, which works because "Treat code evaluation as user action" is on; Firefox rejects the same call with `InvalidStateError` (transient activation required) and needs a real click.
- Chrome 156 and Edge 156 report `getSupportedConstraints().suppressLocalAudioPlayback` as `true`; Firefox 159 does not report it (checked 2026-11-01).
- Not measured: a YouTube **live stream** as the source, Safari, and other Chrome/Edge versions.

**Verdict: PARTIAL.** Regular-video tab audio works in Chrome 156 and Edge 156. The live-stream case is PENDING and the claim for other browsers is limited to what is in the table.

## Step 2: Third-party "Custom RTMP" -> VoxBento RTMPS

Instance: PENDING (relay mode on, room in relay mode, no YouTube key saved; needs a public host with a TLS certificate on TCP 1936).
Server `rtmps://<host>:1936/<event>/<room>`, stream key `floor?key=<redacted>`.

| Platform | rtmps ok | key format ok | path ready | captions ok | Notes |
|---|---|---|---|---|---|
| StreamYard | PENDING | PENDING | PENDING | PENDING | |
| Restream | PENDING | PENDING | PENDING | PENDING | |
| OBS + Multiple RTMP outputs | PENDING | PENDING | PENDING | PENDING | |
| vMix | PENDING | PENDING | PENDING | PENDING | |

What the server does with a rewritten key, from reading `portal/program_ingest.py`
(@SanketKokate712, #1091; code reading only, the error text a platform shows is still to be observed):

- The secret is `secrets.token_urlsafe(32)` (43 chars of `[A-Za-z0-9_-]`), so it
  survives query parsing unchanged; only the `?` and `=` separators can be mangled.
- If a platform URL-encodes the `?` (`floor%3Fkey=…`), the last path component is no
  longer `floor`, so the request falls through to the interpreter-booth check and gets
  **401**. Each 401 counts toward the 10 failures/IP/minute throttle, after which the
  hook answers **429** for the rest of that minute.
- A platform that appends its own query parameters (anything besides `key`) is rejected
  with 401, because the query must be exactly one `key` parameter.
- Alternative formats such as `rtmps://user:pass@host/…` or `?user=…&pass=…` are **not**
  accepted today (`presented_secret` returns empty when a user or password is present).
  Any platform that needs them is a future code change for plan 004.

**Verdict: PENDING** (nothing observed on a platform yet).

## Step 3: YouTube API

Operator @hazelmayank, 2026-11-01. OAuth client: Web application in a test-mode Cloud project,
tester = the operator. Broadcast: unlisted, to be created in YouTube Studio. YouTube live
streaming was not enabled yet on the channel when these first calls ran.

| Question | Answer |
|---|---|
| Plan 002's original example `liveBroadcasts.list` call with both `mine=true` and `broadcastStatus=upcoming` | **400** `incompatibleParameters: mine, broadcastStatus`. The list filters (`broadcastStatus`, `id`, `mine`) are alternatives, so the call must use `broadcastStatus=upcoming` alone, as the [API reference](https://developers.google.com/youtube/data/live/reference/rest/v3/liveBroadcasts/list) says. The example in plan 002 is corrected in this PR. |
| `liveBroadcasts.list?broadcastStatus=upcoming` on a channel with no broadcast and live streaming not yet enabled | **200**, `items: []`, `pageInfo.totalResults: 0`. This is one run only: it does not show that `liveStreamingNotEnabled` can never occur, since the reference still documents that error. |
| `channels.list?part=snippet&mine=true` with `youtube.force-ssl` (the call plan 005 uses to show the organiser's channel) | **200**, one channel in `items` |
| `liveBroadcasts.list` (`broadcastStatus=upcoming`) lists the broadcast and `contentDetails.boundStreamId` | PENDING |
| `cdn.ingestionInfo.streamName` present with `youtube.force-ssl` | PENDING |
| `streamName` equals the Studio stream key | PENDING |
| `cdn.ingestionInfo.ingestionAddress` host | PENDING |
| `contentDetails.closedCaptionsIngestionUrl` host and path (query redacted) | PENDING |
| `contentDetails.isDefaultStream` | PENDING |
| `streamName` returned with `youtube.readonly` only | PENDING |
| `liveBroadcasts.update` setting `closedCaptionsType=closedCaptionsHttpPost` succeeds while `upcoming` | PENDING |
| Same update succeeds while `live` | PENDING |

A channel lookup or an empty upcoming list does not verify `streamName`, the caption URL,
broadcast updates or rendered native captions.

Quota cost per call type (Cloud console -> APIs & Services -> YouTube Data API v3 -> Quotas):

| Call | Units |
|---|---|
| `liveBroadcasts.list` | PENDING |
| `liveStreams.list` | PENDING |
| `liveBroadcasts.update` | PENDING |

**Verdict: PENDING.**

## Step 4: Caption POST

Broadcast from step 3 live, captions set to "POST captions to URL".

| Case | HTTP status / body | Rendered in player CC | Delay |
|---|---|---|---|
| Current UTC timestamp, `seq=1` | PENDING | PENDING | PENDING |
| Timestamp 30 s in the past | PENDING | PENDING | n/a |
| Timestamp 30 s in the future | PENDING | PENDING | n/a |
| Non-ASCII (Hindi) | PENDING | PENDING | n/a |
| Non-ASCII (German umlauts) | PENDING | PENDING | n/a |

Working body template: PENDING.

**Verdict: PENDING.**

## Documentation gap to decide

`plans/README.md` gives per-step gates (003 needs Step 1; 004 Step 2; 005 Step 3; 006 Step 4),
while plan 003's own dependency and STOP text require all of plan 002 to be DONE. Plan 003 is
already merged, so this does not block it, but the two documents should be reconciled by a maintainer.
