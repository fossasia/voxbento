# Sign in with YouTube (#1087)

Organisers use it from **Room audio → From a YouTube stream** (the guide for all YouTube
routes; see `website/docs/admin/youtube.mdx`) or from **From streaming software**.

An event owner connects the event's YouTube channel once, then picks each room's
broadcast from a list (or pastes its link). In relay mode
([RTMPS relay](program-relay.md)) VoxBento then fills in the YouTube stream key
itself, so nobody copies keys out of YouTube Studio. The room page also shows the
broadcast's YouTube-side status.

## What it does and does not do

VoxBento uses the YouTube Live Streaming API with the channel owner's consent to:

- read the channel id and title;
- list the channel's live and upcoming broadcasts;
- read the stream bound to the chosen broadcast: its ingest address, stream key,
  stream status and health.

It **never** fetches, downloads, embeds or pulls YouTube audio or video, never
changes the organiser's YouTube setup (no transitions, no new streams, no title
edits) and never shows the stream key in the browser. Audio for captions and
translation still comes from the room's own source (OBS, a browser tab or the
meeting). Pulling audio from a watch URL is not possible under YouTube's Developer
Policies, and VoxBento does not try.

## How it works

1. **Sign in with YouTube** on the room page (*Room audio → From streaming
   software*) opens `GET /workspace/events/{event_id}/youtube/connect?room_id=…`.
   The portal creates a random state nonce and a PKCE verifier, puts them with the
   event, room and signed-in user into a short-lived signed cookie (`vb_yt_oauth`,
   HttpOnly, SameSite=Lax, path `/youtube/oauth`, 10 minutes) and redirects to
   Google's consent screen (`access_type=offline`, `prompt=consent`, PKCE S256).
2. Google redirects to `{PUBLIC_BASE_URL}/youtube/oauth/callback`. The callback
   rejects the request unless the cookie is valid, `state` equals the nonce, the
   signed-in VoxBento user is the one who started the flow, and that user still owns
   the event (or is an Admin). It exchanges the code with the verifier, reads the
   channel, stores the **encrypted** refresh token and returns to the room page.
   Signing in again with the same channel keeps every room's broadcast. Signing in
   with a *different* channel clears every room's broadcast and, for every room that
   was linked to one, its saved relay destination: both the server URL and the stream
   key, including values entered by hand. After switching channels, choose each room's
   broadcast again, or enter its server URL and stream key again. An empty server URL
   means YouTube's primary RTMPS server, so a room that used a custom server needs both
   values restored.
3. **Use this broadcast** (`PUT …/rooms/{room_id}/youtube/broadcast`) accepts a
   broadcast from the list or a pasted link. VoxBento checks the broadcast is on the
   connected channel and has a stream bound in YouTube Studio, then saves it on the
   room. In relay mode it also stores the bound stream's ingest address and key as
   the relay destination (encrypted, write-only, exactly like a pasted key). If relay
   mode is switched on after the broadcast was linked and the room has no saved stream
   key yet, the room page links it once more on its next status check, so the key is
   filled in then. A stream key that is already saved (pasted by hand or from an
   earlier broadcast) is kept: click **Use this broadcast** again to replace it with the
   chosen broadcast's key.
4. The room status (`GET …/program-ingest`) adds a `youtube` block with the
   broadcast's life-cycle status, stream status and health, cached for 60 s per room.
5. **Captions on YouTube** (`PUT …/rooms/{room_id}/youtube/captions`) posts one caption
   language to the broadcast's own CC track. See [Captions on YouTube](#captions-on-youtube).

## Captions on YouTube

Once a room is linked to a broadcast, **Captions on YouTube** on the room page chooses
what goes into the YouTube player's CC button: **Off**, the spoken language, or one of
the room's enabled translation languages (floor translation must be on). YouTube takes
one caption track per broadcast; viewers can use YouTube's auto-translate on it, and
every language plus translated speech stays on VoxBento's listener page. Only final
captions are posted, never interim text, and only the room's floor captions (not
interpreter booths).

Turning captions on:

1. VoxBento reads the broadcast. **Ultra-low latency** broadcasts cannot show captions
   (choose Normal or Low latency in Studio); an **ended** broadcast is refused, and so is one
   bound in Studio to a stream other than the one the room linked (choose the broadcast again).
2. If the broadcast is not yet set to *POST captions to URL*, VoxBento switches it, sending
   the broadcast's own settings (title, description, category, schedule and stream settings)
   back unchanged apart from the caption type. YouTube only
   allows this **before the broadcast goes live**. A live broadcast that was already set to
   *POST captions to URL* in Studio is accepted as it is; one that was not cannot be changed
   until its next broadcast.
3. VoxBento reads the stream's private caption address and starts posting. The address
   carries the stream key, so it stays in the portal's memory: it is never stored, logged
   or sent to the browser, and VoxBento posts over `https` although YouTube returns `http`.

The choice is saved on the room only when these steps worked; a refusal is shown on the
page and changes nothing. The status line then reads *Waiting for speech*, *Posting
captions* (with the time YouTube last accepted one) or *Not posting* with the reason.

Delivery runs without the room page open. A background check every 30 seconds restores a
saved choice after a portal restart (fetching the caption address again), restarts it when
the room is linked to another broadcast, and stops it when the choice is cleared, the room
or event is deleted (a deleted event's rooms are never started again), floor transcription or the chosen translation language is turned off,
or the connection needs signing in again. While captions run, VoxBento asks YouTube about
the broadcast at most every five minutes, so revoked access, an ended broadcast or a stream
rebound in Studio stops delivery within that time (a rebound broadcast stays stopped until it is
chosen again) (sooner when the room page's status poll
finds the error). After YouTube refuses to set a broadcast up, it is asked again after five
minutes. Unlinking the broadcast, switching channel or **Disconnect** turns captions off.

Captions appear in the player after YouTube's own viewer delay. The end-to-end delay on a
real broadcast has not been measured yet; the plan 006 rehearsal records it here.

Accepted links: `https://www.youtube.com/watch?v=<id>`, `https://youtube.com/live/<id>`,
`https://youtu.be/<id>`, `https://studio.youtube.com/video/<id>/livestreaming`
(`m.youtube.com` too) or the bare 11-character video id. Anything else, including
`http://` links and other hosts, is refused.

## Operator setup

Sign in with YouTube is off until both variables below are set.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a
   project (or reuse one) and enable **YouTube Data API v3**.
2. Configure the **OAuth consent screen**: user type *External*, app name, support
   email, and the scope `https://www.googleapis.com/auth/youtube.force-ssl`. While
   the app is in *Testing*, add each organiser's Google account under **Test users**
   (at most 100). This scope is *sensitive*: before more people can use it, submit the
   app for verification with a privacy-policy URL; until then Google shows an
   "unverified app" warning. While the app stays in *Testing*, Google expires its
   refresh tokens after 7 days, so organisers see "Sign in with YouTube again" weekly;
   publish the app to avoid that.
3. Create an **OAuth client ID** of type *Web application* with the authorised
   redirect URI `{PUBLIC_BASE_URL}/youtube/oauth/callback`, for example
   `https://voxbento.com/youtube/oauth/callback`. Register the full callback URI, not
   `PUBLIC_BASE_URL` alone: VoxBento removes any trailing slash from `PUBLIC_BASE_URL`,
   appends `/youtube/oauth/callback`, and Google refuses sign-in with
   `redirect_uri_mismatch` unless the registered URI matches that result exactly.
4. Set the environment variables and restart the portal:

| Variable | Default | Description |
|---|---|---|
| `YOUTUBE_OAUTH_CLIENT_ID` | _(none)_ | Google OAuth web client id. |
| `YOUTUBE_OAUTH_CLIENT_SECRET` | _(none)_ | Its client secret. Production refuses to start if only one of the two is set. |
| `YOUTUBE_STATUS_CACHE_SECS` | `60.0` | How long a room's YouTube status is reused before asking YouTube again. |

`PUBLIC_BASE_URL` must be the public origin people use: it builds the redirect URI and
is the allowed `Origin` for the state-changing routes. `API_KEY_ENCRYPTION_KEY` must be
set, since refresh tokens and stream keys are stored encrypted.

The organiser's channel must have live streaming enabled (YouTube Studio → *Go live*;
the first activation can take up to 24 hours), and a broadcast needs a stream bound
to it in Studio before VoxBento can fill in its key.

### Local development

Create a second OAuth client (or add a redirect URI) for
`http://localhost:8000/youtube/oauth/callback` and run with
`PUBLIC_BASE_URL=http://localhost:8000` and `DEBUG=true`. Debug mode drops the
`Secure` flag from the state cookie so it works over plain `http://localhost`.

## Routes

| Method | Path | Notes |
|---|---|---|
| GET | `/workspace/events/{event_id}/youtube` | `{available, connected, channel_title, channel_id, connected_at, last_error}`; `available: false` when not configured |
| GET | `/workspace/events/{event_id}/youtube/connect?room_id=N` | 303 to Google, sets `vb_yt_oauth` |
| GET | `/youtube/oauth/callback` | not a workspace route; 400 bad/expired state, 403 other user or no ownership |
| GET | `/workspace/events/{event_id}/youtube/broadcasts` | `{broadcasts: [{id, title, scheduled_start, life_cycle_status, latency_preference}]}`, live first; 409 when access was revoked |
| PUT | `/workspace/events/{event_id}/rooms/{room_id}/youtube/broadcast` | `{broadcast}` → `{broadcast_id, title, life_cycle_status, stream_bound, destination_configured}`; 422 bad link / not on channel / no bound stream, 409 sign in again, 502 YouTube unavailable |
| DELETE | `/workspace/events/{event_id}/rooms/{room_id}/youtube/broadcast` | unlinks and turns captions off; a saved relay destination stays until the owner removes it |
| PUT | `/workspace/events/{event_id}/rooms/{room_id}/youtube/captions` | `{language}` (`null` off, `"floor"`, or an enabled translation code) → `{language, state, reason, last_ok_at, choices}`; 409 no connection / no broadcast / broadcast cannot carry captions / sign in again, 422 unusable language, 502 YouTube unavailable |
| DELETE | `/workspace/events/{event_id}/youtube` | forgets the channel and every room's broadcast; Google revocation is best effort and skipped for a shared channel (see [Revoking access](#revoking-access)) |

The workspace routes are event-owner only (`require_event_owner`) and return 404 while the
feature is off (except the status route). Their JSON responses, and the callback's redirects
back to the room page, answer `Cache-Control: no-store`; the callback's error responses
(400 expired or mismatched state, 403 another user or no ownership) are the portal's
standard error pages, which do not set that header and carry no token or code. PUT and DELETE check `Origin` against
`PUBLIC_BASE_URL` and require the admin CSRF token (`X-CSRF-Token`, `require_admin_csrf`),
which the room page script reads from the page's `csrf-token` meta tag.

## Data stored

- `youtube_connections` (one row per event): channel id and title, the
  Fernet-encrypted refresh token, granted scope, who connected it and when
  (`connected_at`), the last sign-in-again error and timestamps.
- `rooms.youtube_broadcast_id`, `rooms.youtube_stream_id`: the room's broadcast and
  the stream bound to it.
- `rooms.youtube_captions_language` (migration 048): `NULL` (off), `floor` or a
  translation language code. The caption address itself is never stored.

Access tokens live only in the portal's memory (one portal replica), are refreshed
when they have under a minute left, and are dropped on reconnect or disconnect. A
cached token is tied to the stored refresh token, so it is never reused after a
reconnect. Linking, reconnecting and disconnecting are serialized per event.
Tokens, stream keys, caption URLs, authorization codes and Google response bodies are
never logged or returned; log lines carry ids and an action only. The access log
redacts `code=` on `/youtube/oauth/callback` because the path contains `/oauth/`.

## Quota

Listing broadcasts costs two `liveBroadcasts.list` calls (more only for a channel with
over 50 active or upcoming broadcasts; at most four pages per status); linking costs one
`liveBroadcasts.list` and one `liveStreams.list`. Turning captions on costs one
`liveBroadcasts.list`, one `liveBroadcasts.update` when the broadcast is not yet set up, and
one `liveStreams.list`; a running caption target is re-checked with one `liveBroadcasts.list`
every five minutes. Posting captions does not use the API quota. While a room page is open, each room's
status is cached for `YOUTUBE_STATUS_CACHE_SECS` (60 s by default); a refresh costs one
`liveBroadcasts.list` call, plus one `liveStreams.list` call only when the broadcast is ready
or (about to go) live. The cache is a duration, not a hard rate limit: signing in again,
unlinking or disconnecting clears it, so the next status check can ask YouTube
before the interval is up. Linking a different broadcast also skips the cached status. After Google reports the project's quota or rate limit, the room shows
that message and VoxBento stops asking for five minutes. List calls are cheap reads (see
Google's quota calculator) against the project's daily quota, 10,000 units by default.

## Revoking access

**Disconnect** on the room page asks Google to revoke VoxBento's access and forgets the
channel for the event. When another event is connected to the same channel the token
is not revoked, because Google revokes the whole grant. Revocation is best effort: if
Google cannot be reached or refuses, VoxBento still forgets the channel, so check
[myaccount.google.com/permissions](https://myaccount.google.com/permissions) when access
must be gone.

Disconnect keeps each room's saved stream key, so a relay that is running keeps sending
to YouTube. To stop it, click **Remove** next to the stream key in the room's relay
settings, or switch the room to another audio source. Stopping the relay is retried every
few seconds until the relay service confirms it, so if the relay service is unreachable
forwarding can continue briefly after the key is removed. If YouTube Studio still shows the
stream receiving data after a minute, ask the operator to check the `program-relay` service.

Organisers can also remove access at any time at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions); VoxBento then
shows **Sign in with YouTube again** on the next YouTube request (also when the stored token
can no longer be decrypted, e.g. after an `API_KEY_ENCRYPTION_KEY` change). Signing in again
keeps the rooms' broadcasts; Disconnect clears them.

If the organiser resets the stream key in YouTube Studio the relay fails; choosing the
broadcast again (**Use this broadcast**) fetches the new key.
