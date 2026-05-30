# Interpreter Audio Flow

This document traces the complete audio path from the interpreter's microphone to the listener's HLS player.

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
       WHIP POST to MediaMTX :8889/{channel_id}
       Body: SDP offer (application/sdp)
       │
       ▼  (MediaMTX handles WebRTC termination)
       MediaMTX accepts WHIP session
       │
       ▼
       SDP answer returned (201 Created, application/sdp)
       │
       ▼  (back in browser)
       peerConnection.setRemoteDescription(answer)
       │
       ▼
       ICE candidate exchange (embedded in SDP)
       │
       ▼
       RTP stream (Opus codec) → MediaMTX
       │
       ▼
       MediaMTX transcodes and segments to HLS
       │
       ▼
       HLS available at MediaMTX :8888/{channel_id}/playlist.m3u8
       │
       ▼
       Listener page /listen/{booth_id}: hls.js player with auto-recovery
       │
       ▼
       Eventyay viewer: Hidden HLS audio player (drift-corrected against YouTube clock)
```

---

## Browser-side steps in detail

### Step 1 — Device selection (preflight)

`static/js/interpreter-booth.js` calls `navigator.mediaDevices.enumerateDevices()` and filters for `audioinput` devices. The user selects their headset microphone from a dropdown in the mic ingest panel.

### Step 2 — Mic test

1. Calls `getUserMedia` with the selected device and DSP flags.
2. Creates an `AudioContext` with an `AnalyserNode`.
3. Connects the `MediaStreamSource` to the `AnalyserNode` only — never to `destination`. This prevents loopback.
4. Runs a `requestAnimationFrame` loop computing RMS level from the time-domain data and updating the level meter.
5. The mic ingest panel renders the level as a visual meter bar.

The mic test completes when the interpreter confirms they can see the level responding to their voice. The `preflight.micTestComplete` flag is set.

### Step 3 — Ingest connection (Go Live)

1. Creates a new `RTCPeerConnection` with configured STUN servers.
2. Adds all audio tracks from the active `MediaStream`.
3. Creates an offer with `offerToReceiveAudio: false, offerToReceiveVideo: false` (send-only).
4. Sets the local description and waits for ICE gathering to complete (max 3 s).
5. POSTs the SDP offer to the MediaMTX WHIP endpoint at `:8889/{channel_id}` with `Content-Type: application/sdp`.
6. MediaMTX returns a `201 Created` response with the SDP answer in the body.
7. Sets `peerConnection.setRemoteDescription(answer)` to complete the connection.

### Step 4 — Live streaming

Once the peer connection state transitions to `connected`, the audio track flows as an Opus RTP stream directly to MediaMTX. The booth health panel shows live status.

A stats timer polls `RTCPeerConnection.getStats()` every 2 s to compute an approximate bitrate in kbps, which is surfaced in the health panel.

### Step 5 — Stop / handoff

Closing the peer connection stops the WHIP session. MediaMTX detects the disconnection and stops writing HLS segments.

When a handoff occurs (active interpreter changes), `overridePublisher` is enabled on MediaMTX, allowing the new active interpreter to publish to the same channel path, replacing the previous publisher.

---

## MediaMTX ingest details

MediaMTX handles the entire audio pipeline. Python never touches audio data.

### WHIP session lifecycle

- **Publish:** Browser POSTs SDP offer to `http://mediamtx:8889/{channel_id}` via WHIP protocol.
- **Receive:** MediaMTX accepts the WebRTC session, receives Opus RTP, transcodes to HLS.
- **HLS output:** Available at `http://mediamtx:8888/{channel_id}/playlist.m3u8`.
- **Handoff:** `overridePublisher: yes` in MediaMTX config allows a new publisher to replace the current one on the same path without requiring explicit disconnection.

### Connection state monitoring

The browser monitors `RTCPeerConnection.connectionState`. If the state transitions to `failed` or `closed`, the booth health panel shows a warning and the interpreter can retry by clicking **Go Live** again.

---

## Echo prevention

The primary echo prevention mechanism is **operational**: interpreters must use headphones. The portal enforces this via the preflight checklist (`headphonesConnected` item must be checked before Go Live is enabled).

The secondary mechanism is **technical**: browser mic capture enables `echoCancellation: true`. The browser's built-in AEC (Acoustic Echo Cancellation) will suppress any residual floor audio leaking from speakers, but headphones are required to make AEC reliable.

The tertiary guarantee: the analyser node used for the level meter is **never connected to `AudioContext.destination`**, so there is no programmatic loopback path in the browser code.

---

## Failure and reconnect behaviour

| Failure | Browser behaviour | Server behaviour |
|---|---|---|
| ICE negotiation timeout | Show warning; retry up to `MAX_RECONNECT_ATTEMPTS` | N/A — MediaMTX waits for new WHIP session |
| WebRTC `connectionState === 'failed'` | Show warning; auto-retry | MediaMTX cleans up stale session |
| Coordinator reassigns active role | Stop ingest; clear mic state | New active interpreter can publish via `overridePublisher` |
| Interpreter leaves booth | Stop ingest | WebSocket handler broadcasts updated state |

Viewer fallback: if the HLS playlist stops updating, the Eventyay viewer player falls back to the original floor audio.
