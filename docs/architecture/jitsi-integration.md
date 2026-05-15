# Jitsi Integration

This document describes how Jitsi is used in the interpretation portal and what it is explicitly not used for.

---

## Purpose

Jitsi provides the **monitoring path** for interpreters. When an interpreter opens their booth:

1. They see the floor session (speaker audio and video) via an embedded Jitsi iframe.
2. They can hear their backup interpreter or coordinator if those colleagues also have Jitsi open.
3. They have a visual context of who is speaking before they begin interpreting.

Jitsi is **not** the ingest transport. Interpreter audio never flows through Jitsi to viewers. The ingest path is a separate WebRTC connection directly to the portal's ingest API.

---

## Embed configuration

The Jitsi iframe is configured to start in receive-only mode. This is enforced by URL hash parameters set in `src/services/jitsiEmbed.js`:

```js
const JITSI_EMBED_CONFIG = {
  'config.startWithAudioMuted': 'true',
  'config.startWithVideoMuted': 'true',
  'config.prejoinPageEnabled': 'false',
  'config.disableInitialGUM': 'false',
  'config.startSilent': 'true'   // start muted
}
```

These parameters are appended as URL hash params when constructing the embed URL:

```
https://meet.jit.si/eventyay-stage-room
  #config.startWithAudioMuted=true
  &config.startWithVideoMuted=true
  &config.prejoinPageEnabled=false
  &config.disableInitialGUM=true
  &config.startSilent=true
```

The result: the Jitsi frame loads, the interpreter can hear floor audio, but their microphone and camera are not published into the Jitsi call by default.

---

## URL validation (`buildJitsiEmbedUrl`)

`buildJitsiEmbedUrl(rawUrl, options)` in `src/services/jitsiEmbed.js`:

1. Parses the raw Jitsi URL with `new URL(rawUrl)`.
2. Validates that the URL has a room path (not just `/`).
3. If `options.expectedDomain` is set, validates that the URL's origin matches `https://{expectedDomain}`.
4. Returns `{ roomName, embedUrl }`.

The `expectedDomain` check is wired to the `JITSI_DOMAIN` environment variable passed from the Flask server into the Vue config. This prevents an interpreter from substituting an arbitrary Jitsi server.

---

## How the interpreter joins

The `JitsiMonitorPanel` renders:

- A text input for the Jitsi room URL (pre-filled from `DEFAULT_JITSI_ROOM`)
- A **Join Monitor Room** button
- The Jitsi iframe (hidden until joined)

On join:

```js
// useInterpreterBooth.js
function joinJitsi(rawUrl) {
  const { roomName, embedUrl } = buildJitsiEmbedUrl(rawUrl, {
    expectedDomain: env.jitsiDomain
  })
  state.jitsi.roomName = roomName
  state.jitsi.embedUrl = embedUrl
  state.jitsi.status = 'joined'
}
```

The `embedUrl` is set as the `src` attribute of the `<iframe>`. The Jitsi JS API is not used — the portal interacts with Jitsi solely via iframe embed.

---

## Why no Jitsi JS API

The Jitsi JS API (`JitsiMeetExternalAPI`) would allow programmatic control (mute/unmute, participant events, etc.). The portal deliberately does not use it because:

1. **Scope separation.** The portal does not need to control the Jitsi call. It only needs to display it.
2. **Dependency reduction.** Loading the Jitsi external API JS adds a large dependency and cross-origin complexity.
3. **Robustness.** A pure iframe embed degrades gracefully if the Jitsi server is momentarily unreachable — the rest of the console (ingest, chat, participant grid) continues to work.

If future requirements need Jitsi API features (e.g., detecting when the speaker is muted, or auto-joining with a display name), the `jitsiEmbed.js` service is the right place to extend.

---

## Jitsi server configuration

The portal works with any Jitsi server. The default is `meet.jit.si` (the public Jitsi instance). For production events:

- Deploy a dedicated Jitsi server or use a managed Jitsi service.
- Set `DEFAULT_JITSI_ROOM` and `JITSI_DOMAIN` in `.env` to point to it.
- Ensure the Jitsi server has appropriate capacity for the expected number of participants.

There is no Jitsi configuration managed by this portal. Room creation, access control, and capacity management are handled by whoever operates the Jitsi server.

---

## Echo risk and headphone requirement

The monitoring path (Jitsi floor audio playing from the interpreter's speakers) would create acoustic echo if the interpreter does not use headphones. The portal enforces headphone use via the preflight checklist.

The `headphonesConnected` item in `PreflightChecklist` must be checked before the **Go Live** button is enabled. This is an operational control, not a technical detection. The portal trusts the interpreter to self-report headphone use.

Browser echo cancellation (`echoCancellation: true` in `getUserMedia`) provides a fallback if headphones are partially insufficient, but it cannot eliminate severe echo from open speakers.

---

## Jitsi in the booth vs. ingest paths

| Attribute | Jitsi (monitoring) | WebRTC ingest |
|---|---|---|
| Direction | Receive only | Send only (mic upload) |
| Transport | Jitsi's own WebRTC stack | Direct RTCPeerConnection to portal API |
| Audio source | Floor session (speakers, presenter) | Interpreter's microphone |
| Destination | Interpreter's ears | aiortc → FFmpeg → HLS → viewer |
| Echo risk | High (if no headphones) | None (never routed to speakers) |
| Failure impact | Interpreter loses monitoring | Viewer loses language audio |
