/** The booth page's two-way switch (#1397), run against the production interpreter-booth.js. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { createBackoff, finalCloseMessage } from '../portal/static/js/booth-reconnect.js'
import { offersChannel, outputShortcut, outputView } from '../portal/static/js/booth-output.js'
import { micIsActive, watchMicTracks } from '../portal/static/js/mic-activity.js'

const sourceUrl = new URL('../portal/static/js/interpreter-booth.js', import.meta.url)
// Every edit keeps the text the same length, so coverage offsets still match the file:
// imports become spaces, and the page's boot() call becomes nope(), which does nothing.
const source = readFileSync(sourceUrl, 'utf8')
  .replace(/^import .*$/gm, (line) => ' '.repeat(line.length))
  .replace('boot().catch((error) => {', 'nope().catch((error) => {')
// One script for every harness, so coverage adds up across them.
const script = new vm.Script(source + `
    renderParticipants = () => {}
    renderChat = () => {}
    renderMicControls = () => {}
    renderHandoverButton = () => {}
    renderLiveBadge = () => {}
    renderRoleControls = () => {}
    stopMicMeter = () => {}
    joinMonitoringFeed = () => events.push('monitor-rejoined')
    ensureMicStream = async () => { state.micStream = { getAudioTracks: () => [] } }
    waitForIceGathering = async () => {}
    startTranscription = async () => {}
    showError = (message) => events.push(message)
    var events = []
    state.ws = { readyState: 1, send: recordSend }
    globalThis.api = {
      state, applyBoothState, handleServerMessage, renderOutputSwitch, requestOutput, syncMonitorOptions,
      bindEventHandlers, doWhipIngest, startBoothAudioListening, attemptRelayStart, events: () => events,
    }
`, { filename: sourceUrl.pathname })

class FakeElement {
  constructor(id) {
    this.id = id
    this.dataset = {}
    this.style = {}
    this.attributes = {}
    this.listeners = {}
    this.hidden = false
    this.disabled = false
    this.textContent = ''
    this.value = ''
    this.options = []
    this.classList = { toggle() {}, add() {}, remove() {}, contains: () => false }
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn) }
  setAttribute(name, value) { this.attributes[name] = value }
  getAttribute(name) { return this.attributes[name] }
  removeAttribute(name) { delete this.attributes[name] }
  querySelector(selector) { return this.children?.[selector] || null }
  click() { (this.listeners.click || []).forEach((fn) => fn()) }
}

function harness({ relayWhepUrl = '', whepUrl = 'https://media.test/ev/1/th/whep', missing = [] } = {}) {
  const elements = new Map()
  const byId = (id) => {
    if (missing.includes(id)) return null
    if (!elements.has(id)) elements.set(id, new FakeElement(id))
    return elements.get(id)
  }
  const portalEl = byId('interpreter-portal')
  Object.assign(portalEl.dataset, {
    boothId: 'ev-1-th', eventSlug: 'ev', roomId: '1', languageCode: 'th',
    defaultChannel: 'ev/1/th', whipBase: 'https://media.test', whepUrl,
    relayWhepUrl,
  })
  const relayOption = new FakeElement('relay-option')
  byId('monitor-source').children = { "option[value='relay']": relayOption }
  byId('monitor-source').value = 'relay'

  const timers = []
  const sent = []
  const fetches = []
  const whep = []
  const documentListeners = {}
  const context = vm.createContext({
    console, URL, Promise, Error, nope: async () => {},
    WebSocket: { OPEN: 1 },
    createBackoff, finalCloseMessage, micIsActive, watchMicTracks, offersChannel, outputShortcut, outputView,
    document: {
      getElementById: byId,
      addEventListener: (type, fn) => { (documentListeners[type] ||= []).push(fn) },
      activeElement: null,
    },
    localStorage: { getItem: () => null, setItem() {} },
    window: {
      location: { protocol: 'https:', host: 'example.test' },
      addEventListener() {},
      setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length },
      clearTimeout() {},
    },
    setTimeout: (fn, delay) => { timers.push({ fn, delay }); return timers.length },
    clearTimeout() {},
    navigator: { mediaDevices: { addEventListener() {}, getUserMedia: async () => ({ getAudioTracks: () => [] }) } },
    fetch: async (url, options = {}) => {
      fetches.push({ url, method: options.method || 'GET', authorization: options.headers?.Authorization })
      return { ok: true, headers: { get: () => null }, text: async () => 'answer-sdp', json: async () => ({}) }
    },
    RTCPeerConnection: class {
      constructor() { this.localDescription = { sdp: 'offer-sdp' } }
      addTrack() {}
      addEventListener() {}
      async createOffer() { return {} }
      async setLocalDescription() {}
      async setRemoteDescription() {}
      close() {}
    },
    createWhepClient: () => ({
      start: (options) => whep.push(['start', options.whepUrl]),
      stop: () => whep.push(['stop']),
    }),
  })
  Object.assign(context, { recordSend: (data) => sent.push(JSON.parse(data)) })
  script.runInContext(context)
  return { ...context.api, byId, timers, sent, fetches, whep, documentListeners, relayOption }
}

const thai = (fields = {}) => ({
  booth_id: 'ev-1-th',
  language_code: 'th',
  language: 'Thai',
  mediamtx_path: 'ev/1/th',
  return_language_code: 'en',
  return_language: 'English',
  output_language_code: 'th',
  output_mediamtx_path: 'ev/1/th',
  active_interpreter_id: 'ana',
  broadcast_unlocked: false,
  ingest_status: 'disconnected',
  return_taken_by: null,
  participants: [{ participant_id: 'ana', can_set_active: false }],
  chat_messages: [],
  ...fields,
})

/** Run the short timers (retries) and let promises settle; the 5 s ticket timeout is left alone. */
async function flush(h) {
  for (let i = 0; i < 20; i += 1) {
    const due = h.timers.filter((t) => t.delay < 1000)
    h.timers.splice(0, h.timers.length, ...h.timers.filter((t) => t.delay >= 1000))
    due.forEach((t) => t.fn())
    await new Promise((resolve) => setImmediate(resolve))
  }
}

// ── The switch renders from the booth state ────────────────────────────────
{
  const h = harness()
  h.state.participantId = 'ana'
  h.state.joined = true
  h.handleServerMessage({ type: 'booth:state', state: thai() })
  const sw = h.byId('output-switch')
  assert.equal(sw.hidden, false)
  assert.equal(h.byId('output-primary').textContent, 'Into Thai')
  assert.equal(h.byId('output-primary').attributes['aria-pressed'], 'true')
  assert.equal(h.byId('output-return').textContent, 'Into English')
  assert.equal(h.byId('output-return').attributes['aria-pressed'], 'false')
  assert.equal(h.byId('output-return').title, 'Into English (Alt+2)')
  assert.equal(h.byId('output-status').textContent, 'You will interpret into Thai')

  // Clicking the pressed one sends nothing; the other one asks the server.
  h.bindEventHandlers()
  h.byId('output-primary').click()
  assert.deepEqual(h.sent, [])
  h.byId('output-return').click()
  assert.deepEqual(h.sent, [{ type: 'booth:set-output', output: 'return' }])

  // Alt+1 from a button asks for the booth's own channel; typing in chat does not.
  h.sent.length = 0
  h.handleServerMessage({ type: 'booth:state', state: thai({ output_language_code: 'en', output_mediamtx_path: 'ev/1/en' }) })
  let prevented = 0
  const keydown = (event) => h.documentListeners.keydown.forEach((fn) => fn({ preventDefault: () => { prevented += 1 }, ...event }))
  keydown({ altKey: true, code: 'Digit1', target: { tagName: 'BUTTON' } })
  keydown({ altKey: true, code: 'Digit1', target: { tagName: 'INPUT' } })
  keydown({ altKey: false, code: 'KeyA', target: { tagName: 'BUTTON' } })
  assert.deepEqual(h.sent, [{ type: 'booth:set-output', output: 'primary' }])
  assert.equal(prevented, 1)

  // A one-way booth hides the switch and ignores the shortcut.
  h.sent.length = 0
  h.handleServerMessage({ type: 'booth:state', state: thai({ return_language_code: null }) })
  assert.equal(sw.hidden, true)
  keydown({ altKey: true, code: 'Digit2', target: { tagName: 'BUTTON' } })
  assert.deepEqual(h.sent, [])
}

// ── A backup interpreter sees why it can't switch ──────────────────────────
{
  const h = harness()
  h.state.participantId = 'ben'
  h.handleServerMessage({ type: 'booth:state', state: thai({ return_taken_by: 'Japanese' }) })
  assert.equal(h.byId('output-return').disabled, true)
  assert.equal(h.byId('output-reason').textContent, 'Only the interpreter on the mic can switch the channel.')
  h.requestOutput('return')
  assert.deepEqual(h.sent, [])
}

// ── Switching while live publishes again, to the new path ──────────────────
{
  const h = harness()
  h.state.participantId = 'ana'
  h.state.joined = true
  // Already publishing on the booth's own channel.
  h.state.ingestConnected = true
  h.applyBoothState(thai({ broadcast_unlocked: true }))
  h.state.whipResourceUrl = 'https://media.test/ev/1/th/whip/session'
  h.handleServerMessage({
    type: 'booth:state',
    state: thai({ broadcast_unlocked: true, output_language_code: 'en', output_mediamtx_path: 'ev/1/en' }),
  })
  await flush(h)
  // The old session was deleted, then a fresh ticket was asked for.
  assert.equal(h.fetches[0].method, 'DELETE')
  assert.equal(h.fetches[0].url, 'https://media.test/ev/1/th/whip/session')
  const ticketRequest = h.sent.find((m) => m.type === 'booth:request-publish-ticket')
  assert.ok(ticketRequest)
  h.handleServerMessage({ type: 'booth:publish-ticket', ticket: 'secret', path: 'ev/1/en' })
  await flush(h)
  const whip = h.fetches.find((f) => f.method === 'POST' && f.url.endsWith('/whip'))
  assert.equal(whip.url, 'https://media.test/ev/1/en/whip')
  assert.equal(whip.authorization, 'Bearer secret')
  assert.equal(h.state.ingestConnected, true)
  assert.equal(h.sent.at(-1).type, 'booth:update-state')
  assert.equal(h.sent.at(-1).ingest_connected, true)
}

// ── A reply without a path (older server) keeps today's URL ────────────────
{
  const h = harness()
  const pc = { localDescription: { sdp: 'offer' }, setRemoteDescription: async () => {} }
  const pending = h.doWhipIngest(pc)
  h.handleServerMessage({ type: 'booth:publish-ticket', ticket: 'secret' })
  await pending
  assert.equal(h.fetches[0].url, 'https://media.test/ev%2F1%2Fth/whip')
}

// ── A switch during the WHIP handshake retries on the new path ─────────────
{
  const h = harness()
  h.state.participantId = 'ana'
  h.state.joined = true
  // Unlocked: the booth starts publishing on its own and asks for a ticket.
  h.applyBoothState(thai({ broadcast_unlocked: true }))
  await flush(h)
  assert.equal(h.state.ingestStarting, true)
  // The server switched before the ticket was used: the stale ticket is refused.
  h.state.outputPath = 'ev/1/en'
  h.handleServerMessage({ type: 'booth:publish-ticket', error: 'Broadcast is stopped by Mission Control.' })
  await flush(h)
  assert.equal(h.sent.filter((m) => m.type === 'booth:request-publish-ticket').length, 2)
  h.handleServerMessage({ type: 'booth:publish-ticket', ticket: 'fresh', path: 'ev/1/en' })
  await flush(h)
  assert.ok(h.fetches.some((f) => f.url === 'https://media.test/ev/1/en/whip'))
}

// ── Teammates hear the channel the booth interprets into ───────────────────
{
  const h = harness()
  h.applyBoothState(thai())
  h.startBoothAudioListening()
  assert.deepEqual(h.whep.at(-1), ['start', 'https://media.test/ev/1/th/whep'])
  h.applyBoothState(thai({ output_language_code: 'en', output_mediamtx_path: 'ev/1/en' }))
  assert.deepEqual(h.whep.slice(-2), [['stop'], ['start', 'https://media.test/ev/1/en/whep']])
}

// ── The monitor never offers the channel the booth outputs into ────────────
{
  const h = harness({ relayWhepUrl: 'https://media.test/ev/1/en/whep' })
  h.applyBoothState(thai())
  h.syncMonitorOptions()
  assert.equal(h.relayOption.hidden, false)
  h.applyBoothState(thai({ output_language_code: 'en', output_mediamtx_path: 'ev/1/en' }))
  assert.equal(h.relayOption.hidden, true)
  assert.equal(h.relayOption.disabled, true)
  // It was selected, so the monitor moves off it.
  assert.equal(h.byId('monitor-source').value, 'off')
  assert.ok(h.events().includes('monitor-rejoined'))
  // Without a relay option there is nothing to hide.
  const plain = harness()
  plain.byId('monitor-source').children = {}
  plain.syncMonitorOptions()
}

// ── First join checks the monitor too ──────────────────────────────────────
{
  const h = harness({ relayWhepUrl: 'https://media.test/ev/1/th/whep' })
  h.handleServerMessage({ type: 'booth:joined', participant_id: 'ana', state: thai() })
  assert.equal(h.relayOption.hidden, true)
}

// ── Older states and pages ─────────────────────────────────────────────────
{
  // A state without the output path (older server) keeps the booth's own channel.
  const h = harness({ whepUrl: '' })
  h.applyBoothState({ ...thai(), output_mediamtx_path: undefined })
  assert.equal(h.state.outputPath, 'ev/1/th')
  h.startBoothAudioListening()
  assert.deepEqual(h.whep.at(-1), ['start', 'https://media.test/ev%2F1%2Fth/whep'])
  // Without a media server address there is nothing to listen to.
  const offline = harness({ whepUrl: '' })
  offline.state.whipBase = ''
  const before = offline.whep.length
  offline.startBoothAudioListening()
  assert.equal(offline.whep.length, before)
  // A page without the switch renders nothing.
  const bare = harness({ missing: ['output-switch'] })
  bare.renderOutputSwitch()
}

// ── Stop while publishing still stops, without a republish ─────────────────
{
  const h = harness()
  h.state.participantId = 'ana'
  h.state.joined = true
  h.state.ingestConnected = true
  h.applyBoothState(thai({ broadcast_unlocked: true }))
  h.applyBoothState(thai({ broadcast_unlocked: false, output_language_code: 'en', output_mediamtx_path: 'ev/1/en' }))
  await flush(h)
  assert.equal(h.state.ingestConnected, false)
  assert.equal(h.sent.filter((m) => m.type === 'booth:request-publish-ticket').length, 0)
}

// ── A 409 from MediaMTX still retries, as before ───────────────────────────
{
  const h = harness()
  h.state.participantId = 'ana'
  h.state.joined = true
  let posts = 0
  h.handleServerMessage({ type: 'booth:state', state: thai({ broadcast_unlocked: true }) })
  await flush(h)
  h.handleServerMessage({ type: 'booth:publish-ticket', error: 'WHIP error 409: busy' })
  await flush(h)
  posts = h.sent.filter((m) => m.type === 'booth:request-publish-ticket').length
  assert.equal(posts, 2)
}

console.log('Two-way booth page checks passed')
