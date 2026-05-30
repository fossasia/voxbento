# Jitsi Room Management Specification

This document defines the policy for how Jitsi rooms are used in the interpretation portal, and what the portal does and does not control.

---

## Scope

The interpretation portal **embeds** Jitsi rooms via iframe. It does not:

- Create or destroy Jitsi rooms.
- Configure Jitsi server-side settings.
- Manage Jitsi user accounts or moderation.
- Use the Jitsi External API JavaScript library.

All Jitsi room management (capacity, access control, moderation) is the responsibility of whoever operates the Jitsi server.

---

## Room naming convention

There is no enforced naming convention for Jitsi rooms used as monitoring rooms. However, the recommended pattern is:

```
{event-slug}-{stage-id}
```

For example: `eventyay-2025-hall-a`

The room name is configured via `DEFAULT_JITSI_ROOM` in `.env`. Interpreters can also manually enter a different Jitsi URL in the monitoring panel if needed.

---

## Embed policy

The portal embeds Jitsi using a URL hash parameter configuration that enforces receive-only mode by default:

| Hash parameter | Value | Effect |
|---|---|---|
| `config.startWithAudioMuted` | `true` | Interpreter's mic is muted in Jitsi on join |
| `config.startWithVideoMuted` | `true` | Interpreter's camera is off in Jitsi on join |
| `config.prejoinPageEnabled` | `false` | Skip the Jitsi prejoin/lobby page |
| `config.disableInitialGUM` | `true` | Skip initial getUserMedia prompt in Jitsi |
| `config.startSilent` | `true` | Start with audio muted (belt and braces) |

The goal is to prevent the interpreter from accidentally publishing into the Jitsi call. The Jitsi call is for monitoring only; the ingest path is the separate WebRTC connection.

**Important:** These parameters configure the Jitsi client's local behaviour. A determined user could unmute their mic in Jitsi and accidentally publish floor audio. The operational mitigation is to remind interpreters that the Jitsi frame is monitoring-only and to use their headphones.

---

## Domain validation

The portal validates that the Jitsi URL entered by the interpreter uses the configured `JITSI_DOMAIN`. This prevents:

- Accidental use of an arbitrary Jitsi server.
- URL substitution attacks where a malicious link redirects the interpreter to a hostile Jitsi room.

Validation is performed by the `buildJitsiEmbedUrl()` function in `static/js/interpreter-booth.js`.

If the URL's origin does not match `https://{JITSI_DOMAIN}`, an error is shown and the iframe is not loaded.

This check can be bypassed in development by setting `JITSI_DOMAIN` to an empty string, which disables domain validation.

---

## What interpreters see in the Jitsi frame

The Jitsi frame shows the floor session as a normal Jitsi meeting participant view. The interpreter sees:

- The speaker's video (if the speaker has video enabled in Jitsi)
- The floor audio (speaker's microphone)
- Other Jitsi participants (e.g., the event presenter, moderator)
- Jitsi's own UI (mute/unmute, etc.)

The interpreter can interact with the Jitsi UI (e.g., manually unmute if they need to ask a quick clarification to the presenter). This is intentional — the portal does not lock down the Jitsi UI.

---

## Simultaneous Jitsi and ingest audio

The interpreter hears two audio sources simultaneously:

1. **Jitsi floor audio** — from the floor session (the speech they are interpreting).
2. **Ingest feedback** — there is none. The ingest audio is never played back to the interpreter.

This is the correct configuration. The interpreter hears the speaker and speaks their translation without hearing themselves. Headphones are required to prevent the Jitsi floor audio from leaking into the ingest mic.

---

## Multiple booths, one Jitsi room

Multiple interpreter booths for the same event (e.g., French booth and German booth) will typically use the **same Jitsi room** for monitoring. This is intentional — they are all monitoring the same floor session.

Each booth has its own ingest channel (`channel_id`), its own HLS stream, and its own WebSocket room. Only the Jitsi monitoring room is shared.

---

## Jitsi self-hosting

The portal includes a self-hosted Jitsi Meet deployment via Docker Compose (4 containers: jitsi-web, jitsi-prosody, jitsi-jicofo, jitsi-jvb). Key configuration:

- `BOSH_RELATIVE=true` — BOSH URLs use relative paths to avoid double-scheme bugs.
- `ENABLE_XMPP_WEBSOCKET=0` — BOSH is sufficient for monitoring; disables XMPP WebSocket.
- `DOCKER_HOST_ADDRESS` — Must be set to the host's LAN IP for JVB ICE candidates to be reachable.
- Jitsi is served on HTTP port `:8080` for local development (avoids self-signed cert issues in iframes).

---

## Future: Jitsi API integration

If future requirements need programmatic Jitsi control, the extension point is `static/js/interpreter-booth.js`. Specifically:

- Replace or augment the `<iframe>` embed with `new JitsiMeetExternalAPI(...)`.
- This would allow: detecting interpreter mic state in Jitsi, auto-muting/unmuting the Jitsi participant, participant event tracking.

This is not planned for the current phase. Keep the Jitsi integration as a simple iframe embed.
