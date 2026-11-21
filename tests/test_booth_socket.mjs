/** Exercise the production booth socket callbacks with controlled transport and timers. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { createBackoff, finalCloseMessage } from '../portal/static/js/booth-reconnect.js'
import { micIsActive, watchMicTracks } from '../portal/static/js/mic-activity.js'

const source = readFileSync(new URL('../portal/static/js/interpreter-booth.js', import.meta.url), 'utf8')
  .replace(/^import .* from '\.\/booth-reconnect.js'\n/, '')
  .replace(/^import .* from '\.\/mic-activity.js'\n/, '')
  .replace('boot().catch((error) => {', 'Promise.resolve().catch((error) => {')

function harness({ getUserMedia = async () => ({ getAudioTracks: () => [] }) } = {}) {
  const timers = new Map()
  const sockets = []
  const events = []
  let timerId = 0
  class Socket {
    static OPEN = 1
    constructor() { this.handlers = {}; this.sent = []; sockets.push(this) }
    addEventListener(type, fn) { this.handlers[type] = fn }
    send(data) { this.sent.push(JSON.parse(data)) }
    open() { this.readyState = 1; this.handlers.open() }
    close(code = 1006) { this.readyState = 3; this.handlers.close({ code }) }
    message(data) { this.handlers.message({ data: JSON.stringify(data) }) }
  }
  const context = vm.createContext({
    console, URL, Promise, WebSocket: Socket, createBackoff, finalCloseMessage, micIsActive, watchMicTracks,
    document: { getElementById: (id) => id === 'interpreter-portal'
      ? { dataset: { boothId: 'test-1-en', eventSlug: 'test', roomId: '1' } } : null },
    localStorage: { getItem: () => null },
    window: { location: { protocol: 'https:', host: 'example.test' } },
    setTimeout: (fn, delay) => { timers.set(++timerId, { fn, delay }); return timerId },
    clearTimeout: (id) => timers.delete(id),
    navigator: { mediaDevices: { getUserMedia } },
    events,
  })
  vm.runInContext(source + `
    render = () => {};
    setBadge = (_, label) => events.push(label);
    showError = (message) => events.push(message);
    acquireJwt = async () => {};
    applyBoothState = (payload) => {
      state.activeInterpreterId = payload.active_interpreter_id;
      state.broadcastUnlocked = payload.broadcast_unlocked;
    };
    joinMonitoringFeed = () => events.push('monitor-started');
    stopLiveIngest = async () => { events.push('publish-stopped'); state.ingestConnected = false; };
    renderMicControls = () => {};
    startMicMeter = () => {};
    globalThis.api = { state, connectWebSocket, reconnectBackoff, watchMicDevice, currentMicActive, ensureMicStream };
  `, context)
  return { ...context.api, timers, sockets, events }
}

const h = harness()
h.state.joinRequested = true
const connected = h.connectWebSocket()
h.sockets[0].open()
await connected
assert.equal(h.sockets[0].sent.length, 1, 'opening a socket joins once')
h.sockets[0].message({ type: 'booth:joined', participant_id: 'original', state: {
  active_interpreter_id: 'original', broadcast_unlocked: true,
} })
assert.equal(h.state.participantId, 'original')
h.state.ingestConnected = true
h.sockets[0].close()
assert.equal(h.state.joined, false)
assert.equal(h.timers.size, 1, 'one retry after a disconnect')
const retry = [...h.timers.values()][0]
h.timers.clear()
await retry.fn()
h.sockets[1].open()
assert.equal(h.sockets[1].sent[0].participant_id, 'original', 'rejoin sends previous identity')
h.sockets[1].message({ type: 'booth:joined', participant_id: 'original', state: {
  active_interpreter_id: 'original', broadcast_unlocked: true,
} })
assert.equal(h.sockets[1].sent.at(-1).ingest_connected, true, 'surviving ingest is reported again')
assert.equal(h.events.filter((e) => e === 'monitor-started').length, 1, 'monitor is not reloaded')

h.sockets[0].message({ type: 'booth:joined', participant_id: 'stale', state: {} })
assert.equal(h.state.participantId, 'original', 'messages from an old socket are ignored')
h.sockets[0].close()
assert.equal(h.state.ws, h.sockets[1], 'old close cannot replace the current socket')

h.sockets[1].message({ type: 'booth:joined', participant_id: 'new-id', state: {
  active_interpreter_id: 'backup', broadcast_unlocked: true,
} })
assert.ok(h.events.includes('publish-stopped'), 'rejoin cannot keep publishing over the backup')

for (const code of [4001, 4003]) {
  const revoked = harness()
  revoked.state.joinRequested = true
  const opening = revoked.connectWebSocket()
  revoked.sockets[0].open()
  await opening
  const track = { enabled: true }
  revoked.state.micStream = { getAudioTracks: () => [track] }
  revoked.state.micMuted = false
  revoked.state.ingestConnected = true
  revoked.sockets[0].close(code)
  assert.equal(revoked.state.stopReconnecting, true)
  assert.equal(revoked.timers.size, 0, `${code} must not retry`)
  assert.equal(track.enabled, false, `${code} immediately mutes the microphone`)
  assert.ok(revoked.events.includes('publish-stopped'), `${code} stops publishing`)
}

const pending = harness()
const initial = pending.connectWebSocket()
const timeout = [...pending.timers.values()][0]
timeout.fn()
await initial
pending.state.joinRequested = true
pending.sockets[0].open()
assert.equal(pending.sockets[0].sent.length, 1, 'a slow initial connection still joins')

const denied = harness()
denied.state.joinRequested = true
for (let attempt = 0; attempt < 3; attempt++) {
  const p = denied.connectWebSocket()
  denied.sockets.at(-1).open()
  await p
  denied.sockets.at(-1).close()
  const delay = [...denied.timers.values()][0].delay
  assert.ok(delay >= 500 * (2 ** attempt), 'opening without joining must not reset backoff')
  denied.timers.clear()
  denied.state.reconnectTimer = null
}
// Muting the device itself, not the booth's Mute button, must reach the other participants (#1214).
{
  class DeviceTrack {
    constructor() { this.muted = false; this.readyState = 'live'; this.listeners = {} }
    addEventListener(type, fn) { this.listeners[type] = fn }
    removeEventListener(type) { delete this.listeners[type] }
    deviceMute() { this.muted = true; this.listeners.mute() }
    deviceUnmute() { this.muted = false; this.listeners.unmute() }
  }
  const mic = harness()
  mic.state.joinRequested = true
  const opening = mic.connectWebSocket()
  mic.sockets[0].open()
  await opening
  mic.sockets[0].message({ type: 'booth:joined', participant_id: 'me', state: {
    active_interpreter_id: 'me', broadcast_unlocked: true,
  } })
  mic.state.micMuted = false
  mic.state.ingestConnected = true
  const track = new DeviceTrack()
  mic.watchMicDevice({ getAudioTracks: () => [track] })
  const lastSent = () => mic.sockets[0].sent.at(-1)

  track.deviceMute()
  assert.equal(mic.state.micDeviceMuted, true, 'the device mute is remembered')
  assert.equal(mic.currentMicActive(), false, 'a device-muted microphone is not speaking')
  assert.deepEqual(lastSent(), { type: 'booth:update-state', mic_active: false }, 'the device mute is sent to the booth')

  track.deviceUnmute()
  assert.deepEqual(lastSent(), { type: 'booth:update-state', mic_active: true }, 'the device unmute is sent to the booth')

  mic.state.micMuted = true
  const before = mic.sockets[0].sent.length
  track.deviceMute()
  track.deviceUnmute()
  assert.deepEqual(lastSent(), { type: 'booth:update-state', mic_active: false }, 'the Mute button still wins over the device')
  assert.equal(mic.sockets[0].sent.length, before + 2, 'each change is sent')

  mic.state.micMuted = false
  mic.state.ingestConnected = false
  const idle = mic.sockets[0].sent.length
  track.deviceMute()
  assert.equal(mic.sockets[0].sent.length, idle, 'nothing is sent while the booth is not publishing')

  // Handing over: the stream is still up while it stops, but the server would refuse
  // mic_active from someone who is no longer the active interpreter.
  track.deviceUnmute()
  mic.state.ingestConnected = true
  mic.state.activeInterpreterId = 'someone-else'
  const handingOver = mic.sockets[0].sent.length
  track.deviceMute()
  track.deviceUnmute()
  assert.equal(mic.sockets[0].sent.length, handingOver, 'nothing is sent after losing the active role')
}

// Going live and pressing Unmute at the same moment must open the microphone once, so the
// published stream is the one being watched for device mutes.
{
  const requests = []
  const mic = harness({
    getUserMedia: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
  })
  const first = mic.ensureMicStream()
  const second = mic.ensureMicStream()
  assert.equal(requests.length, 1, 'overlapping callers share one microphone request')
  const stream = { getAudioTracks: () => [] }
  requests[0].resolve(stream)
  assert.equal(await first, stream)
  assert.equal(await second, stream, 'both callers get the same stream')
  assert.equal(await mic.ensureMicStream(), stream, 'later calls reuse it')
  assert.equal(requests.length, 1)

  const denied = harness({
    getUserMedia: () => new Promise((resolve, reject) => requests.push({ resolve, reject })),
  })
  const refused = denied.ensureMicStream()
  requests.at(-1).reject(new Error('Permission denied'))
  await assert.rejects(refused, /Permission denied/)
  const retry = denied.ensureMicStream()
  assert.equal(requests.length, 3, 'a refused request can be tried again')
  requests.at(-1).resolve(stream)
  assert.equal(await retry, stream)
}

console.log('Booth socket lifecycle checks passed')
