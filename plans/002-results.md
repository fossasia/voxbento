# Plan 002 results: YouTube-intake assumptions

Measurements for [plan 002](002-youtube-intake-spike.md), issue #1087. No source files were
changed. Every stream key, OAuth token, client secret, stream id and caption ingestion URL is
written as `<redacted>`.

This file consolidates two drafts. Each observation names its operator, date and commit:

- @hazelmayank (#1096): Step 1 browser measurements (regular video and live stream), Step 3 API
  observations, Step 4 caption POST observations.
- @SanketKokate712 (#1091): preflight at `4e14999`, the Step 2 reading of `portal/program_ingest.py`
  (code reading, not yet observed on a platform), and the PENDING scaffold.

Rows marked **PENDING** need a real browser, Google account or streaming platform and have not
been measured. They are not passes. Plan 002 stays IN PROGRESS until every step has a verdict.

## Summary

| Step | Assumption | Verdict | Gates |
|---|---|---|---|
| 1 | Chrome gives a live tab-audio track from `getDisplayMedia` that survives stopping video and backgrounding | YES in Chrome 156 and Edge 156, for a regular video and for a live stream (synthetic test stream); Firefox 159 gives no audio track; Safari untested | Plan 003 (merged in #1097) |
| 2 | At least one third-party platform publishes to VoxBento RTMPS with the `floor?key=<secret>` key | PENDING | Plan 004 |
| 3 | `liveStreams.list` returns `cdn.ingestionInfo.streamName` with `youtube.force-ssl` | YES with `youtube.force-ssl`. Whether a `youtube.readonly`-only grant returns `streamName` is **unconfirmed**: the scopes granted for that call were not captured (see Step 3). Quota costs are 1 unit per call (documented) | Plan 005 |
| 4 | A caption POST to `closedCaptionsIngestionUrl` renders in the YouTube player | YES on a live unlisted broadcast; delay not timed | Plan 006 |

## Preflight (historical)

These describe the commits that were tested. Current `dev` already contains tab capture
(plan 003, merged in #1097), so the `getDisplayMedia` observation below is no longer true
of `dev`.

- @hazelmayank, 2026-11-01, `dev` at `c17147c`: drift check
  `git diff --stat 250aefde..HEAD -- mediamtx.yml docker-compose.yml docker-compose.relay.yml portal/program_ingest.py portal/static/js/program-sender.js`
  printed nothing. `uv run pytest tests/test_program_sender.py tests/test_program_relay.py -q` -> 38 passed.
- @SanketKokate712, 2026-11-01, `dev` at `4e14999` (from #1091), same drift check, no drift. `uv run pytest tests/test_program_sender.py tests/test_program_relay.py -q` -> 38 passed. Facts re-read against the code, true at that commit:
  - `portal/static/js/program-sender.js:116` captured with `getUserMedia({audio: …})`; no `getDisplayMedia`.
  - `mediamtx.yml:81` is `rtmp: no`; `docker-compose.relay.yml` sets `MTX_RTMP: "yes"`,
    `MTX_RTMPENCRYPTION: "strict"`, `MTX_RTMPSADDRESS: ":1936"`.
  - `portal/program_ingest.py` `presented_secret` accepts RTMP only with relay enabled,
    no user/password, and a query that is exactly one `key` parameter.
  - No code calls the YouTube Data API (`grep -rn "youtube/v3" portal` is empty).

## Step 1: Chrome tab audio

The capture was started from the DevTools console of another tab, with the constraints exactly
as in the plan. Chrome and Edge ran it from the console, which works because "Treat code
evaluation as user action" is on.

### Run A: regular YouTube video (operator @hazelmayank, 2026-11-01, `dev` at `c17147c`)

Source: a regular YouTube video (not a live stream) in a separate tab, "Also share tab audio" ticked.

| Browser (version) | Tab audio track | Plays locally | Survives video stop | Track stays `live` for 10 min in background | Audio heard for the whole 10 min | `ended` fires on Stop sharing |
|---|---|---|---|---|---|---|
| Chrome 156.0.8078.74 (Official Build, 64-bit) | yes (1 track, label "Tab audio") | yes | yes (`readyState` stayed `live`) | yes (`live`, `muted: false` logged every minute, no mute events) | not recorded | yes (`ended` at 2026-11-01T07:14:21Z) |
| Edge 156.0.4314.50 (Official build, 64-bit) | yes (1 track, label "Tab audio") | yes | yes (`readyState` stayed `live`) | yes (`live`, `muted: false` logged every minute, no mute events) | yes (heard throughout) | yes (`ended` at 2026-11-01T07:30:56Z) |
| Firefox 159.0 (64-bit) | no (0 audio tracks, 1 video track; `displaySurface` not reported) | n/a | n/a | n/a | n/a | n/a |
| Safari | untested (no Mac available) | - | - | - | - | - |

### Run B: YouTube live stream (operator @hazelmayank, 2026-11-05, `dev` at `6d9cbd4`)

Source: an unlisted YouTube live broadcast on the operator's own channel, watched in a normal tab.
The stream was a synthetic signal pushed with ffmpeg over RTMPS (test pattern plus a 440 Hz tone),
not a real event.

| Browser (version) | Tab audio track | Survives video stop | Track stays `live` for 10 min in background | Audio heard for the whole 10 min | `ended` fires on Stop sharing |
|---|---|---|---|---|---|
| Chrome 156.0.8078.74 | yes (1 track, "Tab audio") | yes (`live`) | yes (`live`, `muted: false` every minute, no `mute` event) | yes, apart from about 30 s (operator estimate) during the 10 min when the player was paused | yes (`ended` at 2026-11-05T11:43:10.072Z) |
| Edge 156.0.4314.50 | yes (1 track, "Tab audio") | yes (`live`) | yes (`live`, `muted: false` every minute, no `mute` event) | yes (heard throughout) | yes (`ended` at 2026-11-05T11:56:47.256Z) |

Notes:

- A paused player does not change the track state (`live`, `muted: false`), so "track survives" and "audio was audible" are separate measurements. In Chrome run B the player was paused for about 30 s of the 10 min (operator estimate, not timed), and the track stayed `live` and unmuted during that time; only Edge run B was audible for the whole 10 min.
- Firefox 159.0 exposes `getDisplayMedia` but returned no audio track and does not report `displaySurface`. Firefox rejects the same console call with `InvalidStateError` (transient activation required) and needs a real click.
- Chrome 156 and Edge 156 report `getSupportedConstraints().suppressLocalAudioPlayback` as `true`; Firefox 159 does not report it (checked 2026-11-01).
- Not measured: Safari, other Chrome/Edge versions, and a real (non-synthetic) live event.

**Verdict: YES** for Chrome 156 and Edge 156, with a regular video and with a live stream. Firefox 159: no tab audio. Safari: untested.

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
  hook answers **429** for the rest of that minute. A platform that retries quickly can
  lock itself out for a minute even after the key is fixed, because the throttle is
  checked before the credential.
- A platform that appends its own query parameters (anything besides `key`) is rejected
  with 401, because the query must be exactly one `key` parameter.
- Alternative formats are not accepted today. `presented_secret` returns empty when a
  token or password is present, so `rtmps://user:pass@host/…` fails if MediaMTX forwards
  the password to the hook, and `?user=…&pass=…` fails because the query must be exactly
  one `key` parameter. Any platform that needs them is a future code change for plan 004.
  (Code reading only, not observed on a platform.)

Suggestions from @SanketKokate712 for whoever runs the remaining steps (not measurements,
not run by him):

- Step 2: wait 60 s between failed attempts, because of the throttle above.
- Step 3: when calling `liveBroadcasts.update`, include `snippet.title` and copy the whole
  current `contentDetails`, since omitted fields may reset to defaults.
- After the run: rotate the VoxBento key, the Studio stream key and the OAuth client.

**Verdict: PENDING** (nothing observed on a platform yet).

## Step 3: YouTube API

Operator @hazelmayank. OAuth client: Web application in a test-mode Cloud project, tester = the
operator, via the OAuth 2.0 Playground. Broadcast: unlisted, scheduled in YouTube Studio with
latency "Normal" and closed captions set to "POST captions to URL". Calls on 2026-11-01 ran
before live streaming was enabled on the channel; the rest ran on 2026-11-05.

| Question | Answer |
|---|---|
| Plan 002's original example `liveBroadcasts.list` call with both `mine=true` and `broadcastStatus=upcoming` (2026-11-01) | **400** `incompatibleParameters: mine, broadcastStatus`. The list filters (`broadcastStatus`, `id`, `mine`) are alternatives, so the call must use `broadcastStatus=upcoming` alone, as the [API reference](https://developers.google.com/youtube/data/live/reference/rest/v3/liveBroadcasts/list) says. The example in plan 002 is corrected in this PR. |
| `liveBroadcasts.list?broadcastStatus=upcoming` on a channel with no broadcast and live streaming not yet enabled (2026-11-01) | **200**, `items: []`, `pageInfo.totalResults: 0`. One run only: it does not show that `liveStreamingNotEnabled` can never occur, since the reference still documents that error. |
| `channels.list?part=snippet&mine=true` with `youtube.force-ssl` (2026-11-01) | **200**, one channel in `items` |
| `liveBroadcasts.list` (`broadcastStatus=upcoming`) lists the broadcast and `contentDetails.boundStreamId` (2026-11-05) | **200**, 1 item, `lifeCycleStatus: ready`, `privacyStatus: unlisted`, `latencyPreference: normal`, `closedCaptionsType: closedCaptionsHttpPost` (set in Studio), `enableClosedCaptions: true`, `boundStreamId: <redacted>` present |
| `cdn.ingestionInfo.streamName` present with `youtube.force-ssl` | **yes** (`liveStreams?part=cdn,status,contentDetails&id=<boundStreamId>` returned 200) |
| `streamName` equals the Studio stream key | not compared by the operator. Separately observed: `streamName` has the same value as the `cid` query parameter of `closedCaptionsIngestionUrl` |
| `cdn.ingestionInfo.ingestionAddress` host | `a.rtmp.youtube.com` (`rtmp://a.rtmp.youtube.com/live2`). The response also returns `rtmpsIngestionAddress` (`rtmps://a.rtmps.youtube.com/live2`) and backup addresses on `b.rtmp.youtube.com` / `b.rtmps.youtube.com` |
| `contentDetails.closedCaptionsIngestionUrl` host and path (query redacted) | `http://upload.youtube.com/closedcaption?cid=<redacted>`: **`http`** scheme as returned; `https` was also accepted (see Step 4) |
| `contentDetails.isDefaultStream` | **absent** from the response. `contentDetails.isReusable: true` was returned instead |
| `streamName` returned with `youtube.readonly` only | **UNCONFIRMED.** The call was repeated after re-authorising for `youtube.readonly` and returned 200 with `streamName` and `closedCaptionsIngestionUrl`, but the scopes the token was granted were not captured and may still have included `youtube.force-ssl`. This does not show what a read-only grant alone exposes. Needs a re-run with the granted scopes recorded |
| `liveBroadcasts.update` setting `closedCaptionsType=closedCaptionsHttpPost` succeeds while `ready` (`upcoming`) | **200**. The body copied the whole `contentDetails` (including `boundStreamId` and `boundStreamLastUpdateTimeMs`), omitted `enableClosedCaptions`, and carried `snippet.title` and `snippet.scheduledStartTime` (already in the past). Round trip `closedCaptionsDisabled` -> `closedCaptionsHttpPost` both **200**; `enableClosedCaptions` followed the type (`false` then `true`) |
| Same update while `live` | **403** `enableClosedCaptionsModificationNotAllowed` ("Modification on enableClosedCaptions is not allowed in current status") when setting `closedCaptionsDisabled`, and **also 403 with the same reason when sending `closedCaptionsHttpPost` while the broadcast was already `closedCaptionsHttpPost`** (a no-op update) |

Other observations: `liveStreams.list` returned one item but `pageInfo.totalResults: 0`.
`liveBroadcasts.update` responses with `part=id,snippet,contentDetails` do not include `status`.

Quota cost per call type, from the [quota cost table](https://developers.google.com/youtube/data/determine_quota_cost)
(read 2026-11-05; documented cost, not measured per call):

| Call | Units (documented) |
|---|---|
| `liveBroadcasts.list` | 1 |
| `liveStreams.list` | 1 |
| `liveBroadcasts.update` | 1 |
| `channels.list` (plan 005's channel lookup) | 1 |

Observed in the Cloud console (project dashboard, "1 day" view, 2026-11-05): 7 requests to the
YouTube Data API v3 with 28% errors, which matches this run: 1 `liveBroadcasts.list`, 2 `liveStreams.list`
and 4 `liveBroadcasts.update`, of which 2 were rejected with 403. That is 7 units against the default
10,000 units/day, shown as 0% in the console. Per-call units were not isolated with a before/after
reading, and it was not checked whether a rejected call is charged.

**Verdict: YES** for `streamName`, the ingestion addresses and `closedCaptionsIngestionUrl` with
`youtube.force-ssl` on this channel. What a `youtube.readonly`-only grant returns is unconfirmed (granted scopes not captured).

## Step 4: Caption POST

Operator @hazelmayank, 2026-11-05. Broadcast from Step 3, `live`, captions set to "POST captions to URL",
source language English, viewed in a normal Chrome watch page with CC on. The stream was a synthetic
ffmpeg signal over RTMPS. Posts were made with `curl` from a WSL shell over `https`
(`http` also returned 200 once, before the broadcast was live).

Request: `POST <closedCaptionsIngestionUrl>&seq=<n>`, `Content-Type: text/plain; charset=UTF-8`.

| Case | HTTP status / body | Rendered in player CC | Delay |
|---|---|---|---|
| Before the broadcast was live, current timestamp (`http`) | 200, body = server time | not observed | n/a |
| Current UTC timestamp, live (`https`) | 200, body = server time | **yes** ("Hello from VoxBento" shown in CC) | not timed (operator's informal estimate 20-30 s, not measured) |
| Timestamp 30 s in the past | 200 | render not captured | n/a |
| Timestamp 30 s in the future | 200 | **yes** ("Thirty seconds in the future" shown) | n/a |
| Timestamp 90 s in the past | **400** `Timestamp skew from local=<server time> larger than 60 sec` | n/a | n/a |
| Timestamp 90 s in the future | **400** same error | n/a | n/a |
| Timestamp 1 h in the past | **400** same error | n/a | n/a |
| Non-ASCII (Hindi) | 200 | **yes** (Devanagari rendered correctly) | n/a |
| Non-ASCII (German umlauts, `ß`) | 200 | **yes** (`Grüße` rendered) | n/a |

Working body template (timestamp line, then the caption text, in UTC with millisecond precision):

```
2026-11-05T11:24:52.978
Hello from VoxBento
```

Observations:

- The response body is YouTube's server time. A timestamp more than 60 s from it, in either direction, is rejected with 400; 30 s was accepted. The exact boundary between 30 s and 90 s was not tested.
- Rendering in the player was observed over `https` only. Over `http` the POST returned 200 once, before the broadcast was live, and its rendering was not observed.
- Each `curl` round trip took about 2 s (new TLS connection each time).
- Captions arrive in the player as a roll-up: later lines appear next to earlier ones.
- The caption delay was not timed. The operator's informal estimate (20-30 s) is plausibly the viewer latency of a "Normal" latency broadcast, but that is an inference, not a measurement. Plan 006's manual check does not wait for it: it uses a 60 s ceiling and records the measured delay in the PR.

**Verdict: YES.** A caption POST to the broadcast's ingestion URL renders in the YouTube player's native CC for ASCII and non-ASCII text. Delay is PENDING.

## Findings for plans 004, 005 and 006

Plan 006 (Steps 2 and 3 and its test list) and plan 002 (Step 3 items 3 and 5) are updated in this PR to apply the findings below.

- **Caption URL is as secret as the stream key.** The `cid` of `closedCaptionsIngestionUrl` has the same value as `streamName`. Never log it or return it to the browser (plan 006 already says so).
- **Use `https` for the caption POST.** The API returns an `http://` URL, but `https://` is accepted. A client that posts to the returned URL as-is sends the secret in clear text. Plan 006 Step 3 now says `activate()` rewrites the scheme to `https` before storing the URL.
- **Timestamps must stay within 60 s of YouTube's clock.** A caption that sat in a queue for more than about a minute is rejected with 400, and so is every caption if the host clock is off by more than 60 s. Plan 006 Step 3 therefore learns YouTube's clock offset from the `Date` header of its responses, clamps chunk timestamps to within 55 s of YouTube's clock (not the host's), retries once after a skew rejection, and gives a clock-skew health reason.
- **Live broadcasts cannot have their caption settings updated, even to the same value.** Plan 006 Step 2 used to check the lifecycle status before the "already `closedCaptionsHttpPost`" shortcut, so a broadcast set to "POST captions to URL" in Studio before it went live would have been refused. Step 2 now checks `ultraLow` first (it applies to already-configured broadcasts too), then refuses a `complete` or `revoked` broadcast, then returns early when the type is already `closedCaptionsHttpPost`, and only then checks the lifecycle status.
- **`isDefaultStream` is not returned** by `liveStreams.list` on this channel. Plan 005 should not rely on it; `isReusable` was present.
- **`youtube.readonly` and the stream key is unconfirmed.** A `liveStreams.list` call made after re-authorising for `youtube.readonly` returned `streamName`, but the granted scopes were not captured and may still have included `youtube.force-ssl`, so this run does not show what a read-only grant exposes. Plan 005 should not make a claim about it in its consent copy until the call is repeated with the granted scopes recorded (plan 002 Step 3, item 5, now asks for that).
- **The API also returns `rtmpsIngestionAddress`** (`rtmps://a.rtmps.youtube.com/live2`), which plan 004 can use when it relays to YouTube.

## Plan 003 and the tracker

`plans/README.md` gives per-step gates (003 needs Step 1; 004 Step 2; 005 Step 3; 006 Step 4). Plan 003's own dependency and STOP text said all of plan 002 had to be DONE, which contradicted that. They now match: plan 003 depends on plan 002 Step 1, so it can be DONE while plan 002 stays IN PROGRESS.
