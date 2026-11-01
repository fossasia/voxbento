/**
 * Unit tests for the program sender source helpers (portal/static/js/program-sender-source.js).
 *
 * Run with: node tests/test_program_sender_source.mjs
 */
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
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
assert(tabCaptureSupported({ getDisplayMedia() {} }) === false, 'getDisplayMedia without tab-audio constraints (Firefox, Safari) means no tab capture')
assert(
  tabCaptureSupported({ getDisplayMedia() {}, getSupportedConstraints: () => ({ suppressLocalAudioPlayback: true }) }) === true,
  'Chromium getDisplayMedia means tab capture works',
)

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

const senderCode = readFileSync(new URL('../portal/static/js/program-sender.js', import.meta.url), 'utf8')
const openInputCode = senderCode.slice(senderCode.indexOf('async function openInput()'), senderCode.indexOf('/** @throws {SourceError}'))
const recoveryCode = senderCode.slice(senderCode.indexOf('async function handleInputLost('), senderCode.indexOf('function startMeter()'))

for (const released of [false, true]) {
  let resolveCapture
  const capture = new Promise(resolve => { resolveCapture = resolve })
  let markRequested
  const requested = new Promise(resolve => { markRequested = resolve })
  const track = { ...fakeTrack(), clone() { return fakeTrack() }, addEventListener() {} }
  const acquired = { getTracks: () => [track], getAudioTracks: () => [track] }
  const context = {
    inputGeneration: 1,
    deviceSelect: { value: '' },
    RAW_AUDIO,
    navigator: { mediaDevices: { getUserMedia() { markRequested(); return capture } } },
    stream: null,
    meterStream: null,
    muted: false,
    sending: true,
    testing: false,
    preferredDevice: '',
    pc: null,
    MediaStream: class { constructor(tracks) { this.tracks = tracks } },
    startMeter() {},
    listDevices: async () => {},
    saveDevice() {},
    states: [],
    setState(state, message) { context.states.push(message) },
    releaseInput() { context.inputGeneration += 1; context.stream = null },
  }
  runInNewContext(`${openInputCode}\n${recoveryCode}`, context)
  const recovery = context.handleInputLost(1)
  await requested
  if (released) context.releaseInput()
  resolveCapture(acquired)
  await recovery
  assert(track.stopped === released, released ? 'released pending capture stops its newly acquired tracks' : 'current recovery keeps the acquired track running')
  assert(context.stream === (released ? null : acquired), released ? 'released pending capture is not adopted' : 'current recovery adopts the acquired input')
  assert(context.states.includes('Audio input restored.') === !released, released ? 'discarded recovery does not report restoration' : 'current recovery reports restoration')
}

console.log(`\n${passed} passed, ${failed} failed`)
process.exit(failed > 0 ? 1 : 0)
