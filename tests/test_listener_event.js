/**
 * Unit tests for the listener page (portal/static/js/listener-event.js).
 *
 * Run with: node tests/test_listener_event.js
 *
 * Loads the real module against a small fake DOM and stubbed WHEP client and
 * WebSockets, so the status badge and captions box can be checked without a
 * browser or media server.
 */
'use strict';

const path = require('path');
const { pathToFileURL } = require('url');

let passed = 0;
let failed = 0;

function assert(condition, message) {
  if (!condition) {
    console.error('  FAIL: ' + message);
    failed++;
  } else {
    console.log('  PASS: ' + message);
    passed++;
  }
}

// ── Fake DOM ─────────────────────────────────────────────────────────────

const elementsById = new Map();

class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.style = { setProperty(name, value) { this[name] = value; }, removeProperty(name) { delete this[name]; } };
    this.dataset = {};
    this.attributes = {};
    this.className = '';
    this.disabled = false;
    this.listeners = {};
    this._id = '';
    this._text = '';
    this._value = '';
    this._selectedIndex = -1;
    this.scrollTop = 0;
  }

  get id() {
    return this._id;
  }

  set id(value) {
    this._id = value;
    elementsById.set(value, this);
  }

  get classList() {
    const el = this;
    const names = () => el.className.split(/\s+/).filter(Boolean);
    return {
      contains: (name) => names().includes(name),
      add: (...add) => {
        el.className = [...new Set([...names(), ...add])].join(' ');
      },
      remove: (...remove) => {
        el.className = names().filter((n) => !remove.includes(n)).join(' ');
      },
      toggle: (name, force) => {
        const on = force === undefined ? !names().includes(name) : force;
        if (on) el.classList.add(name);
        else el.classList.remove(name);
      },
    };
  }

  get textContent() {
    return this._text + this.children.map((c) => c.textContent).join('');
  }

  set textContent(value) {
    this.removeChildren();
    this._text = String(value);
  }

  // Enough of an HTML parser for the flat markup listener-event.js writes.
  set innerHTML(html) {
    this.removeChildren();
    this._text = '';
    const tag = /<(\w+)(?: class="([^"]*)")?(?: value="([^"]*)")?>([^<]*)<\/\1>/g;
    let match;
    while ((match = tag.exec(html))) {
      const child = new FakeElement(match[1]);
      child.className = match[2] || '';
      if (match[3] !== undefined) child.value = match[3];
      child._text = match[4];
      this.appendChild(child);
    }
  }

  get firstChild() {
    return this.children[0] || null;
  }

  get firstElementChild() {
    return this.children[0] || null;
  }

  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    if (this.tagName === 'SELECT' && this._selectedIndex === -1) this._selectedIndex = 0;
    return child;
  }

  removeChildren() {
    this.children.forEach((child) => {
      child.parentNode = null;
    });
    this.children = [];
  }

  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    child.parentNode = null;
    return child;
  }

  querySelector(selector) {
    const name = selector.startsWith('.') ? selector.slice(1) : null;
    for (const child of this.children) {
      if (name && child.classList.contains(name)) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return name in this.attributes ? this.attributes[name] : null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(type, handler) {
    (this.listeners[type] = this.listeners[type] || []).push(handler);
  }

  dispatchEvent(event) {
    (this.listeners[event.type] || []).forEach((handler) => handler.call(this, event));
  }

  // <select> behaviour
  get options() {
    return this.children;
  }

  get selectedIndex() {
    return this.children.length ? this._selectedIndex : -1;
  }

  set selectedIndex(index) {
    this._selectedIndex = index;
  }

  get value() {
    if (this.tagName !== 'SELECT') return this._value;
    const opt = this.children[this.selectedIndex];
    return opt ? opt.value : '';
  }

  set value(value) {
    if (this.tagName !== 'SELECT') {
      this._value = value;
      return;
    }
    this._selectedIndex = this.children.findIndex((o) => o.value === String(value));
  }

  get text() {
    return this.textContent;
  }
}

// Like the real one, getElementById only finds elements still in the page.
function isConnected(el) {
  for (let node = el; node; node = node.parentNode) {
    if (node.inPage) return true;
  }
  return false;
}

const document = {
  getElementById: (id) => {
    const el = elementsById.get(id);
    return el && isConnected(el) ? el : null;
  },
  createElement: (tagName) => new FakeElement(tagName),
};

function make(tagName, id) {
  const el = new FakeElement(tagName);
  el.id = id;
  el.inPage = true;
  return el;
}

[
  'toggle-captions-btn',
  'caption-size-down',
  'caption-size-up',
  'jump-to-latest',
  'share-event-btn',
  'share-event-status',
  'audio-player',
  'status',
  'caption-history',
  'caption-current',
  'waiting-text',
  'translation-lang-group',
  'tts-sync-group',
  'tts-sync-toggle',
  'captions-container',
  'tap-to-listen-btn',
].forEach((id) => make('div', id));
['room-select', 'language-select', 'caption-mode-select', 'translation-lang-select'].forEach((id) =>
  make('select', id),
);

const roomSelect = document.getElementById('room-select');
roomSelect.innerHTML =
  '<option value="">-- Select Room --</option><option value="1">Main Hall</option><option value="2">Recovery Hall</option>';
const captionModeSelect = document.getElementById('caption-mode-select');
captionModeSelect.innerHTML =
  '<option value="original">Original</option><option value="translated">Translated</option>';
document.getElementById('waiting-text').className = 'absolute inset-0 flex';

// Captions box: each history row is rowHeight px tall and 300px are visible.
// Like a browser, it fires "scroll" only when scrollTop actually changes.
const captionsBox = make('div', 'live-captions');
const historyBox = document.getElementById('caption-history');
let rowHeight = 20;
Object.defineProperty(captionsBox, 'clientHeight', { get: () => 300 });
Object.defineProperty(captionsBox, 'scrollHeight', {
  get: () => Math.max(300, historyBox.children.length * rowHeight),
});
let captionsScrollTop = 0;
Object.defineProperty(captionsBox, 'scrollTop', {
  get: () => captionsScrollTop,
  set: (value) => {
    const next = Math.max(0, Math.min(value, captionsBox.scrollHeight - captionsBox.clientHeight));
    if (next === captionsScrollTop) return;
    captionsScrollTop = next;
    captionsBox.dispatchEvent(new Event('scroll'));
  },
});

const listenerData = make('script', 'listener-data');
listenerData.textContent = JSON.stringify({
  eventSlug: 'demo',
  shareUrl: 'https://example.test/listener/demo',
  rooms: [{ id: 1, floor_tts_enabled: true }, { id: 2, floor_tts_enabled: true }],
  booths: [
    { id: 'floor_1', room_id: 1, language_code: 'floor', language_name: 'Floor Audio', whep_url: 'http://mtx/floor/whep', audio_delay_ms: 0 },
    { id: 2, room_id: 1, language_code: 'es', language_name: 'Spanish', whep_url: 'http://mtx/es/whep', audio_delay_ms: 0 },
    {
      id: 'floor_2',
      room_id: 2,
      language_code: 'floor',
      language_name: 'Floor Audio',
      whep_url: 'http://mtx/floor2/whep',
      audio_delay_ms: 0,
      translation_enabled: true,
      translation_languages: [{ code: 'fr', name: 'French', tts_enabled: true }],
    },
  ],
});

// ── Stubbed browser APIs ─────────────────────────────────────────────────

let whepOpts = null;
let resumeCalls = 0;
const WhepListener = {
  start(opts) {
    whepOpts = opts;
  },
  resume() {
    resumeCalls++;
  },
  stop() {
    if (whepOpts) whepOpts.onState({ peerConnection: 'closed', ice: 'closed', audioActive: false });
  },
};

const sockets = [];
class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.OPEN;
    sockets.push(this);
  }
  send() {}
  close() {
    this.readyState = 3;
  }
}
FakeWebSocket.OPEN = 1;

function captionsSocket() {
  return sockets.filter((s) => s.url.includes('/ws/captions/')).pop();
}

let boothState = { ingest_status: 'disconnected' };
const realSetTimeout = setTimeout;
const realClearTimeout = clearTimeout;
function fetch(url) {
  const body = url.includes('/audio-delay') ? { audio_delay_ms: 0 } : boothState;
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
}

// Translated speech: each decoded segment is recorded in the order the
// listener hands it to the scheduler. A segment's PCM length identifies it.
const scheduledSegments = [];
class FakeAudioContext {
  constructor() {
    this.state = 'running';
  }
  createBuffer(channels, length) {
    return { length, copyToChannel() {} };
  }
}
const AudioScheduler = {
  create: () => ({
    scheduleBuffer(buffer) {
      scheduledSegments.push(buffer.length / 10);
    },
    reset() {},
  }),
};

Object.assign(globalThis, {
  document,
  WhepListener,
  WebSocket: FakeWebSocket,
  fetch,
  window: {
    location: { protocol: 'http:', host: 'portal.test', href: 'http://portal.test/listener/demo', search: '' },
    history: {
      replaceState(_state, _title, url) {
        globalThis.window.location.href = String(url);
      },
    },
    setTimeout,
    setInterval: () => 0,
    addEventListener: () => {},
    AudioContext: FakeAudioContext,
    AudioScheduler,
  },
});
require(path.join(__dirname, '..', 'portal', 'static', 'js', 'tts-parser.js'));

// A translation bundle as the server frames it: version byte, header length, JSON header, no audio.
function bundleFrame(header) {
  const json = new TextEncoder().encode(JSON.stringify(header));
  const frame = new Uint8Array(5 + json.length);
  new DataView(frame.buffer).setUint8(0, 1);
  new DataView(frame.buffer).setUint32(1, json.length, false);
  frame.set(json, 5);
  return frame.buffer;
}

// ── Helpers ──────────────────────────────────────────────────────────────

const languageSelect = document.getElementById('language-select');

function statusText() {
  return document.getElementById('status').textContent;
}

function choose(select, value) {
  select.value = value;
  select.dispatchEvent(new Event('change'));
}

function flush() {
  return new Promise((resolve) => realSetTimeout(resolve, 0));
}

// Timers the test runs by hand, so retry delays are checked without waiting.
const fakeTimers = new Set();
function useFakeTimers() {
  globalThis.setTimeout = (fn, ms) => {
    const timer = { fn, ms: ms || 0 };
    fakeTimers.add(timer);
    return timer;
  };
  globalThis.clearTimeout = (timer) => {
    if (!fakeTimers.delete(timer)) realClearTimeout(timer);
  };
}
function pendingTimers(ms) {
  return [...fakeTimers].filter((t) => t.ms === ms).length;
}
function runAllTimers() {
  for (const timer of [...fakeTimers]) {
    fakeTimers.delete(timer);
    timer.fn();
  }
}
function runTimers(ms) {
  for (const timer of [...fakeTimers]) {
    if (timer.ms !== ms) continue;
    fakeTimers.delete(timer);
    timer.fn();
  }
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Poll until the listener reaches the expected state. Gives up after the
// timeout so the assertion that follows reports the failure instead of hanging.
async function waitFor(predicate, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate() && Date.now() < deadline) await sleep(10);
}

function ttsSocket() {
  return sockets.filter((s) => s.url.includes('/ws/tts/')).pop();
}

// A binary TTS bundle as the server frames it: version byte, big-endian JSON
// header length, JSON header, then 16-bit PCM. A segment carries seq * 10
// samples so the scheduler stub can tell which segment it received.
function ttsFrame(header, withAudio) {
  const json = Buffer.from(JSON.stringify(header));
  const pcm = Buffer.alloc(withAudio ? header.seq * 10 * 2 : 0, 1);
  const frame = Buffer.alloc(5 + json.length + pcm.length);
  frame[0] = 1;
  frame.writeUInt32BE(json.length, 1);
  json.copy(frame, 5);
  pcm.copy(frame, 5 + json.length);
  return frame.buffer.slice(frame.byteOffset, frame.byteOffset + frame.length);
}

// The floor pipeline sends each final sentence on the captions feed and then
// as two TTS bundles: text-ready (audio pending) and audio-ready.
function sendFloorSegment(seq, segmentId, text, translation, stage) {
  const header = { seq, segment_id: segmentId, caption: text, translation, error: null };
  if (stage === 'caption') {
    captionsSocket().onmessage({
      data: JSON.stringify({ type: 'caption', status: 'final', seq, segment_id: segmentId, text }),
    });
  } else if (stage === 'text') {
    ttsSocket().onmessage({ data: ttsFrame({ ...header, audio_pending: true }, false) });
  } else {
    ttsSocket().onmessage({ data: ttsFrame({ ...header, audio_pending: false }, true) });
  }
}

async function run() {
  await import(pathToFileURL(path.join(__dirname, '..', 'portal', 'static', 'js', 'listener-event.js')).href);
  choose(roomSelect, '1');

  console.log('\n── Shareable listener link ──');
  assert(new URL(window.location.href).searchParams.get('room') === '1', 'choosing a room puts it in the address bar');
  assert(!new URL(window.location.href).searchParams.has('code'), 'the address bar never carries a join code');

  console.log('\n── Status badge (#985) ──');

  choose(languageSelect, 'http://mtx/floor/whep');
  assert(statusText() === 'Connecting...', 'floor audio shows Connecting... until audio plays, got "' + statusText() + '"');

  // The first session reports "new" from setRemoteDescription until ICE starts.
  whepOpts.onState({ peerConnection: 'new', ice: 'new', audioActive: false, retrying: false });
  assert(statusText() === 'Connecting...', 'a first session that is still starting stays on Connecting..., got "' + statusText() + '"');

  whepOpts.onState({ peerConnection: 'new', ice: 'new', audioActive: false, retrying: true });
  assert(statusText() === 'Reconnecting...', 'a failed WHEP request shows Reconnecting..., got "' + statusText() + '"');

  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: true });
  assert(statusText() === 'Live (WebRTC)', 'shows Live once audio is playing');

  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: false });
  assert(statusText() === 'Connected — waiting for audio', 'a connected peer without playback is not Live');

  for (const state of ['failed', 'disconnected']) {
    whepOpts.onState({ peerConnection: state, ice: state, audioActive: false });
    assert(statusText() === 'Reconnecting...', state + ' connections show Reconnecting...');
  }

  console.log('\n── Tap to listen when autoplay is blocked ──');
  const tapButton = document.getElementById('tap-to-listen-btn');
  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: false, playbackBlocked: true });
  assert(tapButton.hidden === false, 'blocked playback shows the Tap to listen button');
  assert(statusText() === 'Ready — tap to listen', 'blocked playback says to tap, got "' + statusText() + '"');

  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: true, playbackBlocked: true });
  assert(statusText() !== 'Live (WebRTC)', 'a suspended delayed stream is not shown as Live');

  tapButton.dispatchEvent(new Event('click'));
  assert(resumeCalls === 1, 'tapping the button asks the player to start playback');

  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: true, playbackBlocked: false });
  assert(tapButton.hidden === true, 'the button hides once audio plays');
  assert(statusText() === 'Live (WebRTC)', 'playback after the tap shows Live');

  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: false, playbackBlocked: true });
  choose(languageSelect, '');
  assert(tapButton.hidden === true, 'stopping the stream hides the button');

  choose(languageSelect, 'http://mtx/es/whep');
  assert(statusText() !== 'Live (WebRTC)', 'switching to a booth does not show Live before its state is known');
  await flush();
  await flush();
  assert(
    statusText() === 'Broadcast not live yet — waiting...',
    'a booth that is not live shows the waiting message, got "' + statusText() + '"',
  );

  console.log('\n── Captions scrolling (#986) ──');

  const socket = captionsSocket();
  for (let seq = 1; seq <= 40; seq++) {
    socket.onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', seq: seq, text: 'Line ' + seq }) });
  }
  const bottom = captionsBox.scrollHeight - captionsBox.clientHeight;
  assert(captionsBox.scrollTop === bottom, 'captions follow new lines while the listener is at the bottom');

  // Rows get taller after they render (e.g. styles applied late) without the listener scrolling.
  rowHeight = 40;
  socket.onmessage({ data: JSON.stringify({ type: 'caption', status: 'partial', segment_id: 'p40', text: 'Next' }) });
  assert(
    captionsBox.scrollTop === captionsBox.scrollHeight - captionsBox.clientHeight,
    'captions keep following when rows grow after rendering',
  );

  captionsBox.scrollTop = 0;
  socket.onmessage({ data: JSON.stringify({ type: 'caption', status: 'partial', segment_id: 'p41', text: 'More' }) });
  socket.onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', seq: 41, text: 'Line 41' }) });
  assert(captionsBox.scrollTop === 0, 'new captions leave the scroll alone after the listener scrolls up, got ' + captionsBox.scrollTop);

  captionsBox.scrollTop = captionsBox.scrollHeight;
  socket.onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', seq: 42, text: 'Line 42' }) });
  assert(
    captionsBox.scrollTop === captionsBox.scrollHeight - captionsBox.clientHeight,
    'captions follow again once the listener scrolls back to the bottom',
  );

  captionsBox.scrollTop = captionsBox.scrollHeight - captionsBox.clientHeight - 40;
  socket.onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', seq: 43, text: 'Line 43' }) });
  assert(
    captionsBox.scrollTop === captionsBox.scrollHeight - captionsBox.clientHeight,
    'captions still follow within 40px of the bottom',
  );

  captionsBox.scrollTop = captionsBox.scrollHeight - captionsBox.clientHeight - 41;
  const readingPosition = captionsBox.scrollTop;
  socket.onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', seq: 44, text: 'Line 44' }) });
  assert(captionsBox.scrollTop === readingPosition, 'captions stop following beyond the 40px threshold');

  const translationSelect = document.getElementById('translation-lang-select');
  translationSelect.innerHTML = '<option value="fr">French</option>';
  const translation = { type: 'translated_caption', language_code: 'fr', seq: 44, text: 'Ligne 44' };
  socket.onmessage({ data: JSON.stringify(translation) });
  assert(historyBox.children[historyBox.children.length - 1].textContent.includes('Ligne 44'), 'translations render in the matching caption row');
  assert(captionsBox.scrollTop === readingPosition, 'translations preserve the reading position while scrolled up');

  const jumpButton = document.getElementById('jump-to-latest');
  assert(!jumpButton.hidden, 'new captions offer Jump to latest while reading older lines');
  jumpButton.dispatchEvent(new Event('click'));
  assert(captionsBox.scrollTop === captionsBox.scrollHeight - captionsBox.clientHeight,
    'Jump to latest returns to the newest caption');
  assert(jumpButton.hidden, 'Jump to latest hides after returning to live');
  socket.onmessage({ data: JSON.stringify({ ...translation, seq: 45, text: 'Ligne 45' }) });
  assert(
    captionsBox.scrollTop === captionsBox.scrollHeight - captionsBox.clientHeight,
    'translations follow again at the bottom',
  );

  captionsBox.scrollTop = 0;
  choose(languageSelect, 'http://mtx/floor/whep');
  for (let seq = 1; seq <= 20; seq++) {
    captionsSocket().onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', seq: seq, text: 'Floor ' + seq }) });
  }
  assert(
    captionsBox.scrollTop === captionsBox.scrollHeight - captionsBox.clientHeight,
    'captions follow the new source after switching while scrolled up',
  );

  console.log('\n── Captions placeholder (#987) ──');

  const waitingText = document.getElementById('waiting-text');
  assert(waitingText.classList.contains('listener-hidden'), 'the placeholder is hidden once captions arrive');

  choose(languageSelect, 'http://mtx/floor/whep');
  assert(historyBox.children.length === 0, 'switching source clears the old captions');
  assert(!waitingText.classList.contains('listener-hidden'), 'the placeholder comes back after switching source');

  captionsSocket().onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', seq: 1, text: 'Floor' }) });
  choose(roomSelect, '');
  assert(!waitingText.classList.contains('listener-hidden'), 'the placeholder comes back after clearing the room');

  console.log('\n── Floor transcription recovery (#1061) ──');

  // A listener stays on floor audio with French speech while the room's floor
  // transcription restarts. The restarted worker continues the room's stored
  // sequence, so new sentences arrive as 3, 4, 5 with new segment IDs.
  choose(roomSelect, '2');
  choose(languageSelect, 'http://mtx/floor2/whep');
  const ttsToggle = document.getElementById('tts-sync-toggle');
  ttsToggle.checked = true;
  ttsToggle.dispatchEvent(new Event('change'));
  choose(document.getElementById('translation-lang-select'), 'fr');
  assert(Boolean(ttsSocket()) && ttsSocket().url.endsWith('/ws/tts/2/fr/demo-2-floor'), 'French speech opens the room TTS feed');

  // Before the restart: sentence 1 plays; sentence 2's audio is still being synthesized.
  for (const stage of ['caption', 'text', 'audio']) sendFloorSegment(1, 'before-1', 'Before 1', 'Avant 1', stage);
  for (const stage of ['caption', 'text']) sendFloorSegment(2, 'before-2', 'Before 2', 'Avant 2', stage);
  await waitFor(() => scheduledSegments.length >= 1);
  assert(JSON.stringify(scheduledSegments) === '[1]', 'the first sentence plays before the restart, got ' + JSON.stringify(scheduledSegments));

  // After the restart, then the delayed audio for sentence 2 from the old worker.
  for (const seq of [3, 4, 5]) {
    for (const stage of ['caption', 'text', 'audio']) {
      sendFloorSegment(seq, 'after-' + seq, 'After ' + seq, 'Après ' + seq, stage);
    }
  }
  await sleep(60); // fixed wait: this checks that nothing plays yet
  assert(JSON.stringify(scheduledSegments) === '[1]', 'recovered sentences wait for the delayed pre-restart audio');
  sendFloorSegment(2, 'before-2', 'Before 2', 'Avant 2', 'audio');
  await waitFor(() => scheduledSegments.length >= 5);
  assert(
    JSON.stringify(scheduledSegments) === '[1,2,3,4,5]',
    'speech continues across the restart in sentence order, got ' + JSON.stringify(scheduledSegments),
  );

  // A late copy of already played audio must not replay or reorder speech.
  sendFloorSegment(1, 'before-1', 'Before 1', 'Avant 1', 'audio');
  sendFloorSegment(3, 'after-3', 'After 3', 'Après 3', 'audio');
  await sleep(60); // fixed wait: this checks that nothing is replayed
  assert(JSON.stringify(scheduledSegments) === '[1,2,3,4,5]', 'late audio from expired sentences is not replayed');

  const rows = historyBox.children.map((row) => ({
    id: row.id,
    source: row.querySelector('.caption-src-text').textContent,
    translation: row.querySelector('.translation-text').textContent,
  }));
  assert(rows.length === 5, 'every sentence keeps its own history row, got ' + rows.length);
  [
    ['Before 1', 'Avant 1'],
    ['Before 2', 'Avant 2'],
    ['After 3', 'Après 3'],
    ['After 4', 'Après 4'],
    ['After 5', 'Après 5'],
  ].forEach(([source, translation], index) => {
    const row = rows[index] || {};
    assert(
      row.id === 'segment-seq-' + (index + 1) &&
        (row.source || '').endsWith(source) &&
        (row.translation || '').endsWith(translation),
      'history row ' + (index + 1) + ' still shows "' + source + '" / "' + translation + '", got ' + JSON.stringify(row),
    );
  });

  console.log('\n── Empty translations (#1184) ──');

  await import(pathToFileURL(path.join(__dirname, '..', 'portal', 'static', 'js', 'tts-parser.js')).href);
  choose(roomSelect, '1');
  choose(languageSelect, 'http://mtx/floor/whep');
  translationSelect.innerHTML = '<option value="fr">French</option>';
  choose(translationSelect, 'fr');
  const bundles = sockets.filter((s) => s.url.includes('/ws/tts/')).pop();
  assert(Boolean(bundles), 'choosing a translation language opens the bundle feed');

  // The translator ran and had nothing to say: no text, no error, no audio to wait for.
  bundles.onmessage({
    data: bundleFrame({ segment_id: 'a', seq: 1, caption: 'Um.', translation: '', error: null, audio_pending: false }),
  });
  assert(historyBox.textContent.includes('Um.'), 'the original caption of an empty translation still shows');
  assert(
    !historyBox.textContent.includes('(Translation'),
    'an empty translation shows no message, got "' + historyBox.textContent + '"',
  );

  // The queue must already be past the empty segment, or this one would wait behind it.
  bundles.onmessage({
    data: bundleFrame({ segment_id: 'b', seq: 2, caption: 'Hello', translation: '', error: 'pipeline_failed' }),
  });
  assert(
    !historyBox.textContent.includes('(Translation'),
    'a failed translation shows nothing, got "' + historyBox.textContent + '"',
  );
  assert(historyBox.textContent.includes('Hello'), 'the original caption of a failed translation still shows');

  console.log('\n── Translation feed reconnects ──');

  useFakeTimers();
  const feedSockets = () => sockets.filter((s) => s.url.includes('/ws/tts/'));

  // Count new audio contexts and schedulers from here on, and keep the page's context.
  let audioContexts = 0;
  let schedulerCreates = 0;
  let schedulerResets = 0;
  let pageContext = null;
  const BaseAudioContext = window.AudioContext;
  window.AudioContext = class extends BaseAudioContext {
    constructor() {
      super();
      audioContexts++;
    }
  };
  const baseCreate = window.AudioScheduler.create;
  window.AudioScheduler.create = (context, options) => {
    schedulerCreates++;
    pageContext = context;
    const scheduler = baseCreate(context, options);
    scheduler.reset = () => {
      schedulerResets++;
    };
    return scheduler;
  };
  choose(translationSelect, 'fr');
  pageContext.resume = () => Promise.resolve();
  const feed = feedSockets().pop();
  const feedUrl = feed.url;
  const contextsBefore = audioContexts;
  const schedulersBefore = schedulerCreates;

  // The queue is at segment 7 when the feed drops.
  feed.onmessage({
    data: bundleFrame({ segment_id: 's7', seq: 7, caption: 'Seven', translation: 'Sept', error: null, audio_pending: false }),
  });
  // A frame held for the program's sync delay (3s), sent just before the drop.
  feed.onmessage({
    data: bundleFrame({ segment_id: 'held', seq: 50, caption: '', translation: 'Held', error: null, audio_pending: false,
      source_received_at_ms: 5000, server_sent_at_ms: 2000 }),
  });
  const socketsBefore = feedSockets().length;
  feed.onclose({ code: 1006 });
  assert(feedSockets().length === socketsBefore, 'a dropped feed waits before reconnecting');
  assert(pendingTimers(1000) === 1, 'the first retry waits 1s');
  runTimers(1000);
  const first = feedSockets().pop();
  assert(feedSockets().length === socketsBefore + 1 && first.url === feedUrl, 'a dropped translation feed reopens the same feed');
  assert(
    audioContexts === contextsBefore && schedulerCreates === schedulersBefore && schedulerResets === 0,
    'reconnecting keeps the audio context and the speech already scheduled on it',
  );

  // A restarted server numbers segments from 1 again, and the held frame from before the drop must not get in the way.
  runTimers(3000);
  assert(!historyBox.textContent.includes('Held'), 'a frame held from before the drop is dropped with the old feed');
  first.onmessage({
    data: bundleFrame({ segment_id: 'r1', seq: 1, caption: 'Restarted', translation: 'Redémarré', error: null, audio_pending: false }),
  });
  assert(historyBox.textContent.includes('Redémarré'), 'the first segment after a server restart shows, though its number went back to 1');
  assert(historyBox.textContent.includes('Restarted'), 'and the queue plays it at once');

  // Segment 2 is lost in the next drop, so the queue is left waiting up to 65s for it.
  first.onmessage({
    data: bundleFrame({ segment_id: 'r3', seq: 3, caption: 'Three', translation: 'Trois', error: null, audio_pending: false }),
  });
  runTimers(2500);
  assert(pendingTimers(65000) === 1, 'the queue waits for the missing segment 2');

  first.onclose({ code: 1006 });
  assert(pendingTimers(1000) === 1, 'a feed that sent data before it dropped retries after 1s again');
  runTimers(1000);
  const second = feedSockets().pop();
  assert(second !== first && second.url === feedUrl, 'the feed reopens again');
  assert(pendingTimers(65000) === 0, 'reconnecting drops the wait for a segment the old feed never sent');
  second.onopen();

  // Segments 2 to 4 were sent while the feed was down; 5 must not wait 65s for them.
  second.onmessage({
    data: bundleFrame({ segment_id: 'r5', seq: 5, caption: 'Five', translation: 'Cinq', error: null, audio_pending: false }),
  });
  assert(historyBox.textContent.includes('Five'), 'after a gap the queue moves on to the first new segment');

  second.onclose({ code: 1006 });
  assert(pendingTimers(1000) === 1, 'the wait is back at 1s');
  runTimers(1000);
  // A feed that opens but drops before any data counts as a failed try.
  const quiet = feedSockets().pop();
  quiet.onopen();
  quiet.onclose({ code: 1006 });
  assert(pendingTimers(2000) === 1 && pendingTimers(1000) === 0, 'a feed that drops again before any data waits twice as long');
  assert(!statusText().includes('Access ended'), 'a dropped feed does not say access ended');
  runTimers(2000);

  // 1008 is a throttled join code, which clears on its own, so the feed keeps retrying.
  // The server accepts before it closes, so the socket opens first; that must not reset the wait.
  const throttled = feedSockets().pop();
  throttled.onopen();
  throttled.onclose({ code: 1008 });
  assert(pendingTimers(4000) === 1, 'a feed closed with 1008 (throttled) retries');
  assert(!statusText().includes('Access ended'), 'a throttled feed does not say access ended');
  runTimers(4000);
  const throttledAgain = feedSockets().pop();
  throttledAgain.onopen();
  throttledAgain.onclose({ code: 1008 });
  assert(pendingTimers(8000) === 1 && pendingTimers(1000) === 0, 'a feed refused again with 1008 keeps backing off');
  runTimers(8000);
  assert(schedulerResets === 0, 'none of the reconnects reset the speech already scheduled');

  for (const code of [4001, 4003]) {
    const socket = feedSockets().pop();
    const count = feedSockets().length;
    const timers = fakeTimers.size;
    socket.onclose({ code: code });
    assert(fakeTimers.size === timers, 'a feed closed with ' + code + ' schedules no reconnect');
    assert(statusText() === 'Access ended. Reload the page to join again.', 'a ' + code + ' close tells the listener access ended');
    runAllTimers();
    assert(feedSockets().length === count, 'no new feed opens after a ' + code + ' close');
    choose(translationSelect, 'fr');
  }

  // Autoplay is blocked: Tap to listen shows while the feed is up and goes once the server refuses it.
  pageContext.state = 'suspended';
  choose(translationSelect, 'fr');
  assert(tapButton.hidden === false, 'a feed waiting on a blocked audio context asks the listener to tap');
  feedSockets().pop().onclose({ code: 4003 });
  assert(tapButton.hidden === true, 'a refused feed no longer asks the listener to tap');
  pageContext.state = 'running';
  choose(translationSelect, 'fr');

  // Switching language closes the old feed ourselves; its late close event must not reopen it.
  const switched = feedSockets().pop();
  choose(translationSelect, 'fr');
  const count = feedSockets().length;
  switched.onclose({ code: 1006 });
  assert(pendingTimers(1000) === 0 && feedSockets().length === count, 'a feed we closed ourselves is not reopened');

  // The last feed never opened and dropped once, but a newly chosen feed starts over at 1s.
  feedSockets().pop().onclose({ code: 1006 });
  assert(pendingTimers(1000) === 1, 'a newly started feed retries after 1s again');
  choose(languageSelect, '');
  assert(pendingTimers(1000) === 0, 'stopping the stream cancels a pending reconnect');
  const afterStop = feedSockets().length;
  runTimers(1000);
  assert(feedSockets().length === afterStop, 'no feed opens after the stream was stopped');

  console.log('\n── AI voice status while the feed reconnects ──');

  choose(languageSelect, 'http://mtx/floor/whep');
  const voiceToggle = document.getElementById('tts-sync-toggle');
  voiceToggle.checked = true;
  voiceToggle.dispatchEvent(new Event('change'));
  translationSelect.innerHTML = '<option value="fr">French</option>';
  choose(translationSelect, 'fr');
  assert(statusText() === 'Live (French TTS)', 'the AI voice shows as live, got "' + statusText() + '"');
  const voice = feedSockets().pop();
  voice.onopen();
  runTimers(500);
  voice.onclose({ code: 1006 });
  assert(statusText() === 'Reconnecting...', 'a dropped AI voice feed shows Reconnecting, got "' + statusText() + '"');
  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: true });
  assert(statusText() === 'Reconnecting...', 'a WebRTC update does not replace Reconnecting, got "' + statusText() + '"');
  runTimers(1000);
  // The server accepts a throttled socket before refusing it: that must never show Live.
  const refused = feedSockets().pop();
  refused.onopen();
  assert(statusText() === 'Reconnecting...', 'a socket that just opened is not called live yet');
  refused.onclose({ code: 1008 });
  runTimers(500);
  assert(statusText() === 'Reconnecting...', 'a socket refused right after opening never shows Live, got "' + statusText() + '"');
  runTimers(2000);
  const voiceBack = feedSockets().pop();
  voiceBack.onopen();
  runTimers(500);
  assert(statusText() === 'Live (French TTS)', 'the AI voice shows as live again once the feed stays open');

  voiceBack.onclose({ code: 4003 });
  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: true });
  assert(
    statusText() === 'Access ended. Reload the page to join again.',
    'a WebRTC update does not hide that access ended, got "' + statusText() + '"',
  );
  choose(translationSelect, 'fr');
  assert(statusText() === 'Live (French TTS)', 'choosing the voice again starts a new feed');
  whepOpts.onState({ peerConnection: 'connected', ice: 'connected', audioActive: true });
  assert(statusText() === 'Live (WebRTC)', 'WebRTC updates the badge again once a new feed starts');

  // Without the AI voice the badge is WebRTC's, so a translation feed drop leaves it alone.
  voiceToggle.checked = false;
  voiceToggle.dispatchEvent(new Event('change'));
  assert(statusText() === 'Live (WebRTC + Translation)', 'without the AI voice the badge is WebRTC with translation');
  feedSockets().pop().onclose({ code: 1006 });
  assert(pendingTimers(1000) === 1, 'the translation-only feed still reconnects');
  assert(statusText() === 'Live (WebRTC + Translation)', 'a translation-only feed drop leaves the WebRTC badge alone, got "' + statusText() + '"');

  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed > 0 ? 1 : 0);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
