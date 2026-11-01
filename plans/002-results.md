# Plan 002 results: YouTube-intake assumptions

Measurements for [plan 002](002-youtube-intake-spike.md), issue #1087. No source files were
changed. Every stream key, OAuth token, client secret and caption ingestion URL is written
as `<redacted>`.

Run on: 2026-11-01, `dev` at `c17147c`, operator: @hazelmayank.

## Summary

| Step | Assumption | Verdict | Unblocks |
|---|---|---|---|
| 1 | Chrome gives a live tab-audio track from `getDisplayMedia` that survives stopping video and backgrounding | yes (Chrome and Edge; not Firefox) | Plan 003 |
| 2 | At least one third-party platform publishes to VoxBento RTMPS with the `floor?key=<secret>` key | TODO | Plan 004 |
| 3 | `liveStreams.list` returns `cdn.ingestionInfo.streamName` with `youtube.force-ssl` | TODO | Plan 005 |
| 4 | A caption POST to `closedCaptionsIngestionUrl` renders in the YouTube player | TODO | Plan 006 |

## Preflight

- Drift check: `git diff --stat 250aefde..HEAD -- mediamtx.yml docker-compose.yml docker-compose.relay.yml portal/program_ingest.py portal/static/js/program-sender.js`
  printed nothing. No drift.
- Current-state facts still true: `program-sender.js:116` captures with `getUserMedia`; `mediamtx.yml:81` is `rtmp: no`;
  `docker-compose.relay.yml` sets `MTX_RTMP: "yes"`, `MTX_RTMPENCRYPTION: "strict"`, `MTX_RTMPSADDRESS: ":1936"`;
  no code under `portal/` calls the YouTube Data API.
- Sanity tests: `uv run pytest tests/test_program_sender.py tests/test_program_relay.py -q` -> 38 passed.

## Step 1: Chrome tab audio

Source: a YouTube video playing in a separate Chrome tab (not a live stream), "Also share tab audio" ticked.
The capture was started from the DevTools console of another tab, with the constraints exactly as in the plan.

| Browser (version) | Tab audio track | Plays locally | Survives video stop | Survives 10 min background | `ended` fires on Stop sharing |
|---|---|---|---|---|---|
| Chrome 156.0.8078.74 (Official Build, 64-bit) | yes (1 track, label "Tab audio") | yes | yes (`readyState` stayed `live`) | yes (`live`, `muted: false` logged every minute for 10 min, no mute events) | yes (`ended` fired at 2026-11-01T07:14:21Z) |
| Edge 156.0.4314.50 (Official build, 64-bit) | yes (1 track, label "Tab audio") | yes | yes (`readyState` stayed `live`) | yes (`live`, `muted: false` logged every minute for 10 min, no mute events, audio heard throughout) | yes (`ended` fired at 2026-11-01T07:30:56Z) |
| Firefox 159.0 (64-bit) | no (0 audio tracks, 1 video track; `displaySurface` not reported) | n/a | n/a | n/a | n/a |
| Safari not tested (no Mac available) | - | - | - | - | - |

Notes:

- Chrome and Edge behave the same: one tab audio track, it survives stopping the video track and 10 minutes with the shared tab in the background, and `ended` fires when Stop sharing is clicked. Local playback with `suppressLocalAudioPlayback: false` was heard in both; in Edge the audio was also heard for the whole 10 minutes.
- Firefox 159.0 exposes `getDisplayMedia` but returns no audio track (0 audio tracks, 1 video track) and does not report `displaySurface`. Tab audio is therefore a Chrome/Edge feature, and a Firefox user reaches the "no tab audio was shared" path.
- Chrome and Edge ran the capture from the DevTools console, which works because "Treat code evaluation as user action" is on; Firefox rejects the same call with `InvalidStateError` (transient activation required) and needs a real click.
- The source was a YouTube video, not a live stream; the audio track does not depend on that.

**Verdict:** yes (Chrome and Edge). Plan 003 can proceed.

## Step 2: Third-party "Custom RTMP" -> VoxBento RTMPS

Instance: <host, relay mode on, room in relay mode, no YouTube key saved>.
Server `rtmps://<host>:1936/<event>/<room>`, stream key `floor?key=<redacted>`.

| Platform | rtmps ok | key format ok | path ready | captions ok | Notes (exact error if rejected) |
|---|---|---|---|---|---|
| StreamYard | | | | | |
| Restream | | | | | |
| OBS + Multiple RTMP outputs | | | | | |
| vMix | | | | | |

Alternative credential formats a platform needs (future code change, not tested here):

**Verdict:** yes / no

## Step 3: YouTube API

OAuth client: <Web application, test-mode project, tester = operator>. Broadcast: unlisted, created in YouTube Studio.

| Question | Answer |
|---|---|
| Plan 002's example `liveBroadcasts.list` call with both `mine=true` and `broadcastStatus=upcoming` | **400** `incompatibleParameters: mine, broadcastStatus` (2026-11-01). The list filters are alternatives, so the call must use `broadcastStatus=upcoming` alone, as plan 005 already does. |
| `liveBroadcasts.list` (`broadcastStatus=upcoming`) on a channel that has no broadcast and whose live streaming is not enabled yet | **200**, `items: []`, `pageInfo.totalResults: 0` (2026-11-01). No `liveStreamingNotEnabled` error, so "live not enabled" and "no upcoming broadcast" look the same. |
| `channels.list?part=snippet&mine=true` with `youtube.force-ssl` (the call plan 005 uses to show the organiser's channel) | **200**, one channel returned in `items` (2026-11-01) |
| `liveBroadcasts.list` (`broadcastStatus=upcoming`) lists the broadcast and `contentDetails.boundStreamId` | |
| `cdn.ingestionInfo.streamName` present with `youtube.force-ssl` | |
| `streamName` equals the Studio stream key | |
| `cdn.ingestionInfo.ingestionAddress` host | |
| `contentDetails.closedCaptionsIngestionUrl` host and path (query redacted) | |
| `contentDetails.isDefaultStream` | |
| `streamName` returned with `youtube.readonly` only | |
| `liveBroadcasts.update` setting `closedCaptionsType=closedCaptionsHttpPost` succeeds while `upcoming` | |
| Same update succeeds while `live` | |

Quota cost per call (Cloud console -> APIs & Services -> YouTube Data API v3 -> Quotas):

| Call | Units |
|---|---|
| `liveBroadcasts.list` | |
| `liveStreams.list` | |
| `liveBroadcasts.update` | |

**Verdict:** yes / no

## Step 4: Caption POST

Broadcast from step 3 live, captions set to "POST captions to URL".

| Case | HTTP status / body | Rendered in player CC | Delay |
|---|---|---|---|
| Current UTC timestamp, `seq=1` | | | |
| Timestamp 30 s in the past | | | n/a |
| Timestamp 30 s in the future | | | n/a |
| Non-ASCII (Hindi) | | | n/a |
| Non-ASCII (German umlauts) | | | n/a |

Working body template (timestamp format exactly as accepted):

```
<fill in>
```

**Verdict:** yes / no
