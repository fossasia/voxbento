/**
 * Unit tests for the interpreter booth reconnect policy (portal/static/js/booth-reconnect.js).
 *
 * Run with: node tests/test_booth_reconnect.mjs
 */
import {
  createBackoff,
  finalCloseMessage,
  RECONNECT_INITIAL_DELAY_MS,
  RECONNECT_MAX_DELAY_MS,
} from '../portal/static/js/booth-reconnect.js'

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

// Close codes
assert(finalCloseMessage(4001) !== null, 'an expired session (4001) is final')
assert(finalCloseMessage(4003) !== null, 'revoked or foreign booth access (4003) is final')
for (const code of [1000, 1001, 1006, 1011, 1012]) {
  assert(finalCloseMessage(code) === null, `close ${code} reconnects`)
}

// Back-off without jitter (random() = 1 gives the full step)
const full = createBackoff({ random: () => 1 })
const delays = Array.from({ length: 6 }, () => full.next())
assert(delays[0] === RECONNECT_INITIAL_DELAY_MS, 'the first retry waits the initial delay')
assert(delays[1] === 2 * RECONNECT_INITIAL_DELAY_MS, 'each retry doubles the delay')
assert(delays.every((d) => d <= RECONNECT_MAX_DELAY_MS), 'the delay never exceeds the maximum')
assert(delays[5] === RECONNECT_MAX_DELAY_MS, 'the delay settles at the maximum')
full.reset()
assert(full.next() === RECONNECT_INITIAL_DELAY_MS, 'a successful connection resets the delay')

// Jitter keeps the delay between half and all of the step
const half = createBackoff({ random: () => 0 })
assert(half.next() === RECONNECT_INITIAL_DELAY_MS / 2, 'jitter never goes below half the step')
const jittered = createBackoff()
for (let i = 0; i < 50; i++) {
  const d = jittered.next()
  if (d < RECONNECT_INITIAL_DELAY_MS / 2 || d > RECONNECT_MAX_DELAY_MS) {
    assert(false, `jittered delay ${d} out of range`)
    break
  }
}
assert(true, 'jittered delays stay in range')

console.log(`\n${passed} passed, ${failed} failed`)
if (failed) process.exit(1)
