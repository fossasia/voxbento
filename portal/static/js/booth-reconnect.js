// Reconnect policy for the interpreter booth WebSocket.

export const RECONNECT_INITIAL_DELAY_MS = 1000
export const RECONNECT_MAX_DELAY_MS = 15000

// Closes that retrying cannot fix: 4001 is a missing or expired session and
// 4003 is a booth this session may not join, or access that was revoked.
const FINAL_CLOSE_MESSAGES = new Map([
  [4001, 'Your session has expired. Reload the page to sign in and rejoin the booth.'],
  [4003, 'You no longer have access to this booth.'],
])

/** The message to show when a close is final, or null when the socket should reconnect. */
export function finalCloseMessage(code) {
  return FINAL_CLOSE_MESSAGES.get(code) || null
}

/**
 * Doubling back-off between attempts. Each delay is between half and all of the
 * current step, so interpreters dropped by the same server restart do not all
 * reconnect at the same moment.
 */
export function createBackoff({
  initialMs = RECONNECT_INITIAL_DELAY_MS,
  maxMs = RECONNECT_MAX_DELAY_MS,
  random = Math.random,
} = {}) {
  let step = initialMs
  return {
    next() {
      const delay = step * (0.5 + random() / 2)
      step = Math.min(step * 2, maxMs)
      return delay
    },
    reset() {
      step = initialMs
    },
  }
}
