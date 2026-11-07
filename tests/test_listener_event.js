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
].forEach((id) => make('div', id));
['room-select', 'language-select', 'caption-mode-select', 'translation-lang-select'].forEach((id) =>
  make('select', id),
);

const roomSelect = document.getElementById('room-select');
roomSelect.innerHTML = '<option value="">-- Select Room --</option><option value="1">Main Hall</option>';
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
  rooms: [{ id: 1, floor_tts_enabled: false }],
  booths: [
    { id: 'floor_1', room_id: 1, language_code: 'floor', language_name: 'Floor Audio', whep_url: 'http://mtx/floor/whep', audio_delay_ms: 0 },
    { id: 2, room_id: 1, language_code: 'es', language_name: 'Spanish', whep_url: 'http://mtx/es/whep', audio_delay_ms: 0 },
  ],
});

// ── Stubbed browser APIs ─────────────────────────────────────────────────

let whepOpts = null;
const WhepListener = {
  start(opts) {
    whepOpts = opts;
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
function fetch(url) {
  const body = url.includes('/audio-delay') ? { audio_delay_ms: 0 } : boothState;
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
}

Object.assign(globalThis, {
  document,
  WhepListener,
  WebSocket: FakeWebSocket,
  fetch,
  window: {
    location: { protocol: 'http:', host: 'portal.test' },
    setTimeout,
    setInterval: () => 0,
    addEventListener: () => {},
  },
});

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
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function run() {
  await import(pathToFileURL(path.join(__dirname, '..', 'portal', 'static', 'js', 'listener-event.js')).href);
  choose(roomSelect, '1');

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

  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed > 0 ? 1 : 0);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
