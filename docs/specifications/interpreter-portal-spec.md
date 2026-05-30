# Interpreter Portal Specification

This document defines the functional specification for the interpreter console — what the interpreter sees, what they can do, and what guardrails are in place.

---

## Overview

The interpreter console is a single browser tab that combines:

1. A **Jitsi monitoring panel** — the interpreter hears and sees the floor session.
2. A **mic ingest panel** — the interpreter tests their mic and goes live.
3. A **preflight checklist** — gates the Go Live button until all requirements are confirmed.
4. A **participant grid** — shows who is in the booth and their current role/status.
5. A **booth chat panel** — internal text communication between booth participants.
6. A **booth health panel** — live ingest status and stream health indicators.

The interpreter never leaves this tab. Jitsi monitoring and live translation are both driven from this single surface.

---

## Booth URL format

```
/interpreter/<booth_id>?token=<token>&language=<language>&channel=<channel_id>
```

| Parameter | Required | Description |
|---|---|---|
| `booth_id` | Yes (path) | Unique identifier for the booth (e.g., `hall-a-fr`) |
| `token` | If `BOOTH_ACCESS_TOKEN` is set | Temporary invite token from the organizer |
| `language` | No (default: `English`) | Human-readable language name for display |
| `channel` | No (default: `{booth_id}-audio`) | HLS channel identifier |

The organizer generates one URL per language per session and distributes it to the booth team. The URL encodes the booth assignment; the interpreter does not need to configure anything.

---

## Preflight checklist

The **Go Live** button is disabled until all four items are completed. This is enforced in `PreflightChecklist` and the booth JavaScript.

| Item | How it is satisfied |
|---|---|
| Headphones connected | Interpreter manually checks the checkbox |
| Monitoring active | Set automatically when the Jitsi iframe loads |
| Mic test complete | Set after interpreter clicks **Test Mic** and confirms level |
| Ingest reachable | Set after WHIP endpoint (MediaMTX :8889) is confirmed reachable |

The checklist prevents an interpreter from accidentally going live without headphones (echo risk) or without confirming their mic is working.

### Mic test flow

1. Interpreter clicks **Test Mic**.
2. `MicStreamingManager.startMicrophone(deviceId, onLevel)` is called.
3. The level meter bar begins animating.
4. The interpreter speaks into their mic and observes the level.
5. The interpreter confirms the level is responding (via a separate confirm action or by the system detecting a non-zero level above a threshold).
6. `preflight.micTestComplete` is set to `true`.

The mic test does **not** send any audio to the server. It only exercises the `getUserMedia` and `AudioContext` paths locally.

---

## Go Live / Stop

### Go Live

Prerequisites:
- All four preflight items are complete.
- The local participant is the active interpreter for the channel.
- No existing ingest session is active for this participant.

Sequence:
1. Browser creates `RTCPeerConnection` and captures audio track.
2. SDP offer is formatted as a WHIP POST to `MediaMTX :8889/{channel_id}`.
3. MediaMTX responds with an SDP answer.
4. WebRTC connection is established; audio flows to MediaMTX.
5. State transitions: `ingest.status = 'connecting'` → `'connected'`.
6. WebSocket `booth:update-state` emitted to server with `mic_active: true, ingest_connected: true`.
7. Server broadcasts updated `booth:state` to all booth participants.

The **Go Live** button is replaced by a **Stop** button while live.

### Stop

1. Close the WebRTC peer connection (ends the WHIP session in MediaMTX).
2. State transitions: `ingest.status = 'disconnected'`, `ingest.streamingLive = false`.
3. WebSocket `booth:update-state` emitted with `mic_active: false, ingest_connected: false`.

---

## Device selection

The `MicIngestPanel` shows a `<select>` element populated by `navigator.mediaDevices.enumerateDevices()`. The interpreter can switch devices at any time. Switching a device:

1. Stops the current mic stream and level meter.
2. Calls `getUserMedia` with the new `deviceId`.
3. If live, re-negotiates the ingest connection with the new track.

---

## Auto-reconnect

If the WebRTC connection drops while the interpreter is live:

1. `onConnectionStateChange` fires with `'disconnected'` or `'failed'`.
2. `ingest.reconnecting` is set to `true` and a warning is shown in the console.
3. After a brief delay, the system attempts to re-negotiate (up to `MAX_RECONNECT_ATTEMPTS = 5`).
4. If reconnect succeeds, `ingest.reconnecting` is cleared.
5. If all retries are exhausted, the interpreter is shown an error and must manually click **Go Live** again.

On the viewer side: the HLS playlist stops updating. The viewer player falls back to the original floor audio automatically (this is handled by the Eventyay video module, not this portal).

---

## MediaMTX unavailability

If MediaMTX is not running (e.g., during frontend-only development), WHIP POST requests will fail with a connection error. In this case:

- `GET /healthz` still returns `ok: true` (the portal itself is healthy).
- The Go Live button is available but WHIP publishing will fail.
- The console shows a connection error when the interpreter tries to go live.
- All other features (Jitsi monitoring, booth chat, participant grid) continue to work.

This allows booth coordination development to proceed without a running MediaMTX instance.

---

## Layout and responsiveness

The console uses a CSS Grid layout:

```
┌─────────────────────────────────────────────────────┐
│  Topbar (brand + session metadata)                  │
├──────────────────────────┬──────────────────────────┤
│  JitsiMonitorPanel (2fr) │  Sidebar (1fr, min 300px)│
│                          │  ┌──────────────────────┐│
│                          │  │ MicIngestPanel       ││
│                          │  ├──────────────────────┤│
│                          │  │ PreflightChecklist   ││
│                          │  ├──────────────────────┤│
│                          │  │ BoothHealthPanel     ││
│                          │  └──────────────────────┘│
├──────────────────────────┴──────────────────────────┤
│  ParticipantGrid                                     │
├──────────────────────────────────────────────────────┤
│  BoothChatPanel                                      │
└──────────────────────────────────────────────────────┘
```

Below 1080px viewport width, the layout collapses to a single column (Jitsi panel stacked above the sidebar).

---

## Accessibility considerations

- All interactive controls (buttons, selects, checkboxes) use semantic HTML elements.
- The level meter is a `<progress>` or `<div>` with `aria-valuenow` / `aria-valuemax` attributes.
- Role badges in the participant grid use `aria-label` to describe the role.
- The booth chat panel traps focus correctly when typing.

---

## Security considerations

- The booth URL token is sent as a query parameter on initial page load and included in all subsequent API calls and WebSocket messages.
- The token is never stored in `localStorage` or `IndexedDB`.
- All API calls use HTTPS in production (required for `getUserMedia`).
- The ingest endpoint validates both the token and the active interpreter status before accepting an SDP offer, preventing unauthorized publishing.
