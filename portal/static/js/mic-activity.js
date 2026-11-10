// What the microphone is really doing, as opposed to what the booth's Mute button says (#1214).
//
// Muting the device itself (a keyboard mute key, the operating system's input mute, a headset
// button) does not touch the booth page: the stream stays enabled, so the page kept telling
// everyone the interpreter was speaking. The browser reports the real state on the
// MediaStreamTrack: `muted` and the `mute` / `unmute` events, and `ended` when the device is gone.

const TRACK_EVENTS = ['mute', 'unmute', 'ended']

/** True when the browser says this track delivers no audio: muted by the device, or ended. */
export function isTrackSilenced(track) {
  return track.muted === true || track.readyState === 'ended'
}

/** The device is muted when no audio track of the stream can deliver audio. */
function isDeviceMuted(tracks) {
  return tracks.length === 0 || tracks.every(isTrackSilenced)
}

/**
 * Follow the real mute state of a microphone stream.
 *
 * `onChange(deviceMuted)` runs only when the state changes. Call `stop()` to remove the listeners
 * (for example when the stream is replaced).
 */
export function watchMicTracks(stream, onChange) {
  const tracks = stream.getAudioTracks()
  let deviceMuted = isDeviceMuted(tracks)

  const listener = () => {
    const next = isDeviceMuted(tracks)
    if (next === deviceMuted) return
    deviceMuted = next
    onChange(next)
  }

  for (const track of tracks) {
    for (const type of TRACK_EVENTS) track.addEventListener(type, listener)
  }

  return {
    deviceMuted: () => deviceMuted,
    stop() {
      for (const track of tracks) {
        for (const type of TRACK_EVENTS) track.removeEventListener(type, listener)
      }
    },
  }
}

/** Speaking means the booth's Mute button is off and the device is not muted either. */
export function micIsActive({ uiMuted, deviceMuted }) {
  return !uiMuted && !deviceMuted
}
