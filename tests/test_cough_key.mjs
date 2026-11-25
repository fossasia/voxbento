/**
 * Unit tests for the hold-to-mute cough key controller (portal/static/js/cough-key.js, #1395).
 *
 * Run with: node tests/test_cough_key.mjs
 */
import { CoughKey, createCoughKey } from '../portal/static/js/cough-key.js'

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

class FakeTrack {
  constructor({ enabled = true } = {}) {
    this.enabled = enabled
  }
}

const fakeStream = (...tracks) => ({ getAudioTracks: () => tracks })

// 1. Basic press and release
{
  const track = new FakeTrack({ enabled: true })
  const changes = []
  const cough = createCoughKey({
    getStream: () => fakeStream(track),
    onCoughChange: (active) => changes.push(active),
  })

  assert(cough.isCoughing() === false, 'starts unmuted and not coughing')
  assert(track.enabled === true, 'track starts enabled')

  const pressed = cough.press('key')
  assert(pressed === true, 'press succeeds')
  assert(cough.isCoughing() === true, 'isCoughing is true while held')
  assert(track.enabled === false, 'track is disabled while held')
  assert(changes.length === 1 && changes[0] === true, 'onCoughChange notified true')

  const released = cough.release('key')
  assert(released === true, 'release succeeds')
  assert(cough.isCoughing() === false, 'isCoughing is false after release')
  assert(track.enabled === true, 'track is re-enabled after release')
  assert(changes.length === 2 && changes[1] === false, 'onCoughChange notified false')
}

// 2. Key repeat is ignored
{
  const track = new FakeTrack({ enabled: true })
  const changes = []
  const cough = createCoughKey({
    getStream: () => fakeStream(track),
    onCoughChange: (active) => changes.push(active),
  })

  cough.press('key')
  assert(cough.isCoughing() === true, 'initial press engages cough')
  assert(changes.length === 1, 'notified once on initial press')

  // Key repeat attempt
  const repeatPress = cough.press('key')
  assert(repeatPress === false, 'key repeat press returns false')
  assert(changes.length === 1, 'key repeat triggers no additional change notifications')
  assert(track.enabled === false, 'track remains muted on key repeat')

  cough.release('key')
  assert(cough.isCoughing() === false, 'release disengages cough')
  assert(changes.length === 2 && changes[1] === false, 'notified on release')
}

// 3. Multi-source hold (e.g. keyboard + pointer simultaneously)
{
  const track = new FakeTrack({ enabled: true })
  const changes = []
  const cough = createCoughKey({
    getStream: () => fakeStream(track),
    onCoughChange: (active) => changes.push(active),
  })

  cough.press('pointer')
  cough.press('key')
  assert(changes.length === 1, 'only one transition to active for multiple hold sources')
  assert(cough.isCoughing() === true, 'coughing is active')
  assert(track.enabled === false, 'track is muted')

  // Release pointer while key is still held
  cough.release('pointer')
  assert(cough.isCoughing() === true, 'still coughing while key is held')
  assert(track.enabled === false, 'track remains muted while key is held')
  assert(changes.length === 1, 'no unmute transition yet')

  // Release key
  cough.release('key')
  assert(cough.isCoughing() === false, 'coughing disengaged when all sources released')
  assert(track.enabled === true, 'track restored')
  assert(changes.length === 2 && changes[1] === false, 'unmute notified')
}

// 4. Guard condition: canCough returns false (already muted or inactive)
{
  const track = new FakeTrack({ enabled: true })
  let allowed = false
  const changes = []
  const cough = createCoughKey({
    canCough: () => allowed,
    getStream: () => fakeStream(track),
    onCoughChange: (active) => changes.push(active),
  })

  const pressed = cough.press('key')
  assert(pressed === false, 'press rejected when canCough is false')
  assert(cough.isCoughing() === false, 'cough is not active')
  assert(track.enabled === true, 'track was not muted')
  assert(changes.length === 0, 'no state change notified')

  // Now enable permission and test
  allowed = true
  assert(cough.press('key') === true, 'press succeeds when canCough is true')
  assert(cough.isCoughing() === true, 'cough is active')
  assert(track.enabled === false, 'track muted')
  cough.release('key')
}

// 5. Blur and cancel safety release
{
  const track = new FakeTrack({ enabled: true })
  const changes = []
  const cough = createCoughKey({
    getStream: () => fakeStream(track),
    onCoughChange: (active) => changes.push(active),
  })

  cough.press('key')
  assert(cough.isCoughing() === true, 'held before blur')
  assert(track.enabled === false, 'track muted before blur')

  const blurred = cough.blur()
  assert(blurred === true, 'blur returns true when releasing an active hold')
  assert(cough.isCoughing() === false, 'cough disengaged on blur')
  assert(track.enabled === true, 'track restored on blur')
  assert(changes.length === 2 && changes[1] === false, 'unmute notified on blur')

  // Blur when not held does nothing
  assert(cough.blur() === false, 'blur when idle returns false')
}

// 6. Handover during a cough doesn't leave the next session muted
{
  const track = new FakeTrack({ enabled: true })
  const changes = []
  const cough = createCoughKey({
    getStream: () => fakeStream(track),
    onCoughChange: (active) => changes.push(active),
  })

  cough.press('key')
  assert(track.enabled === false, 'track muted during cough')

  // Handover occurs -> reset() called
  cough.reset()
  assert(cough.isCoughing() === false, 'cough reset on handover')
  assert(track.enabled === true, 'track restored on handover reset')
  assert(changes.length === 2 && changes[1] === false, 'unmute notified on reset')

  // Stale key release after handover does not cause double unmute or error
  const staleRelease = cough.release('key')
  assert(staleRelease === false, 'stale key release after reset is safe and returns false')
  assert(track.enabled === true, 'track remains enabled')
}

// 7. Null stream / missing track safety
{
  const cough = createCoughKey({
    getStream: () => null,
  })

  assert(cough.press('key') === true, 'press handles null stream safely')
  assert(cough.release('key') === true, 'release handles null stream safely')
}

console.log(`\n${passed} passed, ${failed} failed`)
if (failed) process.exit(1)
