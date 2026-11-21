/**
 * Unit tests for the interpreter booth WHIP publisher (portal/static/js/booth-publisher.js, #1357).
 *
 * Run with: node tests/test_booth_publisher.mjs
 */
import { createBackoff } from '../portal/static/js/booth-reconnect.js'
import {
  createBoothPublisher,
  isRetryable,
  PUBLISH_DISCONNECT_GRACE_MS,
  PUBLISH_RETRY_INITIAL_DELAY_MS,
  PUBLISH_RETRY_MAX_DELAY_MS,
  PublishRefusedError,
  WhipError,
} from '../portal/static/js/booth-publisher.js'

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

const flush = async () => {
  for (let i = 0; i < 10; i++) await Promise.resolve()
}

/** Timers the test moves forward by hand. */
function makeClock() {
  let now = 0
  let nextId = 0
  const timers = new Map()
  return {
    setTimer(fn, ms) {
      nextId += 1
      timers.set(nextId, { fn, at: now + ms })
      return nextId
    },
    clearTimer(id) {
      timers.delete(id)
    },
    async advance(ms) {
      const end = now + ms
      for (;;) {
        const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0]
        if (!due) break
        timers.delete(due[0])
        now = due[1].at
        due[1].fn()
        await flush()
      }
      now = end
      await flush()
    },
    /** Delays of the timers still waiting, from now. */
    pending() {
      return [...timers.values()].map((t) => t.at - now)
    },
  }
}

/** An RTCPeerConnection stand-in whose connection state the test sets. */
class FakePeer extends EventTarget {
  constructor() {
    super()
    this.connectionState = 'new'
    this.closed = false
  }

  set(state) {
    this.connectionState = state
    this.dispatchEvent(new Event('connectionstatechange'))
  }

  close() {
    this.closed = true
    this.connectionState = 'closed'
  }
}

/**
 * A publisher wired to fakes. `results` scripts what each openSession() call does:
 * 'connected' (the peer connects at once), 'new' (it connects later), an Error to
 * throw, or a Promise for a slow publish.
 */
function setup({ results = [], shouldPublish = () => true } = {}) {
  const clock = makeClock()
  const peers = []
  const closed = []
  const statuses = []
  const changes = []
  let opens = 0
  const publisher = createBoothPublisher({
    async openSession() {
      opens += 1
      const next = results.length ? results.shift() : 'connected'
      const outcome = next instanceof Promise ? await next : next
      if (outcome instanceof Error) throw outcome
      const pc = new FakePeer()
      if (outcome === 'connected') pc.connectionState = 'connected'
      peers.push(pc)
      return { pc }
    },
    async closeSession(session) {
      session.pc.close()
      closed.push(session.pc)
    },
    shouldPublish,
    onChange(change) {
      statuses.push(change.status)
      changes.push(change)
    },
    backoff: createBackoff({
      initialMs: PUBLISH_RETRY_INITIAL_DELAY_MS,
      maxMs: PUBLISH_RETRY_MAX_DELAY_MS,
      random: () => 1,
    }),
    setTimer: clock.setTimer,
    clearTimer: clock.clearTimer,
  })
  return { publisher, clock, peers, closed, statuses, changes, opens: () => opens }
}

// ── Starting ─────────────────────────────────────────────────────────────────

{
  const t = setup({ results: ['new'] })
  t.publisher.start()
  assert(t.publisher.status === 'connecting', 'start() reports connecting before the first attempt')
  await t.clock.advance(0)
  assert(t.opens() === 1, 'start() publishes at once')
  assert(t.publisher.status === 'connecting', 'stays connecting until the peer connects')
  assert(!t.publisher.publishing, 'not publishing before the peer connects')
  t.peers[0].set('connected')
  assert(t.publisher.status === 'live', 'live once the peer connects')
  assert(t.publisher.publishing, 'publishing while live')
  t.publisher.start()
  await t.clock.advance(1000)
  assert(t.opens() === 1, 'start() while running does not publish twice')
}

{
  const t = setup({ results: ['connected'] })
  t.publisher.start()
  await t.clock.advance(0)
  assert(t.publisher.status === 'live', 'a peer already connected when the answer is applied is live')
}

// ── The publish drops ────────────────────────────────────────────────────────

{
  const t = setup({ results: ['connected', 'new'] })
  t.publisher.start()
  await t.clock.advance(0)
  t.peers[0].set('failed')
  assert(t.publisher.status === 'reconnecting', 'a failed peer reports reconnecting')
  assert(t.changes.at(-1).delayMs === 0, 'the first retry after a drop is immediate')
  assert(t.peers[0].closed, 'the dead session is closed')
  await t.clock.advance(0)
  assert(t.opens() === 2, 'a failed peer is published again')
  t.peers[1].set('connected')
  assert(t.publisher.status === 'live', 'back to live after the new peer connects')
  assert(t.changes.at(-1).attempt === 0, 'the attempt count resets once live')
  t.peers[0].set('connected')
  assert(t.statuses.filter((s) => s === 'live').length === 2, 'events from a replaced peer are ignored')
}

{
  const t = setup({ results: ['connected'] })
  t.publisher.start()
  await t.clock.advance(0)
  t.peers[0].set('disconnected')
  assert(t.publisher.status === 'unstable', 'a disconnected peer is unstable, not rebuilt at once')
  assert(t.publisher.publishing, 'an unstable peer still counts as publishing')
  await t.clock.advance(PUBLISH_DISCONNECT_GRACE_MS - 1)
  t.peers[0].set('connected')
  assert(t.publisher.status === 'live', 'a peer that recovers within the grace period stays')
  await t.clock.advance(PUBLISH_DISCONNECT_GRACE_MS)
  assert(t.opens() === 1, 'a recovered peer is not rebuilt')
  assert(!t.peers[0].closed, 'a recovered peer is not closed')
}

{
  const t = setup({ results: ['connected', 'connected'] })
  t.publisher.start()
  await t.clock.advance(0)
  t.peers[0].set('disconnected')
  await t.clock.advance(PUBLISH_DISCONNECT_GRACE_MS)
  assert(t.statuses.includes('reconnecting'), 'a peer still disconnected after the grace period is rebuilt')
  assert(t.opens() === 2 && t.publisher.status === 'live', 'the rebuilt publish goes live')
}

// ── Failed attempts ─────────────────────────────────────────────────────────

{
  const t = setup({
    results: [new Error('Failed to fetch'), new WhipError(503, 'unavailable'), new WhipError(409, 'busy'), 'connected'],
  })
  t.publisher.start()
  await t.clock.advance(0)
  assert(t.publisher.status === 'reconnecting', 'a network error is retried')
  assert(t.clock.pending()[0] === PUBLISH_RETRY_INITIAL_DELAY_MS, 'the first retry waits the initial delay')
  await t.clock.advance(PUBLISH_RETRY_INITIAL_DELAY_MS)
  assert(t.opens() === 2 && t.publisher.status === 'reconnecting', 'a 503 is retried')
  assert(t.clock.pending()[0] === PUBLISH_RETRY_INITIAL_DELAY_MS * 2, 'the delay doubles')
  await t.clock.advance(PUBLISH_RETRY_INITIAL_DELAY_MS * 2)
  assert(t.opens() === 3 && t.publisher.status === 'reconnecting', 'a 409 (path still held) is retried')
  assert(t.changes.at(-1).attempt === 3, 'each failed attempt is counted')
  await t.clock.advance(PUBLISH_RETRY_INITIAL_DELAY_MS * 4)
  assert(t.publisher.status === 'live', 'goes live once an attempt succeeds')
}

{
  const t = setup({ results: Array.from({ length: 50 }, () => new Error('down')) })
  t.publisher.start()
  await t.clock.advance(0)
  for (let i = 0; i < 10; i++) await t.clock.advance(PUBLISH_RETRY_MAX_DELAY_MS)
  const delays = t.changes.filter((c) => c.status === 'reconnecting').map((c) => c.delayMs)
  assert(delays.at(-1) === PUBLISH_RETRY_MAX_DELAY_MS, 'the delay grows to the maximum')
  assert(Math.max(...delays) === PUBLISH_RETRY_MAX_DELAY_MS, 'the delay never goes past the maximum')
  assert(t.publisher.running, 'keeps trying while the booth should be live')
}

for (const [error, label] of [
  [new PublishRefusedError('Only the active interpreter can go live.'), 'a refused ticket'],
  [new WhipError(401, 'unauthorized'), 'a WHIP 401'],
  [new WhipError(403, 'forbidden'), 'a WHIP 403'],
]) {
  const t = setup({ results: [error] })
  t.publisher.start()
  await t.clock.advance(0)
  await t.clock.advance(60000)
  assert(t.publisher.status === 'refused' && t.opens() === 1, `${label} stops without retrying`)
  assert(!t.publisher.running, `${label} leaves the publisher stopped`)
  assert(t.changes.at(-1).error === error, `${label} is passed to onChange`)
}

// ── Stopping ────────────────────────────────────────────────────────────────

{
  const t = setup({ results: ['connected'] })
  t.publisher.start()
  await t.clock.advance(0)
  await t.publisher.stop()
  assert(t.publisher.status === 'idle' && !t.publisher.running, 'stop() goes idle')
  assert(t.closed.length === 1, 'stop() closes the session')
  t.peers[0].set('failed')
  await t.clock.advance(60000)
  assert(t.opens() === 1, 'a stopped publisher never publishes again by itself')
}

{
  let finish
  const slow = new Promise((resolve) => { finish = resolve })
  const t = setup({ results: [slow] })
  t.publisher.start()
  await t.clock.advance(0)
  await t.publisher.stop()
  finish('connected')
  await flush()
  assert(t.closed.length === 1, 'a publish that finishes after stop() is released')
  assert(t.publisher.status === 'idle', 'a late publish does not bring the publisher back to live')
}

{
  const t = setup({ results: [new Error('down')] })
  t.publisher.start()
  await t.clock.advance(0)
  await t.publisher.stop()
  await t.clock.advance(60000)
  assert(t.opens() === 1 && t.clock.pending().length === 0, 'stop() cancels a scheduled retry')
}

{
  let allowed = true
  const t = setup({ results: ['connected'], shouldPublish: () => allowed })
  t.publisher.start()
  await t.clock.advance(0)
  allowed = false
  t.peers[0].set('failed')
  await t.clock.advance(0)
  assert(t.opens() === 1, 'no new publish once this participant may no longer publish')
  assert(t.publisher.status === 'idle' && !t.publisher.running, 'the publisher stops instead')
}

// ── Retrying early ──────────────────────────────────────────────────────────

{
  const t = setup({ results: [new Error('down'), new Error('down'), 'connected'] })
  t.publisher.start()
  await t.clock.advance(0)
  await t.clock.advance(PUBLISH_RETRY_INITIAL_DELAY_MS)
  assert(t.clock.pending()[0] > 0, 'a retry is waiting')
  t.publisher.retryNow()
  await t.clock.advance(0)
  assert(t.publisher.status === 'live', 'retryNow() skips the remaining back-off')
  t.publisher.retryNow()
  await t.clock.advance(0)
  assert(t.opens() === 3, 'retryNow() does nothing while live')
}

// ── Which errors are worth retrying ─────────────────────────────────────────

assert(isRetryable(new Error('Not connected to server')), 'no booth socket: retry')
assert(isRetryable(new Error('Timed out waiting for a publish ticket')), 'ticket timeout: retry')
for (const status of [409, 429, 500, 502, 503]) {
  assert(isRetryable(new WhipError(status, '')), `WHIP ${status}: retry`)
}
for (const status of [400, 401, 403, 404]) {
  assert(!isRetryable(new WhipError(status, '')), `WHIP ${status}: refused`)
}
assert(!isRetryable(new PublishRefusedError('no')), 'refused ticket: refused')

console.log(`\n${passed} passed, ${failed} failed`)
if (failed) process.exit(1)
