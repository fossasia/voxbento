/** Mission Control: choosing who is live and the booth socket's reconnect policy (#1338). */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { createBackoff, finalCloseMessage } from '../portal/static/js/booth-reconnect.js'

const source = readFileSync(new URL('../portal/static/js/mission-control.js', import.meta.url), 'utf8')
  .replace(/^import .* from '\.\/booth-reconnect\.js';\n/m, '')
  .replace(/^import .* from '\.\/confirm-dialog\.js';\n/m, '')

const BOOTH = 'conf-1-fr'
const A = 'aaaa'
const B = 'bbbb'

function boothState({ active = A, aOnAir = true, handoff = 'idle' } = {}) {
  return {
    booth_id: BOOTH,
    room_id: 1,
    language: 'French',
    language_code: 'fr',
    active_interpreter_id: active,
    handoff_state: handoff,
    ingest_status: aOnAir ? 'connected' : 'disconnected',
    broadcast_unlocked: true,
    participants: [
      { participant_id: A, display_name: 'Ana', can_go_live: true, mic_active: aOnAir, ingest_connected: aOnAir, connected: true },
      { participant_id: B, display_name: 'Ben', can_go_live: true, mic_active: false, ingest_connected: false, connected: true },
    ],
  }
}

function harness({ confirmAnswer = true } = {}) {
  const sockets = []
  const timers = []
  const toasts = []
  const confirms = []
  class Socket {
    static OPEN = 1
    constructor(url) { this.url = url; this.sent = []; this.readyState = 0; sockets.push(this) }
    send(data) { this.sent.push(JSON.parse(data)) }
    open() { this.readyState = 1; this.onopen() }
    close(code = 1006) { this.readyState = 3; this.onclose({ code }) }
    message(data) { this.onmessage({ data: JSON.stringify(data) }) }
  }
  const context = vm.createContext({
    console,
    JSON,
    WebSocket: Socket,
    createBackoff: () => createBackoff({ random: () => 1 }),
    finalCloseMessage,
    confirmChange: async (message) => { confirms.push(message); return confirmAnswer },
    document: {
      readyState: 'loading',
      addEventListener: () => {},
      getElementById: () => null,
    },
    window: { location: { protocol: 'https:', host: 'example.test' } },
    setTimeout: (fn, delay) => { timers.push({ fn, delay }) },
  })
  vm.runInContext(`${source}
    renderCard = () => {};
    showToast = (message, type) => toasts.push([message, type]);
    globalThis.mc = { boothMap, connectWs, makeLive };
  `, Object.assign(context, { toasts }))
  const { boothMap, connectWs, makeLive } = context.mc
  boothMap.set(BOOTH, {
    state: boothState(),
    ws: null,
    whep: null,
    audioElement: null,
    currentVolume: 0,
    pendingFocusId: '',
    backoff: context.createBackoff(),
    closedMessage: '',
    switchingTo: '',
  })
  connectWs(BOOTH)
  return { sockets, timers, toasts, confirms, entry: boothMap.get(BOOTH), makeLive, connectWs }
}

// Make live asks first when someone is on air, then sends what the coordinator saw.
{
  const h = harness()
  h.sockets[0].open()
  await h.makeLive(BOOTH, B)
  assert.equal(h.confirms.length, 1)
  assert.match(h.confirms[0], /Ana is on air in French\. Make Ben live instead\?/)
  assert.deepEqual(h.sockets[0].sent, [{ type: 'booth:set-active', target_id: B, expected_active_id: A }])
  assert.equal(h.entry.switchingTo, B)

  // A second click while the first is pending sends nothing.
  await h.makeLive(BOOTH, B)
  assert.equal(h.sockets[0].sent.length, 1)

  h.sockets[0].message({ type: 'booth:state', state: boothState({ active: B, aOnAir: false }) })
  assert.equal(h.entry.switchingTo, '')
  assert.equal(h.entry.state.active_interpreter_id, B)
}

// Cancelling the dialog changes nothing.
{
  const h = harness({ confirmAnswer: false })
  h.sockets[0].open()
  await h.makeLive(BOOTH, B)
  assert.deepEqual(h.sockets[0].sent, [])
  assert.equal(h.entry.switchingTo, '')
}

// Nobody on air: no dialog, and expected_active_id says who is live (or null).
{
  const h = harness()
  h.entry.state = boothState({ active: null, aOnAir: false })
  h.sockets[0].open()
  await h.makeLive(BOOTH, B)
  assert.equal(h.confirms.length, 0)
  assert.deepEqual(h.sockets[0].sent, [{ type: 'booth:set-active', target_id: B, expected_active_id: null }])
}

// The expected ID is the one seen at click time, even if state changes while the dialog is open.
{
  const h = harness()
  h.sockets[0].open()
  const pending = h.makeLive(BOOTH, B)
  h.sockets[0].message({ type: 'booth:state', state: boothState({ active: B, aOnAir: false }) })
  await pending
  assert.equal(h.sockets[0].sent[0].expected_active_id, A)
}

// A refusal is shown and frees the button.
{
  const h = harness()
  h.sockets[0].open()
  await h.makeLive(BOOTH, B)
  h.sockets[0].message({ type: 'booth:error', message: 'Who is live in this booth just changed. Check the booth and try again.' })
  assert.equal(h.entry.switchingTo, '')
  const [message, type] = h.toasts.at(-1)
  assert.equal(message, 'Who is live in this booth just changed. Check the booth and try again.')
  assert.equal(type, 'error')
}

// Not connected: nothing is sent.
{
  const h = harness()
  await h.makeLive(BOOTH, B)
  assert.deepEqual(h.sockets[0].sent, [])
  assert.match(h.toasts.at(-1)[0], /Can't reach this booth/)
}

// Dropped connections back off, doubling up to the cap, and reset once connected.
{
  const h = harness()
  h.sockets[0].close(1006)
  h.timers.shift().fn()
  h.sockets[1].close(1006)
  h.timers.shift().fn()
  h.sockets[2].close(1012)
  assert.equal(h.timers.length, 1)
  assert.equal(h.timers[0].delay, 4000)
  h.timers.shift().fn()
  h.sockets[3].open()
  h.sockets[3].close(1006)
  assert.equal(h.timers[0].delay, 1000)
}

// A refused session stops retrying and says why.
for (const [code, text] of [[4003, /no longer have access/], [4001, /session has expired/]]) {
  const h = harness()
  h.sockets[0].open()
  h.sockets[0].close(code)
  assert.equal(h.timers.length, 0)
  assert.equal(h.sockets.length, 1)
  assert.match(h.entry.closedMessage, text)
}

console.log('mission control tests passed')
