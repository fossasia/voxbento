# WebRTC Ingest

This document describes the WebRTC ingest design: how the interpreter's microphone audio gets from the browser to the server-side aiortc endpoint, and how the SDP negotiation works.

---

## Overview

The ingest path uses the standard WebRTC offer/answer exchange:

1. **Browser** captures mic audio via `getUserMedia`.
2. **Browser** creates a `RTCPeerConnection`, adds the audio track, and generates an SDP offer.
3. **Browser** POSTs the offer to `POST /api/interpreter/connect/{channel_id}`.
4. **Server** (`portal/ingest.py`) creates a server-side `aiortc.RTCPeerConnection`, sets the offer as the remote description, creates an answer, and returns it.
5. **Browser** sets the answer as its remote description.
6. ICE candidates embedded in the SDP allow the connection to establish.
7. **RTP Opus stream** flows from browser to server.
8. Server hands the audio to `aiortc.contrib.media.MediaRecorder` → FFmpeg → HLS.

---

## SDP offer construction (browser)

`MicStreamingManager.createIngestConnection(onConnectionStateChange)`:

```js
this.peerConnection = new RTCPeerConnection({
  iceServers: createIceServers(this.stunServers)   // from VITE_STUN_SERVERS env var
})

for (const track of this.stream.getAudioTracks()) {
  this.peerConnection.addTrack(track, this.stream)
}

const offer = await this.peerConnection.createOffer({
  offerToReceiveAudio: false,   // send-only
  offerToReceiveVideo: false
})

await this.peerConnection.setLocalDescription(offer)
await waitForIceGathering(this.peerConnection)   // 3 s timeout

return this.peerConnection.localDescription
```

Key points:
- The offer is audio-only and send-only.
- ICE gathering runs for up to 3 seconds. Any candidates gathered are embedded in the SDP before it is sent (vanilla ICE / non-trickle). This avoids needing a separate ICE candidate signalling channel.
- `offerToReceiveAudio: false` prevents the server from sending audio back to the browser (which would create a loopback risk).

---

## Ingest API endpoint

`POST /api/interpreter/connect/{channel_id}`

Request body (JSON):

```json
{
  "type": "offer",
  "sdp": "v=0\r\no=- ...",
  "booth_id": "hall-a-fr",
  "participant_id": "abc123",
  "token": "optional-token",
  "language": "French"
}
```

Authorization checks (in order):

1. `require_access_token(token)` — validates against `BOOTH_ACCESS_TOKEN` if set.
2. `booths.is_active_interpreter(booth_id, participant_id, language, channel_id)` — only the active interpreter may publish.

If either check fails, the endpoint returns `403`. The browser surfaces this as an ingest error.

Response body (JSON):

```json
{
  "type": "answer",
  "sdp": "v=0\r\no=- ..."
}
```

The browser also accepts a Janus-style wrapper `{ jsep: { type, sdp } }` for compatibility with Janus-based ingest backends (handled by `normalizeAnswerPayload` in `ingestClient.js`).

---

## Server-side SDP handling (aiortc)

`IngestService._connect()` in `portal/ingest.py`:

```python
await peer_connection.setRemoteDescription(
    RTCSessionDescription(sdp=offer_sdp, type=offer_type)
)
answer = await peer_connection.createAnswer()
await peer_connection.setLocalDescription(answer)
await self._wait_for_ice_completion(peer_connection)
```

After the connection is established:

```python
@peer_connection.on('track')
async def on_track(track):
    if track.kind != 'audio':
        return
    recorder.addTrack(track)
    if not session.recorder_started:
        await recorder.start()
        session.recorder_started = True
```

The recorder is an `aiortc.contrib.media.MediaRecorder` pointed at the HLS output path. It uses FFmpeg under the hood to transcode the incoming Opus/RTP audio and segment it into HLS.

---

## Async runtime

aiortc is an asyncio library. The Flask server uses threading (not asyncio). The `AsyncRuntime` class bridges these:

```python
class AsyncRuntime:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = Thread(target=self._run_forever, daemon=True)
        self.thread.start()

    def run(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result()   # blocks the calling thread until done
```

All aiortc operations (`_connect`, `_disconnect`) run on this dedicated event loop. The Flask request thread blocks on `future.result()` during SDP negotiation (typically < 2 s).

---

## One session per channel

The `IngestService.sessions` dict is keyed by `channel_id`. When a new offer arrives for a channel that already has an active session, the old session is disconnected first:

```python
async def _connect(self, *, channel_id, ...):
    await self._disconnect(channel_id)   # clean up any existing session
    ...
```

This ensures that when a coordinator performs a handoff and the new active interpreter goes live, the previous interpreter's ingest connection is terminated server-side.

---

## HLS output

The `MediaRecorder` writes to `INGEST_HLS_ROOT/{channel_id}/playlist.m3u8`. FFmpeg creates the directory if it does not exist.

HLS parameters (configurable via `.env`):

| Variable | Default | Effect |
|---|---|---|
| `HLS_SEGMENT_SECONDS` | `2` | Target segment duration |
| `HLS_PLAYLIST_LENGTH` | `8` | Number of segments in the live playlist |

At 2 s segments with 8 in the playlist, the live edge latency is approximately 16 s. This can be tuned for lower latency (at the cost of increased segment overhead) or higher stability (at the cost of latency).

---

## STUN server configuration

The default configuration does not require a TURN server for local development (browser and server are on the same machine or LAN). For production:

- Configure `VITE_STUN_SERVERS` with a reliable STUN server list.
- If the ingest server is behind a NAT that does not support hairpinning, a TURN server is required. Configure it alongside STUN in the `iceServers` array.

The `createIceServers` helper in `micStreamingManager.js` maps the `VITE_STUN_SERVERS` array to the `RTCIceServer` format.

---

## Ingest availability flag

`AIORTC_AVAILABLE` in `portal/ingest.py` reflects whether `aiortc` is importable. It is exposed via:

- `GET /healthz` response: `{ "ok": true, "aiortc_available": true }`
- `GET /api/interpreter/status/{channel_id}` response: `{ "reachable": true, "state": "..." }`
- Template variable `aiortc_available` passed to the booth page

When `AIORTC_AVAILABLE` is `False` (aiortc not installed), the ingest endpoint returns `503` and the Vue console shows an appropriate warning. All other booth features (monitoring, chat, participant grid) continue to work.

---

## Future: Janus ingest backend

The `normalizeAnswerPayload` function in `ingestClient.js` already handles the Janus `jsep` wrapper format. Swapping the ingest backend from aiortc to a Janus WebRTC gateway requires only:

1. Changing the `VITE_INGEST_BASE_URL` to point to the Janus endpoint.
2. Ensuring the Janus room/plugin returns a compatible SDP answer (with or without the `jsep` wrapper).

No changes to the browser-side WebRTC or SDP offer logic are needed.
