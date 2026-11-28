// Hold-to-mute cough key for the interpreter booth (#1395).
//
// While the key or button is held the interpreter's microphone sends silence; letting go brings it
// back. Unlike Mute it is local only: the interpreter stays live and keeps the channel, so nothing
// is sent to the server and the booth, Mission Control and attendance are unchanged.
//
// The booth never writes `track.enabled` directly. It asks `micTrackEnabled()`, so a cough, the
// Mute button and a handover can't overwrite each other.

/** The C key with no modifier, so Ctrl+C / Cmd+C still copy. */
export function isCoughShortcut(event) {
  return event.code === 'KeyC' && !event.ctrlKey && !event.metaKey && !event.altKey && !event.shiftKey
}

/** Keys typed into these elements belong to the element, not to the booth shortcuts. */
export function isTypingTarget(element) {
  if (!element) return false
  const tag = element.tagName?.toLowerCase()
  return tag === 'input' || tag === 'textarea' || tag === 'select' || element.isContentEditable === true
}

/** The microphone sends audio only when it isn't muted, no cough is held and no handover is stopping it. */
export function micTrackEnabled({ micMuted, coughHeld, relayingOut = false }) {
  return !micMuted && !coughHeld && !relayingOut
}

/**
 * Track who is holding the cough key: the C key, the button through the keyboard, or one or more
 * pointers (each by `pointerId`). The cough lasts while any of them is held.
 *
 * `canCough()` is asked on every press; a press it refuses is ignored. `onChange(held)` runs only
 * when the held state flips.
 */
export function createCoughKey({ canCough = () => true, onChange = () => {} } = {}) {
  const sources = new Set()

  const notifyIfChanged = (wasHeld) => {
    const held = sources.size > 0
    if (held !== wasHeld) onChange(held)
  }

  return {
    /** Start holding from `source`. Returns true when the press counts. */
    press(source) {
      if (sources.has(source)) return true
      if (!canCough()) return false
      const wasHeld = sources.size > 0
      sources.add(source)
      notifyIfChanged(wasHeld)
      return true
    },

    /** Stop holding from `source`; releasing a source that isn't held does nothing. */
    release(source) {
      if (!sources.has(source)) return
      sources.delete(source)
      notifyIfChanged(true)
    },

    /** Let go of every source: the window lost focus, or the interpreter can no longer cough. */
    releaseAll() {
      if (sources.size === 0) return
      sources.clear()
      notifyIfChanged(true)
    },

    isHeld() {
      return sources.size > 0
    },
  }
}
