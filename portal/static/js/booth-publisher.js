// Keeps the active interpreter's WHIP publish to MediaMTX running (#1357).
//
// A publish can drop without the booth WebSocket noticing: a network change, a
// MediaMTX restart or a kicked session ends the peer connection while the page
// stays connected to the portal. The publisher watches the peer connection and
// publishes again with a fresh ticket, backing off between attempts, until it
// is stopped or the server refuses the publish.

import { createBackoff } from './booth-reconnect.js'

/** How long a `disconnected` peer may take to recover on its own before it is rebuilt. */
export const PUBLISH_DISCONNECT_GRACE_MS = 3000
/** How long a new peer may take to connect after MediaMTX accepted its offer. */
export const PUBLISH_CONNECT_TIMEOUT_MS = 15000
export const PUBLISH_RETRY_INITIAL_DELAY_MS = 250
export const PUBLISH_RETRY_MAX_DELAY_MS = 15000

/**
 * A publish the server refused: the ticket was denied or the configuration is
 * missing. Trying again would get the same answer.
 */
export class PublishRefusedError extends Error {
  constructor(message) {
    super(message)
    this.name = 'PublishRefusedError'
  }
}

/** A WHIP answer that was not 2xx. */
export class WhipError extends Error {
  constructor(status, detail) {
    super(`WHIP error ${status}: ${detail}`)
    this.name = 'WhipError'
    this.status = status
  }
}

// The microphone is refused or missing; the interpreter has to act, so retrying only hides it.
// NotReadableError (device busy) and AbortError stay retryable, since they can clear by themselves.
const FINAL_MEDIA_ERRORS = new Set(['NotAllowedError', 'SecurityError', 'NotFoundError', 'OverconstrainedError'])

/**
 * Whether another attempt could succeed. Network errors, timeouts and the WHIP
 * answers 409 (path still held), 429 and 5xx can; any other WHIP 4xx, a refused
 * ticket and a refused or missing microphone cannot.
 */
export function isRetryable(error) {
  if (error instanceof PublishRefusedError) return false
  if (error instanceof WhipError) return error.status === 409 || error.status === 429 || error.status >= 500
  if (FINAL_MEDIA_ERRORS.has(error?.name)) return false
  return true
}

/**
 * Create a publisher.
 *
 * - `openSession()` builds a peer connection, publishes it over WHIP and resolves
 *   with `{ pc, ... }`. It throws `PublishRefusedError` or `WhipError` when refused.
 * - `closeSession(session)` closes the peer connection and deletes its WHIP resource.
 * - `shouldPublish()` says whether this participant may still publish (active
 *   interpreter, booth unlocked). When it turns false, the publisher stops.
 * - `onChange({ status, attempt, error, delayMs })` runs on every status change.
 *
 * Statuses: `idle` (not publishing), `connecting` (first attempt), `live`,
 * `unstable` (a live peer reported `disconnected`; waiting to see if it recovers),
 * `reconnecting` (publishing again after a drop or a failed attempt) and
 * `refused` (stopped because the server refused the publish).
 */
export function createBoothPublisher({
  openSession,
  closeSession,
  shouldPublish = () => true,
  onChange = () => {},
  graceMs = PUBLISH_DISCONNECT_GRACE_MS,
  connectTimeoutMs = PUBLISH_CONNECT_TIMEOUT_MS,
  backoff = createBackoff({ initialMs: PUBLISH_RETRY_INITIAL_DELAY_MS, maxMs: PUBLISH_RETRY_MAX_DELAY_MS }),
  setTimer = (fn, ms) => setTimeout(fn, ms),
  clearTimer = (id) => clearTimeout(id),
  log = console,
} = {}) {
  let running = false
  let status = 'idle'
  let session = null
  // Bumped by start() and stop(); an attempt from an older generation discards its result.
  let generation = 0
  let connectingGeneration = null
  let attempt = 0
  let retryTimer = null
  let graceTimer = null
  let connectTimer = null
  let sessionListener = null
  // The previous session's release, which a new attempt waits for so MediaMTX frees the path first.
  let releasing = Promise.resolve()

  function setStatus(next, extra = {}) {
    status = next
    onChange({ status: next, attempt, ...extra })
  }

  function cancelTimer(id) {
    if (id !== null) clearTimer(id)
    return null
  }

  function clearPeerTimers() {
    graceTimer = cancelTimer(graceTimer)
    connectTimer = cancelTimer(connectTimer)
  }

  function clearTimers() {
    retryTimer = cancelTimer(retryTimer)
    clearPeerTimers()
  }

  async function closeQuietly(old) {
    try {
      await closeSession(old)
    } catch (error) {
      log.warn('Could not release the previous booth publish:', error)
    }
  }

  /** Close a session and remember the release, so the next attempt waits for it. */
  function release(old) {
    if (!old) return releasing
    releasing = closeQuietly(old)
    return releasing
  }

  function takeSession() {
    const old = session
    session = null
    if (old && sessionListener) old.pc.removeEventListener('connectionstatechange', sessionListener)
    sessionListener = null
    clearPeerTimers()
    return old
  }

  function schedule(delayMs) {
    cancelTimer(retryTimer)
    retryTimer = setTimer(() => {
      retryTimer = null
      connect()
    }, delayMs)
  }

  /** The peer connection dropped or never connected: publish again, at once the first time, then backing off. */
  function peerLost(owned) {
    if (!running || owned !== session) return
    const delayMs = attempt === 0 ? 0 : backoff.next()
    attempt += 1
    // Stop sending the dead session now; connect() waits for this release before publishing again.
    release(takeSession())
    setStatus('reconnecting', { delayMs })
    schedule(delayMs)
  }

  function onPeerState(owned) {
    if (owned !== session) return
    const state = owned.pc.connectionState
    if (state === 'connected') {
      clearPeerTimers()
      if (status !== 'live') {
        attempt = 0
        backoff.reset()
        setStatus('live')
      }
    } else if (state === 'failed') {
      peerLost(owned)
    } else if (state === 'disconnected' && graceTimer === null) {
      // ICE often recovers from a short blip by itself; only rebuild if it does not.
      // A peer that has not connected yet stays connecting or reconnecting, never live.
      if (status === 'live') setStatus('unstable')
      graceTimer = setTimer(() => {
        graceTimer = null
        peerLost(owned)
      }, graceMs)
    }
  }

  async function connect() {
    if (!running || connectingGeneration === generation) return
    if (!shouldPublish()) {
      await stop()
      return
    }
    const gen = generation
    connectingGeneration = gen
    try {
      await release(takeSession())
      if (gen !== generation) return
      const opened = await openSession()
      if (gen !== generation) {
        // Stopped or restarted while publishing: give back the session nobody will use.
        await release(opened)
        return
      }
      session = opened
      sessionListener = () => onPeerState(opened)
      opened.pc.addEventListener('connectionstatechange', sessionListener)
      // MediaMTX accepted the offer; a peer that never connects is treated as dropped.
      connectTimer = setTimer(() => {
        connectTimer = null
        if (opened === session && status !== 'live' && status !== 'unstable') peerLost(opened)
      }, connectTimeoutMs)
      // The peer may already be connected by the time the WHIP answer is applied.
      onPeerState(opened)
    } catch (error) {
      if (gen !== generation) return
      if (!isRetryable(error)) {
        log.warn('Booth publish refused:', error)
        running = false
        generation += 1
        clearTimers()
        setStatus('refused', { error })
        return
      }
      const delayMs = backoff.next()
      attempt += 1
      log.warn(`Booth publish attempt ${attempt} failed; retrying in ${Math.round(delayMs)} ms:`, error)
      setStatus('reconnecting', { error, delayMs })
      schedule(delayMs)
    } finally {
      if (connectingGeneration === gen) connectingGeneration = null
    }
  }

  /** Start publishing. Does nothing while already running. */
  function start() {
    if (running) return
    running = true
    generation += 1
    attempt = 0
    backoff.reset()
    clearTimers()
    setStatus('connecting')
    schedule(0)
  }

  /** Stop publishing and release the session. Safe to call at any time. */
  async function stop() {
    const wasActive = running || session !== null || status !== 'idle'
    running = false
    generation += 1
    attempt = 0
    clearTimers()
    const old = takeSession()
    if (wasActive) setStatus('idle')
    await release(old)
  }

  /** Skip the remaining back-off, for example when the network or the booth socket comes back. */
  function retryNow() {
    if (!running || session !== null) return
    if (connectingGeneration === generation) return
    schedule(0)
  }

  return {
    start,
    stop,
    retryNow,
    get status() {
      return status
    },
    /** True from start() until stop() or a refusal, including while reconnecting. */
    get running() {
      return running
    },
    /** Whether audio is (or, while `unstable`, may still be) reaching MediaMTX. */
    get publishing() {
      return status === 'live' || status === 'unstable'
    },
  }
}
