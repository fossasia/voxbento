# Jitsi Integration

This document describes how Jitsi is used in the interpretation portal and what it is explicitly not used for.

---

## Purpose

Jitsi provides the **monitoring path** for interpreters. When an interpreter opens their booth:

1. They see the floor session (speaker audio and video) via an embedded Jitsi iframe.
2. They can hear their backup interpreter or coordinator if those colleagues also have Jitsi open.
3. They have a visual context of who is speaking before they begin interpreting.

Jitsi is **not** the ingest transport. Interpreter audio never flows through Jitsi to viewers. The ingest path is a separate WHIP connection directly to MediaMTX.

---

## Embed configuration

The Jitsi iframe is configured to start in receive-only mode. This is enforced by URL hash parameters set in `static/js/interpreter-booth.js`:

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
https://jitsi.example.com:8443/eventyay-stage-room
  #config.startWithAudioMuted=true
  &config.startWithVideoMuted=true
  &config.prejoinPageEnabled=false
  &config.disableInitialGUM=true
  &config.startSilent=true
```

The result: the Jitsi frame loads, the interpreter can hear floor audio, but their microphone and camera are not published into the Jitsi call by default.

---

## URL validation

`static/js/interpreter-booth.js` validates that the Jitsi URL entered by the interpreter uses the configured `JITSI_DOMAIN`:

1. Parses the raw Jitsi URL with `new URL(rawUrl)`.
2. Validates that the URL has a room path (not just `/`).
3. If `expectedDomain` is set, validates that the URL's origin matches `https://{expectedDomain}`.

The `expectedDomain` check is wired to the `JITSI_DOMAIN` environment variable passed from the FastAPI server into the Jinja2 template. This prevents an interpreter from substituting an arbitrary Jitsi server.

---

## How the interpreter joins

The interpreter booth page renders:

- A text input for the Jitsi room URL (pre-filled from `DEFAULT_JITSI_ROOM`)
- A **Join Monitor Room** button
- The Jitsi iframe (hidden until joined)

On join, the JavaScript validates the URL, constructs the embed URL with receive-only parameters, and sets it as the `src` attribute of the `<iframe>`. The Jitsi JS API is not used — the portal interacts with Jitsi solely via iframe embed.

---

## Why no Jitsi JS API

The Jitsi JS API (`JitsiMeetExternalAPI`) would allow programmatic control (mute/unmute, participant events, etc.). The portal deliberately does not use it because:

1. **Scope separation.** The portal does not need to control the Jitsi call. It only needs to display it.
2. **Dependency reduction.** Loading the Jitsi external API JS adds a large dependency and cross-origin complexity.
3. **Robustness.** A pure iframe embed degrades gracefully if the Jitsi server is momentarily unreachable — the rest of the console (chat, participant grid) continues to work.

If future requirements need Jitsi API features (e.g., detecting when the speaker is muted, or auto-joining with a display name), `static/js/interpreter-booth.js` is the right place to extend.

---

## Jitsi server configuration

The portal uses a **self-hosted Jitsi deployment** running as Docker containers alongside the portal.

### Docker Compose services

| Service | Image | Port |
|---|---|---|
| `jitsi-web` | `jitsi/web:stable-9823` | :8443 (HTTPS) |
| `jitsi-prosody` | `jitsi/prosody:stable-9823` | Internal XMPP |
| `jitsi-jicofo` | `jitsi/jicofo:stable-9823` | Conference focus |
| `jitsi-jvb` | `jitsi/jvb:stable-9823` | Video bridge |

Set `DEFAULT_JITSI_ROOM` and `JITSI_DOMAIN` in `.env` to point to the self-hosted instance. The default domain should match the hostname of the `jitsi-web` container.

Room creation, access control, and capacity management are handled by the Jitsi server configuration.

---

## Echo risk and headphone requirement

The monitoring path (Jitsi floor audio playing from the interpreter's speakers) would create acoustic echo if the interpreter does not use headphones. The portal enforces headphone use via the preflight checklist.

The `headphonesConnected` item in the preflight checklist must be checked before the **Go Live** button is enabled. This is an operational control, not a technical detection. The portal trusts the interpreter to self-report headphone use.

Browser echo cancellation (`echoCancellation: true` in `getUserMedia`) provides a fallback if headphones are partially insufficient, but it cannot eliminate severe echo from open speakers.

---

## Jitsi in the booth vs. ingest paths

| Attribute | Jitsi (monitoring) | WHIP ingest |
|---|---|---|
| Direction | Receive only | Send only (mic upload) |
| Transport | Jitsi's own WebRTC stack | WHIP to MediaMTX |
| Audio source | Floor session (speakers, presenter) | Interpreter's microphone |
| Destination | Interpreter's ears | MediaMTX → HLS → listener |
| Echo risk | High (if no headphones) | None (never routed to speakers) |
| Failure impact | Interpreter loses monitoring | Listener loses language audio |
