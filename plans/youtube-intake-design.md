# YouTube intake: design brief

Written 1 November 2026 against commit `250aefde`. Issue: https://github.com/fossasia/voxbento/issues/1087. Source for plans 002–007 and the
upstream GitHub issue. Parent epic: fossasia/voxbento#746. Related: #748 (browser
sender, closed), #749 (RTMPS relay, closed), #750 (gateway hardening + SRT), #751
(guided floor-source setup).

## Goal

An organiser whose event is on YouTube gets VoxBento captions, translations and
translated speech **without Jitsi screen-sharing, without the floor bot, and without
copying stream keys**. The default path is: *Sign in with YouTube → pick the
broadcast → follow one short, tailored instruction*. For a stream the organiser does
not control, VoxBento offers a one-click browser-tab capture that replaces the
current Jitsi workaround.

## Today's workaround and why it hurts

```
YouTube → organiser's browser tab → screen-share into Jitsi (Opus re-encode, JVB SFU)
        → floor-bot headless Chromium decodes → PulseAudio → ffmpeg → RTSP → MediaMTX
        → ffmpeg → STT → captions / translation / TTS
```

About 8 hops and 2 lossy re-encodes. Every hop is a failure point: Jitsi lobby or
password, bot join failures, PulseAudio start-up, the organiser's laptop sleeping,
and Jitsi's echo, noise and gain processing on music. Pasting a YouTube URL straight
into the bot or a server-side downloader gets blocked by YouTube's bot detection,
which is what the team keeps hitting.

## Research: how the market does it

| Provider | Original-audio input | Output to audiences |
|---|---|---|
| Wordly | Organiser adds Wordly as an **extra RTMPS output** in their streaming studio (`rtmps://media.wordly.ai/live`, key = session id), or a dedicated computer with a virtual audio cable | Separate attendee page/iframe, link/QR |
| KUDO | AV desk/USB audio, NDI, or a virtual cable on a routing computer | KUDO player via link/QR; caption overlay via OBS + RTMP out |
| Interprefy | Audio from AV stack/encoder; documented YouTube flow is RTMP **out** of Interprefy | App/widget; RTMP per language to YouTube |

**None of them accepts a YouTube watch URL as input.** They all sit *beside* or
*before* YouTube: a second destination from the organiser's production tool, or a
dedicated audio routing device. Translated audio is delivered in parallel (link/QR,
widget) and captions can go back to YouTube.

Sources: Wordly RTMP Studio setup (support.brandlive.com/hc/en-us/articles/360049021831),
Wordly "Send audio to Wordly from another platform" (help.wordly.ai/use-wordly-with-other-streaming-platforms),
KUDO onsite setup (support.kudoway.com/hc/en-us/articles/15106976725399), KUDO NDI caption
overlay (support.kudoway.com/hc/en-us/articles/40800864893335), Interprefy YouTube guide
(knowledge.interprefy.com/en/knowledge/how-to-stream-interprefy-interpretation-onto-a-youtube-live-event).

## Hard constraints

1. **YouTube API Services Developer Policies** (developers.google.com/youtube/terms/developer-policies):
   API clients must not "download, import, backup, cache, or store copies of YouTube
   audiovisual content", must not "separate, isolate, or modify the audio or video
   components", must not "apply alternate audio tracks to videos", and must not play
   content "from a background player". So:
   - no server-side yt-dlp/streamlink puller;
   - no headless browser playing YouTube;
   - no embedding YouTube's player in VoxBento and capturing its audio;
   - no listener page that mutes YouTube's embedded player and dubs our TTS over it.
2. **Repository invariant 3** (`agents.md`): "No YouTube pulling, no unauthenticated
   or plaintext RTMP, no SRT [in that slice]. Floor ownership is `jitsi_bot` or
   `program_ingest`; never last-publisher-wins." #746 lists "Pulling arbitrary
   YouTube pages or bypassing YouTube anti-bot controls" as a non-goal.
3. Media never traverses FastAPI. Exactly one floor owner per room. Credentials are
   write-only, room-scoped and never logged.

## What the YouTube Data/Live API *does* allow (verified against reference docs)

| Need | API | Scope |
|---|---|---|
| Show the organiser's channel | `channels.list?part=snippet&mine=true` | `youtube.readonly` or `youtube.force-ssl` |
| List live/upcoming broadcasts | `liveBroadcasts.list?broadcastStatus=active\|upcoming` | readonly or force-ssl |
| Resolve a pasted link to a broadcast | `liveBroadcasts.list?id=<videoId>` | readonly or force-ssl |
| Read the bound stream's ingest address and **stream key** | `liveStreams.list?part=cdn,status,contentDetails&id=<boundStreamId>` → `cdn.ingestionInfo.{ingestionAddress,streamName}` | readonly or force-ssl |
| Caption ingestion URL | same call → `contentDetails.closedCaptionsIngestionUrl` | readonly or force-ssl |
| Switch the broadcast to POSTed captions | `liveBroadcasts.update` with `contentDetails.closedCaptionsType=closedCaptionsHttpPost` | `youtube` or `youtube.force-ssl` |
| Stream health / lifecycle | `liveStreams.status.streamStatus`, `healthStatus`; `liveBroadcasts.status.lifeCycleStatus` | readonly or force-ssl |

Notes: ultra-low-latency broadcasts do not support closed captions. `youtube.force-ssl`
is a sensitive scope: 100 users is the hard cap for unverified apps, not the threshold when
verification becomes required. Public production applications requesting sensitive scopes
must complete Google OAuth verification before public use (subject to internal testing and
personal-use exceptions). Unverified apps display a consent warning screen. Every self-hosted
deployment registers its own Google OAuth client.

## Target organiser experience

Room page → **Room audio → From a YouTube stream**

1. **Sign in with YouTube** (one Google consent screen). Optional: organisers who
   refuse can still paste the stream key manually (the existing #749 flow).
2. **Pick the broadcast** from the list of live/upcoming broadcasts on the channel,
   or paste its link (`youtube.com/watch?v=…`, `youtube.com/live/…`, `youtu.be/…`,
   `studio.youtube.com/video/…/livestreaming`).
3. VoxBento asks **"How does video reach YouTube?"** and shows exactly one recipe:

| Answer | Recipe | Encoder change | Failure isolation |
|---|---|---|---|
| OBS / vMix / Wirecast / hardware encoder | **VoxBento forwards to YouTube** (#749 relay). Stream key fetched from YouTube automatically, never shown. Organiser pastes one VoxBento server + key into the encoder. | One-time destination change | VoxBento in the YouTube path |
| StreamYard / Restream / vMix multi-output / OBS multi-RTMP plugin / Zoom or Teams live streaming | **Add VoxBento as an extra destination** (new RTMPS-without-relay mode) | Add a "Custom RTMP" destination | YouTube independent |
| YouTube Studio webcam, phone app, or "it's not my stream" | **Share the YouTube tab** from VoxBento's sender page (Chrome/Edge tab audio → WHIP) | None | YouTube independent; a person keeps the tab open |

4. **Captions on YouTube** (toggle, on by default when signed in): VoxBento posts
   final captions (floor language, or one chosen translation) to the broadcast's
   caption ingestion URL, so viewers get native YouTube CC. **Important:** The YouTube Live
   API requires caption configuration (`closedCaptionsHttpPost`) to be set up **before**
   a broadcast starts. Active or live broadcasts cannot switch to HTTP-post captions via
   the API; for broadcasts already live or active without HTTP captions enabled, the UI
   shows a clear unsupported state explaining that "POST captions to URL" must be enabled
   in YouTube Studio settings prior to going live. Complementary option already
   on `dev`: encoder users can burn captions into the video with the room's
   **Captions overlay** URL as an OBS/vMix browser source (any number of languages,
   but only one per output).
5. Listeners get the existing VoxBento listener link/QR for every language with
   translated speech. Viewers keep watching on YouTube.
6. Status shows one line per stage: *YouTube broadcast* (created/testing/live/complete,
   stream health), *Audio → VoxBento*, *VoxBento → YouTube* (relay only), *Captions on
   YouTube*, *Captions and translation*.

Without sign-in, rows 2 and 3 of the table and the existing manual-key relay still
work. Jitsi + floor bot remains for real Jitsi meetings only.

## Architecture

```
                 ┌─────────── Google OAuth (event owner) ────────────┐
                 │  refresh token (Fernet) → access token in memory  │
                 ▼                                                   │
   portal/youtube_account.py  ── list broadcasts, read bound stream key/CC URL, status
        │ fills room relay destination        │ CC URL (memory only)
        ▼                                     ▼
   program_relay (existing)          portal/youtube_captions.py  ◀── aggregator finals /
                                                                     translation finals
 Encoder ─RTMPS─▶ MediaMTX ─RTSP▶ program-relay ─RTMPS─▶ YouTube      │ HTTP POST
 StreamYard ─RTMPS(second dest)─▶ MediaMTX                            ▼
 Chrome tab ─WHIP─▶ MediaMTX                                   YouTube native CC
        └── canonical {event}/{room}/floor ─▶ ffmpeg ▶ STT ▶ translation ▶ TTS (existing)
```

## Phases (plans)

| Plan | Slice | Effort | Depends on |
|---|---|---|---|
| 002 | Spike: verify tab audio, third-party RTMPS key format, caption POST format, OAuth fields | S | — |
| 003 | Browser-tab audio source in the program sender (kills the Jitsi workaround) | S | 002 (tab item) |
| 004 | RTMPS ingest without relay ("add VoxBento as an extra destination") | M | 002 (RTMPS item) |
| 005 | Sign in with YouTube: OAuth, broadcast picker, automatic stream key, status | L | 002 (OAuth item) |
| 006 | Captions posted to YouTube's native caption track | M | 005 |
| 007 | Guided "From a YouTube stream" flow + docs; retire Jitsi screen-share guidance | M | 003, 004, 005, 006; coordinate with #751 |

## Rejected options

| Option | Why rejected |
|---|---|
| Server-side yt-dlp / streamlink puller | Violates YouTube developer policies III.E.1; datacenter IPs get "Sign in to confirm you're not a bot"; cookie workarounds risk account bans; contradicts invariant 3 and #746 non-goals |
| Floor bot opens the watch/embed URL in headless Chromium | Same blocking; automated access; background-player rule |
| Embed YouTube player in VoxBento and capture its audio | "Separate, isolate, or modify the audio … components" |
| Listener page plays YouTube muted + our TTS as alternate audio | "Apply alternate audio tracks to videos" |
| YouTube multi-language audio tracks for live | Not available for live streams as far as could be verified; revisit if YouTube ships it |
| VoxBento creates a new YouTube `liveStream` and rebinds the organiser's broadcast | Silently changes the organiser's YouTube setup; reading the already-bound stream is enough |

## Open questions for maintainers

1. Plan 004 changes the wording of invariant 3 (RTMPS ingest no longer implies relay). OK?
2. Who registers the production Google OAuth client and owns the verification (privacy
   policy URL, homepage, demo video)?
3. One YouTube channel per **event** (proposed) or per room?
4. Captions to YouTube: floor language only (proposed default) or allow one translation?
