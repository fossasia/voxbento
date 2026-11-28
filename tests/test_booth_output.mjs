/** Two-way booth switch (#1397): what the booth page and Mission Control show for a booth state. */
import assert from 'node:assert/strict'
import { directionLabel, offersChannel, outputShortcut, outputView, pathOf } from '../portal/static/js/booth-output.js'

const thai = {
  language_code: 'th',
  language: 'Thai',
  return_language_code: 'en',
  return_language: 'English',
  output_language_code: 'th',
  active_interpreter_id: 'ana',
  broadcast_unlocked: false,
  ingest_status: 'disconnected',
  return_taken_by: null,
}

// One-way booths have no switch.
assert.equal(outputView({ ...thai, return_language_code: null }, { participantId: 'ana' }), null)
assert.equal(outputView(null), null)
assert.equal(outputView(undefined), null)

// The active interpreter sees both buttons, the current one pressed.
let view = outputView(thai, { participantId: 'ana' })
assert.equal(view.current, 'primary')
assert.deepEqual(view.buttons, [
  { output: 'primary', label: 'Into Thai', shortcut: 'Alt+1', pressed: true, disabled: false, reason: '' },
  { output: 'return', label: 'Into English', shortcut: 'Alt+2', pressed: false, disabled: false, reason: '' },
])
assert.equal(view.reason, '')
assert.equal(view.status, 'You will interpret into Thai')

// Switched and on air: the status says which channel.
view = outputView({ ...thai, output_language_code: 'en', broadcast_unlocked: true, ingest_status: 'connected' }, { participantId: 'ana' })
assert.equal(view.current, 'return')
assert.deepEqual(view.buttons.map((b) => b.pressed), [false, true])
assert.equal(view.status, 'On air in English')

// Unlocked but not publishing is not on air.
assert.equal(outputView({ ...thai, broadcast_unlocked: true }, { participantId: 'ana' }).status, 'You will interpret into Thai')

// A backup interpreter can't switch; a coordinator can.
view = outputView(thai, { participantId: 'ben' })
assert.ok(view.buttons.every((b) => b.disabled && b.reason === 'Only the interpreter on the mic can switch the channel.'))
assert.equal(view.reason, 'Only the interpreter on the mic can switch the channel.')
assert.ok(outputView(thai, { participantId: 'ben', canSetActive: true }).buttons.every((b) => !b.disabled))
assert.ok(outputView(thai).buttons.every((b) => b.disabled))

// Nobody on the mic.
view = outputView({ ...thai, active_interpreter_id: null }, { canSetActive: true })
assert.equal(view.reason, 'Nobody is interpreting in this booth yet.')
assert.ok(view.buttons.every((b) => b.disabled))

// Another booth holds the return channel: only that button is disabled, with the reason.
view = outputView({ ...thai, return_taken_by: 'Japanese' }, { participantId: 'ana' })
assert.deepEqual(view.buttons.map((b) => b.disabled), [false, true])
assert.equal(view.buttons[1].reason, 'The Japanese booth is interpreting into English right now.')
assert.equal(view.reason, 'The Japanese booth is interpreting into English right now.')

// Names fall back to the codes (e.g. right after a restart).
view = outputView({ ...thai, language: '', return_language: '' }, { participantId: 'ana' })
assert.deepEqual(view.buttons.map((b) => b.label), ['Into TH', 'Into EN'])
assert.equal(outputView({ return_language_code: 'en', active_interpreter_id: 'a' }, { participantId: 'a' }).buttons[0].label, 'Into ')

// Alt+1 / Alt+2 by physical key, never while typing.
const key = (code, extra = {}) => ({ altKey: true, code, target: { tagName: 'BUTTON' }, ...extra })
assert.equal(outputShortcut(key('Digit1')), 'primary')
assert.equal(outputShortcut(key('Digit2')), 'return')
assert.equal(outputShortcut(key('Numpad1')), 'primary')
assert.equal(outputShortcut(key('Numpad2')), 'return')
assert.equal(outputShortcut(key('Digit3')), null)
assert.equal(outputShortcut(key('Digit1', { altKey: false })), null)
for (const modifier of ['ctrlKey', 'metaKey', 'shiftKey']) {
  assert.equal(outputShortcut(key('Digit1', { [modifier]: true })), null)
}
assert.equal(outputShortcut(key('Digit1', { target: null })), 'primary')
assert.equal(outputShortcut(key('Digit1', { target: { tagName: 'INPUT' } })), null)
assert.equal(outputShortcut(key('Digit1', { target: { tagName: 'input', type: 'search' } })), null)
assert.equal(outputShortcut(key('Digit1', { target: { tagName: 'INPUT', type: 'range' } })), 'primary')
assert.equal(outputShortcut(key('Digit1', { target: { tagName: 'INPUT', type: 'checkbox' } })), 'primary')
assert.equal(outputShortcut(key('Digit1', { target: { tagName: 'TEXTAREA' } })), null)
assert.equal(outputShortcut(key('Digit1', { target: { tagName: 'SELECT' } })), null)
assert.equal(outputShortcut(key('Digit1', { target: { isContentEditable: true } })), null)
assert.equal(outputShortcut(key('Digit1', { target: {} })), 'primary')

// The monitor never offers the channel the booth interprets into.
assert.equal(pathOf('https://media.example/ev/1/en/whep'), 'ev/1/en')
assert.equal(pathOf('/ev/1/th%20x/whep'), 'ev/1/th x')
assert.equal(pathOf('https://media.example/whep'), '')
assert.equal(pathOf('http://[bad'), '')
assert.equal(offersChannel('https://media.example/ev/1/en/whep', 'ev/1/th'), true)
assert.equal(offersChannel('https://media.example/ev/1/en/whep', 'ev/1/en'), false)
assert.equal(offersChannel('', 'ev/1/en'), false)

// Mission Control's direction label.
assert.equal(directionLabel(thai), 'Into Thai')
assert.equal(directionLabel({ ...thai, output_language_code: 'en' }), 'Into English')
assert.equal(directionLabel({ ...thai, output_language_code: 'en', return_language: '' }), 'Into EN')
assert.equal(directionLabel({ ...thai, language: '' }), 'Into TH')
assert.equal(directionLabel({ return_language_code: 'en', output_language_code: 'th' }), 'Into ')
assert.equal(directionLabel({ ...thai, return_language_code: null }), '')
assert.equal(directionLabel(null), '')

console.log('Booth output checks passed')
