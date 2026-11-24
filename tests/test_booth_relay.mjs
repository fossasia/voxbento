/**
 * Unit tests for the booth page's relay card (portal/static/js/booth-relay.js, #1382).
 *
 * Run with: node tests/test_booth_relay.mjs
 */
import {
  readRelay,
  relayAnnouncement,
  relayBadge,
  relayDescription,
  relayPlaybackAction,
} from '../portal/static/js/booth-relay.js'

let passed = 0
let failed = 0

function assert(condition, message) {
  if (condition) {
    console.log('  PASS: ' + message)
    passed++
  } else {
    console.error('  FAIL: ' + message)
    failed++
  }
}

const OWN = 'demo-1-fr'
const english = (onAir) => ({
  booth_id: 'demo-1-en',
  language_code: 'en',
  language_name: 'English',
  whep_url: 'https://media.example/demo/1/en/whep',
  on_air: onAir,
})
const spanish = {
  booth_id: 'demo-1-es',
  language_code: 'es',
  language_name: 'Spanish',
  whep_url: 'https://media.example/demo/1/es/whep',
  on_air: true,
}
const fromEnglish = (onAir) => readRelay({ relay_booth_id: 'demo-1-en', relay: english(onAir) }, OWN)
const none = readRelay({ relay_booth_id: null, relay: null }, OWN)
const weAreRelay = readRelay({ relay_booth_id: OWN, relay: null }, OWN)

console.log('readRelay')
assert(fromEnglish(true).relay.language_name === 'English' && !fromEnglish(true).isRelayBooth, 'reads a relay to play')
assert(weAreRelay.isRelayBooth && weAreRelay.relay === null, 'knows when its own booth is the relay')
assert(none.relayBoothId === null && none.relay === null && !none.isRelayBooth, 'no relay')
assert(readRelay(null, OWN).relay === null, 'missing page data means no relay')
assert(readRelay({ relay_booth_id: 'x', relay: { language_name: 'X' } }, OWN).relay === null, 'ignores a relay without a URL')

console.log('relayBadge')
const badge = (relay, listening, playback, isRelayBooth = false) => relayBadge({ relay, isRelayBooth, listening, playback })
assert(badge(null, false, null, true).text === 'You are the relay', 'relay booth')
assert(badge(null, false, null).text === 'No relay', 'no relay')
assert(badge(english(false), true, { audioActive: true }).text === 'Off air', 'off air wins over a connected player')
assert(badge(english(true), false, null).text === 'On air', 'on air, not listening')
assert(badge(english(true), true, null).text === 'Connecting...', 'connecting')
assert(badge(english(true), true, { audioActive: true }).text === 'Listening', 'listening')
assert(badge(english(true), true, { peerConnection: 'failed' }).tone === 'danger', 'failed playback')

console.log('relayDescription')
assert(relayDescription(weAreRelay).includes('Your booth is the relay'), 'relay booth')
assert(relayDescription(fromEnglish(false)).includes('off air'), 'off air tells the interpreter to use the floor')
assert(relayDescription(none) === 'No relay booth is set for this room.', 'no relay')

console.log('relayAnnouncement')
assert(relayAnnouncement(none, fromEnglish(true)) === 'The relay is now the English booth.', 'new relay')
assert(relayAnnouncement(none, fromEnglish(false)).endsWith('which is off air.'), 'new relay off air')
assert(relayAnnouncement(fromEnglish(true), none) === 'The relay was removed.', 'relay removed')
assert(relayAnnouncement(fromEnglish(true), weAreRelay) === 'Your booth is now the relay for this room.', 'became the relay')
assert(relayAnnouncement(fromEnglish(true), fromEnglish(false)) === 'The English relay went off air.', 'went off air')
assert(relayAnnouncement(fromEnglish(false), fromEnglish(true)) === 'The English relay is back on air.', 'back on air')
assert(relayAnnouncement(fromEnglish(true), fromEnglish(true)) === '', 'nothing changed')

console.log('relayPlaybackAction')
assert(relayPlaybackAction({ listening: false, prevRelay: english(true), nextRelay: spanish }) === 'none', 'not listening')
assert(relayPlaybackAction({ listening: true, prevRelay: english(true), nextRelay: spanish }) === 'switch', 'new source')
assert(relayPlaybackAction({ listening: true, prevRelay: english(true), nextRelay: english(false) }) === 'none', 'same source going off air keeps playing')
assert(relayPlaybackAction({ listening: true, prevRelay: english(true), nextRelay: null }) === 'stop', 'relay removed or own booth')

console.log(`\n${passed} passed, ${failed} failed`)
if (failed > 0) process.exit(1)
