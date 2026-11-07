# Sign in with YouTube (#1087)

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
   with a *different* channel clears every room's broadcast and the relay destination
   that was filled in from the old channel's stream.
3. **Use this broadcast** (`PUT …/rooms/{room_id}/youtube/broadcast`) accepts a
   broadcast from the list or a pasted link. VoxBento checks the broadcast is on the
   connected channel and has a stream bound in YouTube Studio, then saves it on the
   room. In relay mode it also stores the bound stream's ingest address and key as
   the relay destination (encrypted, write-only, exactly like a pasted key). If relay
   mode is switched on after the broadcast was linked, the room page links it once
   more on its next status check, so the key is filled in then.
4. The room status (`GET …/program-ingest`) adds a `youtube` block with the
   broadcast's life-cycle status, stream status and health, cached for 30 s per room.

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
   `https://voxbento.com/youtube/oauth/callback`. It must match `PUBLIC_BASE_URL`
   exactly.
4. Set the environment variables and restart the portal:

| Variable | Default | Description |
|---|---|---|
| `YOUTUBE_OAUTH_CLIENT_ID` | _(none)_ | Google OAuth web client id. |
| `YOUTUBE_OAUTH_CLIENT_SECRET` | _(none)_ | Its client secret. Production refuses to start if only one of the two is set. |
| `YOUTUBE_STATUS_CACHE_SECS` | `30.0` | How long a room's YouTube status is reused before asking YouTube again. |

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
| DELETE | `/workspace/events/{event_id}/rooms/{room_id}/youtube/broadcast` | unlinks; a saved relay destination stays until the owner removes it |
| DELETE | `/workspace/events/{event_id}/youtube` | revokes the token at Google, forgets the channel and every room's broadcast |

All routes are event-owner only (`require_event_owner`), answer `Cache-Control: no-store`,
and return 404 while the feature is off (except the status route). PUT and DELETE check
`Origin` against `PUBLIC_BASE_URL`.

## Data stored

- `youtube_connections` (one row per event): channel id and title, the
  Fernet-encrypted refresh token, granted scope, who connected it, the last
  sign-in-again error and timestamps.
- `rooms.youtube_broadcast_id`, `rooms.youtube_stream_id`: the room's broadcast and
  the stream bound to it.

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
`liveBroadcasts.list` and one `liveStreams.list`. The room status adds the same two
calls at most once per `YOUTUBE_STATUS_CACHE_SECS` per room, and only while the room
page is open. List calls are cheap reads (see Google's quota calculator) against the
project's daily quota, 10,000 units by default.

## Revoking access

**Disconnect** on the room page revokes VoxBento's access at Google and forgets the
channel for the event. When another event is connected to the same channel the token
is not revoked, because Google revokes the whole grant. Organisers can also remove
access at any time at [myaccount.google.com/permissions](https://myaccount.google.com/permissions);
VoxBento then shows **Sign in with YouTube again** on the next YouTube request (also
when the stored token can no longer be decrypted, e.g. after an `API_KEY_ENCRYPTION_KEY`
change). Signing in again keeps the rooms' broadcasts; Disconnect clears them.

If the organiser resets the stream key in YouTube Studio the relay fails; choosing the
broadcast again (**Use this broadcast**) fetches the new key.
