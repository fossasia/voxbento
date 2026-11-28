/**
 * Unit tests for the hold-to-mute cough key (portal/static/js/cough-key.js, #1395).
 *
 * Run with: node tests/test_cough_key.mjs
 */
import assert from 'node:assert/strict'
import { createCoughKey, isCoughShortcut, isTypingTarget, micTrackEnabled } from '../portal/static/js/cough-key.js'

function recorder(options = {}) {
  const changes = []
  const key = createCoughKey({ ...options, onChange: (held) => changes.push(held) })
  return { key, changes }
}

// Press and release.
{
  const { key, changes } = recorder()
  assert.equal(key.isHeld(), false)
  assert.equal(key.press('key'), true)
  assert.equal(key.isHeld(), true)
  key.release('key')
  assert.equal(key.isHeld(), false)
  assert.deepEqual(changes, [true, false], 'onChange runs once per change')
}

// A second press from the same source (key repeat) changes nothing.
{
  const { key, changes } = recorder()
  key.press('key')
  key.press('key')
  key.release('key')
  assert.deepEqual(changes, [true, false])
}

// Several sources: the cough lasts until the last one lets go.
{
  const { key, changes } = recorder()
  key.press('key')
  key.press('pointer:1')
  key.press('pointer:2')
  key.release('pointer:1')
  assert.equal(key.isHeld(), true, 'another pointer is still down')
  key.release('key')
  assert.equal(key.isHeld(), true)
  key.release('pointer:2')
  assert.equal(key.isHeld(), false)
  assert.deepEqual(changes, [true, false])
}

// Releasing something that isn't held does nothing.
{
  const { key, changes } = recorder()
  key.release('key')
  key.press('pointer:1')
  key.release('pointer:7')
  assert.equal(key.isHeld(), true, 'another pointer cannot end the cough')
  assert.deepEqual(changes, [true])
}

// releaseAll lets go of everything at once, and only reports a change if something was held.
{
  const { key, changes } = recorder()
  key.releaseAll()
  assert.deepEqual(changes, [])
  key.press('key')
  key.press('pointer:1')
  key.releaseAll()
  assert.equal(key.isHeld(), false)
  assert.deepEqual(changes, [true, false])
  key.release('key')
  assert.deepEqual(changes, [true, false], 'a late key-up after releaseAll changes nothing')
}

// A press is refused when the interpreter can't cough (muted, passive, not joined).
{
  let allowed = false
  const { key, changes } = recorder({ canCough: () => allowed })
  assert.equal(key.press('key'), false)
  assert.equal(key.isHeld(), false)
  allowed = true
  assert.equal(key.press('key'), true)
  allowed = false
  assert.equal(key.press('key'), true, 'a source already held stays held')
  assert.equal(key.press('pointer:1'), false, 'a new source is refused')
  key.release('key')
  assert.deepEqual(changes, [true, false])
}

// The microphone track is on only when Mute, the cough key and a handover all allow it.
{
  assert.equal(micTrackEnabled({ micMuted: false, coughHeld: false }), true)
  assert.equal(micTrackEnabled({ micMuted: false, coughHeld: true }), false)
  assert.equal(micTrackEnabled({ micMuted: true, coughHeld: false }), false)
  assert.equal(micTrackEnabled({ micMuted: true, coughHeld: true }), false)
  assert.equal(micTrackEnabled({ micMuted: false, coughHeld: false, relayingOut: true }), false)
}

// Only a bare C is the shortcut, so Ctrl+C / Cmd+C still copy.
{
  const event = (extra) => ({ code: 'KeyC', ctrlKey: false, metaKey: false, altKey: false, shiftKey: false, ...extra })
  assert.equal(isCoughShortcut(event()), true)
  assert.equal(isCoughShortcut(event({ ctrlKey: true })), false)
  assert.equal(isCoughShortcut(event({ metaKey: true })), false)
  assert.equal(isCoughShortcut(event({ altKey: true })), false)
  assert.equal(isCoughShortcut(event({ shiftKey: true })), false)
  assert.equal(isCoughShortcut(event({ code: 'KeyV' })), false)
}

// Typing in the chat box or a field is never a shortcut.
{
  assert.equal(isTypingTarget(null), false)
  assert.equal(isTypingTarget({ tagName: 'BUTTON' }), false)
  assert.equal(isTypingTarget({ tagName: 'BODY' }), false)
  for (const tagName of ['INPUT', 'TEXTAREA', 'SELECT']) {
    assert.equal(isTypingTarget({ tagName }), true, tagName)
  }
  assert.equal(isTypingTarget({ tagName: 'DIV', isContentEditable: true }), true)
}

console.log('Cough key checks passed')
