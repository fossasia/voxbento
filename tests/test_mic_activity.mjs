/**
 * Unit tests for following the real microphone state (portal/static/js/mic-activity.js, #1214).
 *
 * Run with: node tests/test_mic_activity.mjs
 */
import { isTrackSilenced, micIsActive, watchMicTracks } from '../portal/static/js/mic-activity.js'

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

/** A MediaStreamTrack stand-in that lets the test play the device's part. */
class FakeTrack {
  constructor({ muted = false, readyState = 'live' } = {}) {
    this.muted = muted
    this.readyState = readyState
    this.listeners = new Map()
  }

  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set())
    this.listeners.get(type).add(fn)
  }

  removeEventListener(type, fn) {
    this.listeners.get(type)?.delete(fn)
  }

  count() {
    return [...this.listeners.values()].reduce((sum, fns) => sum + fns.size, 0)
  }

  emit(type) {
    for (const fn of this.listeners.get(type) ?? []) fn({ type })
  }

  deviceMute() {
    this.muted = true
    this.emit('mute')
  }

  deviceUnmute() {
    this.muted = false
    this.emit('unmute')
  }

  unplug() {
    this.readyState = 'ended'
    this.emit('ended')
  }
}

const streamOf = (...tracks) => ({ getAudioTracks: () => tracks })

// What counts as silenced
assert(isTrackSilenced(new FakeTrack()) === false, 'a live, unmuted track is not silenced')
assert(isTrackSilenced(new FakeTrack({ muted: true })) === true, 'a track muted by the device is silenced')
assert(isTrackSilenced(new FakeTrack({ readyState: 'ended' })) === true, 'an ended track is silenced')

// A muted device is reported, and so is the unmute
{
  const track = new FakeTrack()
  const changes = []
  const watcher = watchMicTracks(streamOf(track), (muted) => changes.push(muted))
  assert(watcher.deviceMuted() === false, 'a live microphone starts as not muted')
  track.deviceMute()
  assert(watcher.deviceMuted() === true, 'the device mute is picked up')
  assert(changes.length === 1 && changes[0] === true, 'the device mute is reported once')
  track.deviceUnmute()
  assert(watcher.deviceMuted() === false, 'the device unmute is picked up')
  assert(changes.length === 2 && changes[1] === false, 'the device unmute is reported')
}

// A device that is already muted when the stream opens
{
  const watcher = watchMicTracks(streamOf(new FakeTrack({ muted: true })), () => {})
  assert(watcher.deviceMuted() === true, 'a microphone that starts muted is reported as muted')
}

// Unplugging the device ends the track
{
  const track = new FakeTrack()
  const changes = []
  const watcher = watchMicTracks(streamOf(track), (muted) => changes.push(muted))
  track.unplug()
  assert(watcher.deviceMuted() === true, 'an unplugged microphone counts as muted')
  assert(changes.length === 1 && changes[0] === true, 'unplugging is reported')
}

// Only a change is reported
{
  const track = new FakeTrack()
  const changes = []
  watchMicTracks(streamOf(track), (muted) => changes.push(muted))
  track.emit('mute')
  track.emit('unmute')
  assert(changes.length === 0, 'events that do not change the state are not reported')
  track.muted = true
  track.emit('mute')
  track.emit('mute')
  assert(changes.length === 1, 'a repeated mute is reported once')
}

// Several tracks: muted only when none can deliver audio
{
  const first = new FakeTrack()
  const second = new FakeTrack()
  const watcher = watchMicTracks(streamOf(first, second), () => {})
  first.deviceMute()
  assert(watcher.deviceMuted() === false, 'one track still delivering audio keeps the microphone on')
  second.deviceMute()
  assert(watcher.deviceMuted() === true, 'every track muted means the microphone is muted')
}

// A stream without audio has nothing to transmit
assert(watchMicTracks(streamOf(), () => {}).deviceMuted() === true, 'a stream without audio tracks counts as muted')

// stop() removes every listener
{
  const track = new FakeTrack()
  const changes = []
  const watcher = watchMicTracks(streamOf(track), (muted) => changes.push(muted))
  assert(track.count() === 3, 'the watcher listens to mute, unmute and ended')
  watcher.stop()
  assert(track.count() === 0, 'stop() removes the listeners')
  track.deviceMute()
  assert(changes.length === 0, 'a stopped watcher reports nothing')
}

// Speaking needs both the Mute button off and the device not muted
assert(micIsActive({ uiMuted: false, deviceMuted: false }) === true, 'unmuted and live is speaking')
assert(micIsActive({ uiMuted: true, deviceMuted: false }) === false, 'the Mute button silences it')
assert(micIsActive({ uiMuted: false, deviceMuted: true }) === false, 'the device mute silences it')
assert(micIsActive({ uiMuted: true, deviceMuted: true }) === false, 'both muted is not speaking')

console.log(`\n${passed} passed, ${failed} failed`)
if (failed) process.exit(1)
