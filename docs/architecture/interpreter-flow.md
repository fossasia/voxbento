# Interpreter Audio Flow

This document traces the complete audio path from the interpreter's microphone to the viewer's HLS player.

---

## End-to-end flow diagram

```
Interpreter microphone (physical headset mic)
│
▼
navigator.mediaDevices.getUserMedia({
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
    deviceId: { exact: selectedDeviceId }   ← user-selected in preflight
  }
})
│
▼
MediaStream (audio-only; never connected to AudioContext.destination)
│
├──► AudioContext analyser node  ← level meter (visualisation only; no output)
│
└──► RTCPeerConnection.addTrack(audioTrack, stream)
       │
       ▼
       peerConnection.createOffer({ offerToReceiveAudio: false, offerToReceiveVideo: false })
       │
       ▼
       peerConnection.setLocalDescription(offer)
       │
       ▼
       ICE gathering (3 s timeout)
       │
       ▼
       POST /api/interpreter/connect/{channel_id}
       Body: { type, sdp, booth_id, participant_id, token, language }
       │
       ▼  (server-side: portal/ingest.py)
       aiortc RTCPeerConnection.setRemoteDescription(offer)
       │
       ▼
       aiortc RTCPeerConnection.createAnswer()
       │
       ▼
       ICE gathering (server-side)
       │
       ▼
       JSON response: { type: "answer", sdp: "..." }
       │
       ▼  (back in browser)
       peerConnection.setRemoteDescription(answer)
       │
       ▼
       ICE candidate exchange (embedded in SDP / trickle ICE)
       │
       ▼
       RTP stream (Opus codec) → server-side aiortc peer connection
       │
       ▼
       aiortc @peer_connection.on('track')
         → recorder.addTrack(track)
         → recorder.start()
       │
       ▼
       MediaRecorder → FFmpeg pipe or HLS file recorder
       │
       ▼
       hls-output/{channel_id}/playlist.m3u8
       hls-output/{channel_id}/segment-XXXX.ts
       │
       ▼
       HLS origin / CDN
       │
       ▼
       Eventyay viewer: Hidden HLS audio player (drift-corrected against YouTube clock)
```

---

## Browser-side steps in detail

### Step 1 — Device selection (preflight)

`MicStreamingManager.listInputDevices()` calls `navigator.mediaDevices.enumerateDevices()` and filters for `audioinput` devices. The user selects their headset microphone from a dropdown in the `MicIngestPanel`.

### Step 2 — Mic test

`MicStreamingManager.startMicrophone(deviceId, onLevel)`:

1. Calls `getUserMedia` with the selected device and DSP flags.
2. Creates an `AudioContext` with an `AnalyserNode`.
3. Connects the `MediaStreamSource` to the `AnalyserNode` only — never to `destination`. This prevents loopback.
4. Runs a `requestAnimationFrame` loop computing RMS level from the time-domain data and calling `onLevel(level)`.
5. The `MicIngestPanel` renders the level as a visual meter bar.

The mic test completes when the interpreter confirms they can see the level responding to their voice. The `preflight.micTestComplete` flag is set.

### Step 3 — Ingest connection (Go Live)

`MicStreamingManager.createIngestConnection(onConnectionStateChange)`:

1. Creates a new `RTCPeerConnection` with configured STUN servers.
2. Adds all audio tracks from the active `MediaStream`.
3. Creates an offer with `offerToReceiveAudio: false, offerToReceiveVideo: false` (send-only).
4. Sets the local description and waits for ICE gathering to complete (max 3 s).
5. Returns the local description (offer).

`IngestClient.negotiate(channelId, localDescription)`:

1. POSTs the SDP offer to `POST /api/interpreter/connect/{channel_id}`.
2. Expects a JSON response with `type` and `sdp` fields (or a Janus-style `jsep` wrapper).
3. Returns the answer SDP.

Back in `MicStreamingManager`: calls `peerConnection.setRemoteDescription(answer)` to complete the connection.

### Step 4 — Live streaming

Once the peer connection state transitions to `connected`, the audio track flows as an Opus RTP stream to the server. The `BoothHealthPanel` shows live status.

The `statsTimer` polls `RTCPeerConnection.getStats()` every 2 s to compute an approximate bitrate in kbps, which is surfaced in the health panel.

### Step 5 — Stop / handoff

`MicStreamingManager.stopPeerConnection()` closes the peer connection and removes event listeners.

`IngestClient` calls `POST /api/interpreter/disconnect/{channel_id}` to signal the server.

The server calls `ingest.disconnect(channel_id)` which closes the aiortc peer connection and stops the recorder.

---

## Server-side ingest in detail (`portal/ingest.py`)

The `IngestService` manages a dedicated asyncio event loop on a background thread (`AsyncRuntime`). All aiortc operations run on this loop to avoid blocking Flask's threading model.

### Connection sequence

```python
await peer_connection.setRemoteDescription(offer)
answer = await peer_connection.createAnswer()
await peer_connection.setLocalDescription(answer)
await _wait_for_ice_completion(peer_connection)    # polls gatheringstate
```

The `@peer_connection.on('track')` handler:

```python
async def on_track(track):
    if track.kind != 'audio':
        return
    recorder.addTrack(track)
    await recorder.start()
```

The recorder is an `aiortc.contrib.media.MediaRecorder` pointed at the HLS output path. It transcodes the incoming Opus track with FFmpeg and segments it into `.ts` files with a `.m3u8` playlist.

### Connection state monitoring

```python
@peer_connection.on('connectionstatechange')
async def on_connectionstatechange():
    session.connection_state = peer_connection.connectionState
    if peer_connection.connectionState in {'failed', 'closed'}:
        await _disconnect(channel_id)
```

When the browser disconnects or the peer connection fails, the server cleans up the session automatically.

---

## Echo prevention

The primary echo prevention mechanism is **operational**: interpreters must use headphones. The portal enforces this via the preflight checklist (`headphonesConnected` item must be checked before Go Live is enabled).

The secondary mechanism is **technical**: browser mic capture enables `echoCancellation: true`. The browser's built-in AEC (Acoustic Echo Cancellation) will suppress any residual floor audio leaking from speakers, but headphones are required to make AEC reliable.

The tertiary guarantee: the analyser node used for the level meter is **never connected to `AudioContext.destination`**, so there is no programmatic loopback path in the browser code.

---

## Failure and reconnect behaviour

| Failure | Browser behaviour | Server behaviour |
|---|---|---|
| ICE negotiation timeout | Show warning; retry up to `MAX_RECONNECT_ATTEMPTS` | N/A — server waits for new offer |
| WebRTC `connectionState === 'failed'` | Show warning; auto-retry | Session cleaned up via `connectionstatechange` handler |
| Coordinator reassigns active role | Stop ingest; clear mic state | `ingest.disconnect(channel_id)` called by Socket.IO handler |
| Interpreter leaves booth | Stop ingest | `leave_participant` + `ingest.disconnect` |

Viewer fallback: if the HLS playlist stops updating, the Eventyay viewer player falls back to the original floor audio.
