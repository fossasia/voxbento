/**
 * Unit tests for the program sender source helpers (portal/static/js/program-sender-source.js).
 *
 * Run with: node tests/test_program_sender_source.mjs
 */
import {
  isTabSurface,
  normalizeSource,
  pickTabAudio,
  RAW_AUDIO,
  SOURCES,
  tabCaptureOptions,
  tabCaptureSupported,
} from '../portal/static/js/program-sender-source.js'

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

function fakeTrack() {
  return { stopped: false, stop() { this.stopped = true } }
}

function fakeStream(videoTracks, audioTracks) {
  return { getVideoTracks: () => videoTracks, getAudioTracks: () => audioTracks }
}

// Source names
assert(normalizeSource('tab') === 'tab', 'tab stays tab')
assert(normalizeSource('device') === 'device', 'device stays device')
assert(normalizeSource('x') === 'device', 'an unknown source falls back to device')
assert(normalizeSource(undefined) === 'device', 'a missing source falls back to device')
assert(SOURCES.TAB === 'tab' && SOURCES.DEVICE === 'device', 'source names are stable storage values')

// Capture options
const options = tabCaptureOptions()
assert(options.video === true, 'a video request is made because Chromium needs one')
assert(options.audio.echoCancellation === false, 'tab audio bypasses echo cancellation')
assert(options.audio.noiseSuppression === false && options.audio.autoGainControl === false, 'tab audio bypasses noise suppression and gain control')
assert(options.audio.suppressLocalAudioPlayback === false, 'the tab stays audible on this computer')
assert(options.selfBrowserSurface === 'exclude', 'the sender tab is not offered')
assert(options.systemAudio === 'exclude', 'system audio is not offered')
assert(Object.isFrozen(RAW_AUDIO), 'the shared raw-audio constraints cannot be mutated')

// Support detection
assert(tabCaptureSupported(undefined) === false, 'no mediaDevices means no tab capture')
assert(tabCaptureSupported({}) === false, 'mediaDevices without getDisplayMedia means no tab capture')
assert(tabCaptureSupported({ getDisplayMedia() {} }) === true, 'getDisplayMedia means tab capture works')

// Surface check (the picker options are only hints)
function surfaceStream(displaySurface) {
  return fakeStream([{ getSettings: () => ({ displaySurface }) }], [])
}
assert(isTabSurface(surfaceStream('browser')) === true, 'a browser tab is accepted')
assert(isTabSurface(surfaceStream('window')) === false, 'a shared window is rejected')
assert(isTabSurface(surfaceStream('monitor')) === false, 'a shared screen is rejected')
assert(isTabSurface(surfaceStream(undefined)) === true, 'a browser that does not report the surface is accepted')
assert(isTabSurface(fakeStream([], [])) === true, 'a stream without video settings is accepted')

// Picking the audio track
const video = fakeTrack()
const audio = fakeTrack()
assert(pickTabAudio(fakeStream([video], [audio])) === audio, 'the shared audio track is returned')
assert(video.stopped === true, 'the video track is stopped')
assert(audio.stopped === false, 'the audio track keeps running')

const lonelyVideo = fakeTrack()
assert(pickTabAudio(fakeStream([lonelyVideo], [])) === null, 'no shared audio returns null')
assert(lonelyVideo.stopped === true, 'the video track is stopped even without audio')

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed > 0 ? 1 : 0)
