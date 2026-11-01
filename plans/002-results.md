# Plan 002 results: YouTube-intake assumptions

Spike for [plan 002](002-youtube-intake-spike.md), issue #1087. No source files were
changed. All credentials, stream keys, OAuth tokens and caption ingestion URLs are
written as `<redacted>`.

Rows marked **PENDING** are observations that need a real browser, Google account or
streaming platform. They are filled from the recorded screencast before the plan
status moves to DONE.

## Summary

| Step | Assumption | Verdict | Unblocks |
|---|---|---|---|
| 1 | Chrome gives a live tab-audio track from `getDisplayMedia` that survives stopping video and backgrounding | PENDING | Plan 003 |
| 2 | At least one third-party platform publishes to VoxBento RTMPS with the `floor?key=<secret>` key | PENDING | Plan 004 |
| 3 | `liveStreams.list` returns `cdn.ingestionInfo.streamName` with `youtube.force-ssl` | PENDING | Plan 005 |
| 4 | A caption POST to `closedCaptionsIngestionUrl` renders in the YouTube player | PENDING | Plan 006 |

## Preflight

- Run against `dev` at `4e14999` (plan written at `250aefde`).
- Drift check: `git diff --stat 250aefde..HEAD -- mediamtx.yml docker-compose.yml docker-compose.relay.yml portal/program_ingest.py portal/static/js/program-sender.js`
  printed nothing. **No drift.**
- "Current state" facts re-read against the code, all still true:
  - `portal/static/js/program-sender.js:116` captures with `getUserMedia({audio: …})`; no `getDisplayMedia` anywhere.
  - `mediamtx.yml:81` is `rtmp: no`; `docker-compose.relay.yml` sets `MTX_RTMP: "yes"`,
    `MTX_RTMPENCRYPTION: "strict"`, `MTX_RTMPSADDRESS: ":1936"`.
  - `portal/program_ingest.py` `presented_secret` accepts RTMP only with relay enabled,
    no user/password, and a query that is exactly one `key` parameter.
  - No code calls the YouTube Data API (`grep -rn "youtube/v3" portal` is empty).
- Sanity tests: `uv run pytest tests/test_program_sender.py tests/test_program_relay.py -q`
  → **38 passed**.

## Step 1: Chrome tab audio

Capture constraints exactly as in the plan (`video: true`, all audio processing off,
`suppressLocalAudioPlayback: false`, `selfBrowserSurface: 'exclude'`,
`surfaceSwitching: 'include'`, `systemAudio: 'exclude'`). Source: a YouTube live stream
playing in a Chrome tab, "Also share tab audio" ticked.

| Browser (version) | Tab audio track | Plays locally | Survives video stop | Survives 10 min background | `ended` fires on Stop sharing |
|---|---|---|---|---|---|
| Chrome PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Edge PENDING | PENDING | PENDING | PENDING | PENDING | PENDING |
| Firefox PENDING | PENDING | — | PENDING | PENDING | PENDING |
| Safari PENDING | PENDING | — | PENDING | PENDING | PENDING |

Notes: PENDING

**Verdict: PENDING**

## Step 2: Third-party "Custom RTMP" → VoxBento RTMPS

Instance: PENDING (relay mode on, room in relay mode, no YouTube key saved).
Server `rtmps://<host>:1936/<event>/<room>`, stream key `floor?key=<redacted>`.

| Platform | rtmps ok | key format ok | path ready | captions ok | Notes |
|---|---|---|---|---|---|
| StreamYard | PENDING | PENDING | PENDING | PENDING | |
| Restream | PENDING | PENDING | PENDING | PENDING | |
| OBS + Multiple RTMP outputs | PENDING | PENDING | PENDING | PENDING | |
| vMix | PENDING | PENDING | PENDING | PENDING | |

What the server does with a rewritten key (from reading `portal/program_ingest.py`; the
error text a platform shows is still to be observed):

- The secret is `secrets.token_urlsafe(32)` (43 chars of `[A-Za-z0-9_-]`), so it
  survives query parsing unchanged; only the `?` and `=` separators can be mangled.
- If a platform URL-encodes the `?` (`floor%3Fkey=…`), the last path component is no
  longer `floor`, so the request falls through to the interpreter-booth check and gets
  **401**. Each 401 counts toward the 10 failures/IP/minute throttle, after which the
  hook answers **429** for the rest of that minute. A platform that retries quickly can
  lock itself out for a minute even after the key is fixed.
- A platform that appends its own query parameters (anything besides `key`) is rejected
  with 401, because the query must be exactly one `key` parameter.
- Alternative formats such as `rtmps://user:pass@host/…` or `?user=…&pass=…` are **not**
  accepted today (`presented_secret` returns empty when a user or password is present).
  Any platform that needs them is recorded as a future code change for plan 004.

**Verdict: PENDING**

## Step 3: YouTube API

OAuth client: Web application in a test-mode Cloud project, tester = the spike operator.
Broadcast: unlisted, created in YouTube Studio.

| Question | Answer |
|---|---|
| `liveBroadcasts.list` (`mine=true`, `broadcastStatus=upcoming`) lists the broadcast and `contentDetails.boundStreamId` | PENDING |
| `cdn.ingestionInfo.streamName` present with `youtube.force-ssl` | PENDING |
| `streamName` equals the Studio stream key | PENDING |
| `cdn.ingestionInfo.ingestionAddress` host | PENDING |
| `contentDetails.closedCaptionsIngestionUrl` host and path (query redacted) | PENDING |
| `contentDetails.isDefaultStream` | PENDING |
| `streamName` returned with `youtube.readonly` only | PENDING |
| `liveBroadcasts.update` setting `closedCaptionsType=closedCaptionsHttpPost` succeeds while `upcoming` | PENDING |
| Same update succeeds while `live` | PENDING |

Quota cost per call type (Cloud console → APIs & Services → YouTube Data API v3 → Quotas):

| Call | Units |
|---|---|
| `liveBroadcasts.list` | PENDING |
| `liveStreams.list` | PENDING |
| `liveBroadcasts.update` | PENDING |

**Verdict: PENDING**

## Step 4: Caption POST

Broadcast from step 3 live, captions set to "POST captions to URL".

| Case | HTTP status / body | Rendered in player CC | Delay |
|---|---|---|---|
| Current UTC timestamp, `seq=1` | PENDING | PENDING | PENDING |
| Timestamp 30 s in the past | PENDING | PENDING | — |
| Timestamp 30 s in the future | PENDING | PENDING | — |
| Non-ASCII (Hindi) | PENDING | PENDING | — |
| Non-ASCII (German umlauts) | PENDING | PENDING | — |

Working body template:

```
PENDING
```

**Verdict: PENDING**
