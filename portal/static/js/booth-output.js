/**
 * Two-way booths (#1397): which channel a booth interprets into, and its two switch buttons.
 *
 * A two-way booth interprets into its own language, or back into the room's spoken
 * language, for example a Thai booth answering a question asked in Thai into English.
 * The server decides who may switch and which path the interpreter publishes to; this
 * module only turns the booth state into what the booth page shows. No DOM here.
 */

/**
 * The switch for a booth state, or null for a booth that is not two-way.
 *
 * @param {object} state booth:state from the server
 * @param {{participantId?: string|null, canSetActive?: boolean}} viewer
 */
export function outputView(state, { participantId = null, canSetActive = false } = {}) {
  const returnCode = state?.return_language_code
  if (!returnCode) return null
  const ownName = state.language || String(state.language_code || '').toUpperCase()
  const returnName = state.return_language || returnCode.toUpperCase()
  const current = state.output_language_code === returnCode ? 'return' : 'primary'
  const allowed = Boolean(participantId && state.active_interpreter_id === participantId) || canSetActive

  let reason = ''
  if (!state.active_interpreter_id) reason = 'Nobody is interpreting in this booth yet.'
  else if (!allowed) reason = 'Only the interpreter on the mic can switch the channel.'
  const takenReason = state.return_taken_by
    ? `The ${state.return_taken_by} booth is interpreting into ${returnName} right now.`
    : ''

  const buttons = [
    { output: 'primary', label: `Into ${ownName}`, shortcut: 'Alt+1' },
    { output: 'return', label: `Into ${returnName}`, shortcut: 'Alt+2' },
  ].map((button) => {
    const pressed = button.output === current
    const blocked = reason || (button.output === 'return' && !pressed ? takenReason : '')
    return { ...button, pressed, disabled: Boolean(blocked), reason: blocked }
  })

  const into = current === 'return' ? returnName : ownName
  const onAir = state.broadcast_unlocked === true && state.ingest_status === 'connected'
  return {
    current,
    buttons,
    reason: reason || takenReason,
    status: onAir ? `On air in ${into}` : `You will interpret into ${into}`,
  }
}

/**
 * The output an Alt+1 / Alt+2 key press asks for, or null. Uses `code`, because Alt changes
 * `key` on macOS. Ignored while typing, so the chat box keeps its own shortcuts.
 *
 * @param {{altKey: boolean, ctrlKey?: boolean, metaKey?: boolean, shiftKey?: boolean, code: string, target?: object}} event
 */
export function outputShortcut(event) {
  if (!event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return null
  if (isTextField(event.target)) return null
  if (event.code === 'Digit1' || event.code === 'Numpad1') return 'primary'
  if (event.code === 'Digit2' || event.code === 'Numpad2') return 'return'
  return null
}

function isTextField(target) {
  if (!target) return false
  if (target.isContentEditable) return true
  const tag = String(target.tagName || '').toUpperCase()
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (tag !== 'INPUT') return false
  const type = String(target.type || 'text').toLowerCase()
  return !['button', 'checkbox', 'radio', 'range', 'submit', 'reset', 'color', 'file'].includes(type)
}

/** The MediaMTX path in a WHEP or WHIP URL (`…/<path>/whep`), or ''. */
export function pathOf(url) {
  try {
    const parts = new URL(url, 'http://localhost').pathname.split('/').filter(Boolean)
    return parts.length > 1 ? parts.slice(0, -1).map(decodeURIComponent).join('/') : ''
  } catch {
    return ''
  }
}

/**
 * Whether the monitor can offer a channel: never the one this booth interprets into,
 * so an interpreter never hears their own voice back.
 */
export function offersChannel(whepUrl, outputPath) {
  const path = pathOf(whepUrl)
  return Boolean(path) && path !== outputPath
}

/** Mission Control's direction label for a two-way booth ("Into English"), or ''. */
export function directionLabel(state) {
  if (!state?.return_language_code) return ''
  const returnName = state.return_language || state.return_language_code.toUpperCase()
  return state.output_language_code === state.return_language_code
    ? `Into ${returnName}`
    : `Into ${state.language || String(state.language_code || '').toUpperCase()}`
}
