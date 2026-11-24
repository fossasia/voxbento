// Relay interpreting on the booth page (#1382).
//
// The room's relay booth is the booth that interpreters who don't understand the speaker listen
// to instead of the floor. Mission Control can change it during a session, and the relay booth
// goes on and off air, so the server pushes `booth:relay` and the page follows it without a
// reload. These helpers decide what the Relay Audio card says; interpreter-booth.js does the DOM
// and the audio.
//
// `relay` is what this booth may play: { booth_id, language_code, language_name, whep_url, on_air },
// or null. It is null for the relay booth itself, which must never hear its own channel.

/** Read the relay fields of `booth:joined` / `booth:relay` (or the page's first render). */
export function readRelay(data, ownBoothId) {
  const relayBoothId = typeof data?.relay_booth_id === 'string' ? data.relay_booth_id : null
  const relay = data?.relay && typeof data.relay.whep_url === 'string' ? data.relay : null
  return { relayBoothId, relay, isRelayBooth: relayBoothId !== null && relayBoothId === ownBoothId }
}

/** The badge in the card's header. `playback` is the last WHEP state while listening. */
export function relayBadge({ relay, isRelayBooth, listening, playback }) {
  if (isRelayBooth) return { text: 'You are the relay', tone: 'success' }
  if (!relay) return { text: 'No relay', tone: 'warning' }
  if (!relay.on_air) return { text: 'Off air', tone: 'warning' }
  if (!listening) return { text: 'On air', tone: 'success' }
  if (playback?.peerConnection === 'failed') return { text: 'Error', tone: 'danger' }
  if (playback?.audioActive) return { text: 'Listening', tone: 'success' }
  return { text: 'Connecting...', tone: '' }
}

/** The sentence in the card's body. */
export function relayDescription({ relay, isRelayBooth }) {
  if (isRelayBooth) return 'Your booth is the relay for this room: other booths listen to your interpretation.'
  if (!relay) return 'No relay booth is set for this room.'
  if (!relay.on_air) {
    return `The ${relay.language_name} booth is the relay, but it is off air right now. Listen to the floor until it is back.`
  }
  return `The ${relay.language_name} booth is the relay for this room.`
}

/** What screen readers hear when the relay changes, or '' when nothing worth saying changed. */
export function relayAnnouncement(prev, next) {
  if (prev.relayBoothId !== next.relayBoothId) {
    if (next.isRelayBooth) return 'Your booth is now the relay for this room.'
    if (!next.relay) return 'The relay was removed.'
    return next.relay.on_air
      ? `The relay is now the ${next.relay.language_name} booth.`
      : `The relay is now the ${next.relay.language_name} booth, which is off air.`
  }
  if (prev.relay && next.relay && prev.relay.on_air !== next.relay.on_air) {
    return next.relay.on_air
      ? `The ${next.relay.language_name} relay is back on air.`
      : `The ${next.relay.language_name} relay went off air.`
  }
  return ''
}

/** What the player must do: keep playing, switch to the new source, or stop. */
export function relayPlaybackAction({ listening, prevRelay, nextRelay }) {
  if (!listening) return 'none'
  if (!nextRelay) return 'stop'
  if (!prevRelay || prevRelay.whep_url !== nextRelay.whep_url) return 'switch'
  return 'none'
}
