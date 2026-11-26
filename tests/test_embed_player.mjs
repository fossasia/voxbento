/**
 * Unit tests for the embed player's floor fallback (portal/static/js/embed-player.js, #1397).
 *
 * Run with: node tests/test_embed_player.mjs
 *
 * Loads the real module once per case against a small fake DOM, fake WHEP clients,
 * fake WebSockets, a fake clock and hand-run timers.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

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

// Both are classic scripts, loaded in the order embed.html loads them.
const JS_DIR = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'portal', 'static', 'js');
const CHANNEL_SOURCE = path.join(JS_DIR, 'channel-source.js');
const EMBED_PLAYER = path.join(JS_DIR, 'embed-player.js');
const EMBED_PLAYER_SRC = fs.readFileSync(EMBED_PLAYER, 'utf8');
vm.runInThisContext(fs.readFileSync(CHANNEL_SOURCE, 'utf8'), { filename: CHANNEL_SOURCE });

// ── Fake clock and timers ─────────────────────────────────────────────────

let clock = 0;
const timers = new Set();
globalThis.setTimeout = (fn, ms) => {
  const timer = { fn, at: clock + (ms || 0), ms: ms || 0 };
  timers.add(timer);
  return timer;
};
globalThis.clearTimeout = (timer) => timers.delete(timer);
Date.now = () => clock;
function advance(ms) {
  const end = clock + ms;
  for (;;) {
    const due = [...timers].filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    timers.delete(due);
    clock = Math.max(clock, due.at);
    due.fn();
  }
  clock = end;
}

// ── Fake DOM, sockets and WHEP clients ────────────────────────────────────

function element(tag) {
  return {
    tag,
    className: '',
    textContent: '',
    disabled: false,
    paused: true,
    muted: false,
    volume: 1,
    srcObject: null,
    children: [],
    listeners: {},
    classList: { add() {}, remove() {} },
    scrollHeight: 0,
    scrollTop: 0,
    clientHeight: 0,
    plays: 0,
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    },
    appendChild(child) {
      this.children.push(child);
    },
    removeChild(child) {
      this.children = this.children.filter((c) => c !== child);
    },
    get firstChild() {
      return this.children[0];
    },
    play() {
      this.plays++;
      this.paused = false;
      return Promise.resolve();
    },
    pause() {
      this.paused = true;
    },
  };
}

let page;
function setupPage(config, { captionsBox = false } = {}) {
  const els = {};
  for (const id of ['embed-audio', 'play-btn', 'status-dot', 'status-text']) els[id] = element(id);
  if (captionsBox) {
    for (const id of ['captions-box', 'embed-caption-history', 'embed-caption-current']) els[id] = element(id);
  }
  els['embed-config'] = { textContent: JSON.stringify(config) };
  const docListeners = {};
  const posted = [];
  const windowListeners = {};
  page = { els, posted, sockets: [], clients: [], docListeners, windowListeners };
  globalThis.document = {
    getElementById: (id) => els[id] || null,
    createElement: (tag) => element(tag),
    addEventListener: (type, fn) => {
      docListeners[type] = fn;
    },
  };
  globalThis.window = {
    location: { protocol: 'https:', host: 'portal.test' },
    parent: { postMessage: (msg, origin) => posted.push({ msg, origin }) },
    addEventListener: (type, fn) => {
      windowListeners[type] = fn;
    },
  };
  globalThis.WebSocket = class {
    constructor(url) {
      this.url = url;
      page.sockets.push(this);
    }
  };
  globalThis.createWhepClient = () => {
    const client = {
      stopped: false,
      start(opts) {
        this.opts = opts;
        this.audioEl = opts.audioEl;
      },
      stop() {
        this.stopped = true;
        this.opts.onState({ peerConnection: 'closed', audioActive: false });
      },
      setAudioElement(el, autoplay) {
        this.audioEl = el;
        this.autoplay = autoplay;
        this.opts.onState({ peerConnection: 'connected', audioActive: true });
      },
      setAudioDelayMs() {},
      resume() {},
      live() {
        this.opts.onState({ peerConnection: 'connected', audioActive: true });
      },
    };
    page.clients.push(client);
    return client;
  };
}

async function load(config, options) {
  timers.clear();
  clock = 1000;
  setupPage(config, options);
  vm.runInThisContext(EMBED_PLAYER_SRC, { filename: EMBED_PLAYER });
  page.docListeners.DOMContentLoaded();
  return page;
}

const BASE = {
  whep_url: 'https://mtx.test/demo/3/th/whep',
  caption_url: 'wss://portal.test/ws/captions/demo-3-th',
  captions_enabled: false,
  token: 'embed.jwt',
  audio_delay_ms: 0,
  source_lang_code: 'th',
  target_lang_code: 'th',
  headless: false,
  target_origin: '*',
  allowed_origins: [],
  floor_fallback: null,
};
const FALLBACK = { whep_url: 'https://mtx.test/demo/3/floor/whep', booth_id: 'demo-3-floor', channel_id: 'demo/3/floor' };
const state = (s) => ({ data: JSON.stringify({ type: 'booth:state', state: s }) });
const status = () => page.els['status-text'].textContent;
const running = () => page.clients.filter((c) => !c.stopped);

console.log('\n── Without a fallback the embed is unchanged ──');
{
  await load(BASE);
  assert(page.sockets.length === 0, 'captions off and no fallback opens no socket');
  assert(page.clients.length === 1 && page.clients[0].opts.whepUrl === BASE.whep_url, 'it plays the booth path at once');
  assert(page.clients[0].audioEl === page.els['embed-audio'], 'on the embed player');
  page.clients[0].opts.onLog('ignored');
  page.clients[0].opts.onState({ peerConnection: 'connecting', audioActive: false });
  assert(status() === 'Connecting…', 'connecting shows Connecting…');
  page.clients[0].opts.onState({ peerConnection: 'failed', audioActive: false });
  assert(status() === 'Stream unavailable', 'a failed peer shows Stream unavailable');
  page.clients[0].live();
  assert(status() === 'Live', 'and shows Live');
}

console.log('\n── Off-air booth plays the floor, then the interpretation (#1397) ──');
{
  await load({ ...BASE, floor_fallback: FALLBACK });
  const player = page.els['embed-audio'];
  assert(status() === 'Waiting for the interpreter…', 'it waits during the handover guard, got "' + status() + '"');
  assert(page.clients.length === 0, 'nothing plays yet');
  const ws = page.sockets[0];
  assert(
    ws && ws.url === 'wss://portal.test/ws/captions/demo-3-th?token=embed.jwt',
    'captions off still opens the booth socket for its status, with the embed token',
  );
  ws.onopen();
  ws.onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', text: 'hidden' }) });
  ws.onmessage({ data: 'not json' });
  ws.onmessage({ data: JSON.stringify({ type: 'booth:state' }) });
  advance(3000);
  const floor = page.clients[0];
  assert(floor && floor.opts.whepUrl === FALLBACK.whep_url && floor.audioEl === player, 'after 3 s the floor plays on the player');
  floor.live();
  assert(status() === 'Original audio', 'the status says original audio, got "' + status() + '"');

  page.els['play-btn'].listeners.click();
  await Promise.resolve();
  assert(player.plays === 1, 'Play still drives the embed player');

  ws.onmessage(state({ on_air: true }));
  const booth = page.clients[1];
  assert(booth.opts.whepUrl === BASE.whep_url && booth.audioEl !== player && booth.audioEl.muted, 'the booth warms up muted');
  assert(!floor.stopped, 'the floor plays until the booth has audio');
  booth.live();
  assert(floor.stopped && booth.audioEl === player && running().length === 1, 'the booth takes over the player');
  assert(status() === 'Live', 'and the status says Live');

  player.volume = 0.4;
  assert(player.volume === 0.4, 'volume and mute stay on the one player across switches');

  ws.onclose({ code: 4001 });
  assert(running().length === 1 && !booth.stopped && booth.audioEl === player, 'an expired session keeps the booth playing');
  assert(status() === 'Session expired', 'and shows the expired session');
  advance(60000);
  assert(page.sockets.length === 1 && page.clients.length === 2 && timers.size === 0, 'and never reconnects or switches again');
}
{
  await load({ ...BASE, floor_fallback: FALLBACK });
  const ws = page.sockets[0];
  advance(3000);
  const floor = page.clients[0];
  floor.live();
  ws.onmessage(state({ on_air: true }));
  const booth = page.clients[1];
  ws.onclose({ code: 4001 });
  assert(!floor.stopped && booth.stopped && floor.audioEl === page.els['embed-audio'], 'expiry mid-switch keeps the floor in the player');
  advance(60000);
  assert(running().length === 1 && page.clients.length === 2 && timers.size === 0, 'and the floor stays');
}

console.log('\n── 4003 and dropped sockets ──');
{
  await load({ ...BASE, floor_fallback: FALLBACK });
  page.sockets[0].onclose({ code: 1006 });
  assert(page.sockets.length === 1 && [...timers].some((t) => t.ms >= 500 && t.ms <= 1000), 'a dropped socket reconnects after a jittered back-off');
  advance(1000);
  const again = page.sockets[1];
  assert(again, 'a new socket opens');
  again.onclose({ code: 4003 });
  assert(running().length === 0 && status() === 'Stream unavailable', 'a 4003 close stops the fallback');
  advance(20000);
  assert(page.sockets.length === 2 && page.clients.length === 0, 'and nothing reconnects or starts later');
}
{
  await load({ ...BASE, captions_enabled: true }, { captionsBox: true });
  page.sockets[0].onclose({ code: 4003 });
  assert(page.clients[0].stopped === false && status() !== 'Stream unavailable', 'without a fallback a 4003 close leaves the audio as before');
}
{
  await load({ ...BASE, captions_enabled: true }, { captionsBox: true });
  page.sockets[0].onclose({ code: 4001 });
  assert(status() === 'Session expired' && page.clients[0].stopped === false, 'without a fallback 4001 still only marks the session expired');
}

console.log('\n── Headless hosts keep their messages ──');
{
  await load({ ...BASE, headless: true, floor_fallback: FALLBACK });
  advance(3000);
  page.clients[0].live();
  const msg = page.posted.at(-1).msg;
  assert(msg.type === 'booth_state' && msg.payload.state === 'connected', 'the host is told the player connected');
  page.windowListeners.message({ origin: 'https://host.test', data: { source: 'voxbento-parent', type: 'set_volume', volume: 0.25 } });
  assert(page.els['embed-audio'].volume === 0.25, 'set_volume still reaches the player during the fallback');
}

console.log('\n── Captions still render with a fallback ──');
{
  await load({ ...BASE, captions_enabled: true, floor_fallback: FALLBACK }, { captionsBox: true });
  page.sockets[0].onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', text: 'Sawasdee' }) });
  const history = page.els['embed-caption-history'];
  assert(history.children.length === 1 && history.children[0].textContent === 'Sawasdee', 'a final caption is shown');
  page.sockets[0].onmessage(state({ on_air: true }));
  assert(page.clients.length === 1 && page.clients[0].opts.whepUrl === BASE.whep_url, 'the same socket switches the audio');
}

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
