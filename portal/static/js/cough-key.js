// Hold-to-mute cough key for the interpreter console (ISO 20109, #1395).
//
// Interpreters hold the cough key to momentarily cut the mic without latching mute,
// sending silence over WebRTC while keeping the WHIP ingest alive and preserving the
// on-air status for attendees, attendance, and Mission Control.

/**
 * Controller for the momentary cough key / button.
 *
 * Cuts the mic stream track while held and restores it on release. Pure state machine
 * with no DOM dependencies so it can be unit tested in Node.
 */
export class CoughKey {
  /**
   * @param {Object} [options]
   * @param {() => boolean} [options.canCough] - Returns true if interpreter is permitted to cough-mute
   * @param {() => MediaStream|{getAudioTracks: () => Array<{enabled: boolean}>}|null} [options.getStream] - Stream provider
   * @param {(active: boolean) => void} [options.onCoughChange] - Called on cough state transition
   */
  constructor({ canCough, getStream, onCoughChange } = {}) {
    this._canCough = canCough ?? (() => true)
    this._getStream = getStream ?? (() => null)
    this._onCoughChange = onCoughChange ?? (() => {})
    this._sources = new Set()
    this._active = false
  }

  /** True if the cough key is actively cutting the microphone. */
  isCoughing() {
    return this._active
  }

  /**
   * Press down a cough hold source ('key', 'pointer', etc.).
   *
   * @param {string} [source='default']
   * @returns {boolean} True if press initiated or tracked
   */
  press(source = 'default') {
    if (this._sources.has(source)) {
      // Key repeat or duplicate pointerdown ignored
      return false
    }

    if (!this._active && !this._canCough()) {
      return false
    }

    this._sources.add(source)

    if (!this._active) {
      this._active = true
      this._applyTrackMute(true)
      this._onCoughChange(true)
    }
    return true
  }

  /**
   * Release a cough hold source.
   *
   * @param {string} [source='default']
   * @returns {boolean} True if release removed an active source
   */
  release(source = 'default') {
    if (!this._sources.has(source)) {
      return false
    }

    this._sources.delete(source)

    if (this._sources.size === 0 && this._active) {
      this._active = false
      this._applyTrackMute(false)
      this._onCoughChange(false)
    }
    return true
  }

  /** Cancel active hold (e.g., pointercancel or pointerleave). */
  cancel() {
    return this.blur()
  }

  /**
   * Window blur or safety release. Clears all held sources and restores mic audio.
   *
   * @returns {boolean} True if an active hold was released
   */
  blur() {
    if (!this._active && this._sources.size === 0) {
      return false
    }
    this._sources.clear()
    if (this._active) {
      this._active = false
      this._applyTrackMute(false)
      this._onCoughChange(false)
    }
    return true
  }

  /** Reset state completely (e.g., during handover, stream change, or disconnect). */
  reset() {
    const wasActive = this._active
    this._sources.clear()
    this._active = false
    if (wasActive) {
      this._applyTrackMute(false)
      this._onCoughChange(false)
    }
  }

  _applyTrackMute(muted) {
    const stream = this._getStream()
    if (!stream) return
    const tracks = typeof stream.getAudioTracks === 'function' ? stream.getAudioTracks() : []
    for (const track of tracks) {
      track.enabled = !muted
    }
  }
}

export function createCoughKey(options) {
  return new CoughKey(options)
}
