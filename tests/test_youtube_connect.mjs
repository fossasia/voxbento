import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../portal/static/js/youtube-connect.js', import.meta.url), 'utf8');
const flush = async () => { for (let i = 0; i < 20; i += 1) await new Promise(setImmediate); };

function element(extra = {}) {
  return {
    hidden: true, value: '', textContent: '', className: '', dataset: {}, handlers: {},
    addEventListener(type, handler) { this.handlers[type] = handler; },
    replaceChildren(...children) { this.children = children; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    ...extra,
  };
}

// Runs the room page script against a fake DOM. `routes` answers fetch by "METHOD url".
async function page(routes) {
  const panels = [element({ dataset: { youtubeState: 'disconnected' } }), element({ dataset: { youtubeState: 'connected' } })];
  const elements = {
    'youtube-connect': element({
      dataset: {
        statusUrl: '/yt', broadcastsUrl: '/yt/broadcasts', broadcastUrl: '/room/yt', roomStatusUrl: '/room/status',
        captionsUrl: '/room/yt/captions',
      },
      querySelectorAll: (selector) => (selector === '[data-youtube-state]' ? panels : []),
    }),
    'youtube-broadcast-select': element({ value: '' }),
  };
  for (const id of ['youtube-message', 'youtube-channel', 'youtube-broadcast-form', 'youtube-broadcast-link',
    'youtube-status-line', 'youtube-unlink', 'youtube-signin-again', 'youtube-disconnect', 'youtube-captions',
    'youtube-captions-select', 'youtube-captions-status']) {
    elements[id] = element();
  }
  const calls = [];
  const timers = [];
  const context = vm.createContext({
    document: {
      hidden: false,
      getElementById: (id) => elements[id],
      querySelector: (selector) => (selector === 'meta[name="csrf-token"]' ? { content: 'csrf-123' } : null),
    },
    window: { location: { search: '', pathname: '/room/', hash: '#audio' }, addEventListener() {}, confirm: () => true },
    history: { replaceState() {} },
    URLSearchParams,
    CSS: { escape: (value) => value },
    Option: class { constructor(text, value) { this.text = text; this.value = value; } },
    Event: class { constructor(type) { this.type = type; } },
    console: { error() {} },
    setInterval: (callback) => { timers.push(callback); return timers.length; },
    clearInterval() {},
    fetch: async (url, options = {}) => {
      const key = `${options.method || 'GET'} ${url}`;
      calls.push({ key, body: options.body ? JSON.parse(options.body) : undefined, headers: options.headers || {} });
      const [status, body] = routes[key] ? await routes[key]() : [404, { detail: 'not found' }];
      return { ok: status < 400, status, json: async () => body };
    },
  });
  vm.runInContext(source, context);
  await flush();
  return { elements, calls, timers };
}

const connected = () => [200, { available: true, connected: true, channel_title: 'Test Channel', last_error: null }];
const broadcasts = () => [200, { broadcasts: [{ id: 'LiveVideo01', title: 'Talk', life_cycle_status: 'live' }] }];
const linked = () => [200, { broadcast_id: 'LiveVideo01', title: 'Talk', destination_configured: false }];
const whipStatus = () => [200, { enabled: true, mode: 'whip', destination: { configured: false } }];

// The input changed last wins: a stale pasted link never overrides a later list choice, and back.
{
  const { elements, calls } = await page({
    'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': whipStatus, 'PUT /room/yt': linked,
  });
  const link = elements['youtube-broadcast-link'];
  const select = elements['youtube-broadcast-select'];
  link.value = 'https://youtu.be/AAAAAAAAAAA';
  link.handlers.input();
  select.value = 'LiveVideo01';
  select.handlers.change();
  assert.equal(link.value, '');
  elements['youtube-broadcast-form'].handlers.submit({ preventDefault() {} });
  await flush();
  assert.deepEqual(calls.filter((call) => call.key === 'PUT /room/yt').map((call) => call.body), [{ broadcast: 'LiveVideo01' }]);
  // Changes carry the page's CSRF token; reads do not need it.
  const put = calls.find((call) => call.key === 'PUT /room/yt');
  assert.equal(put.headers['X-CSRF-Token'], 'csrf-123');
  assert.equal(put.headers['Content-Type'], 'application/json');
  assert.equal(calls.find((call) => call.key === 'GET /yt').headers['X-CSRF-Token'], undefined);

  link.value = 'https://youtu.be/BBBBBBBBBBB';
  link.handlers.input();
  assert.equal(select.value, '');
  elements['youtube-broadcast-form'].handlers.submit({ preventDefault() {} });
  await flush();
  assert.equal(calls.filter((call) => call.key === 'PUT /room/yt').at(-1).body.broadcast, 'https://youtu.be/BBBBBBBBBBB');
}

// Expired access offers "Sign in with YouTube again" without disconnecting.
{
  const reauth = 'YouTube access was revoked or expired. Sign in with YouTube again.';
  const { elements } = await page({
    'GET /yt': () => [200, { available: true, connected: true, channel_title: 'Test Channel', last_error: reauth }],
    'GET /yt/broadcasts': () => [409, { detail: reauth, last_error: reauth }],
    'GET /room/status': whipStatus,
  });
  assert.equal(elements['youtube-signin-again'].hidden, false);
  assert.equal(elements['youtube-message'].textContent, reauth);
  assert.equal(elements['youtube-message'].className, 'alert alert-error');
}
{
  const { elements } = await page({ 'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': whipStatus });
  assert.equal(elements['youtube-signin-again'].hidden, true);
}

// Relay mode switched on while the page is open: the broadcast is linked again once, so the key is filled in.
{
  let status = { enabled: true, mode: 'whip', destination: { configured: false } };
  const youtube = { broadcast_id: 'LiveVideo01', title: 'Talk', life_cycle_status: 'ready' };
  const { elements, calls, timers } = await page({
    'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': () => [200, { ...status, youtube }],
    'PUT /room/yt': () => [200, { broadcast_id: 'LiveVideo01', title: 'Talk', destination_configured: true }],
  });
  const puts = () => calls.filter((call) => call.key === 'PUT /room/yt');
  assert.equal(puts().length, 0);
  status = { enabled: true, mode: 'rtmps_relay', destination: { configured: false } };
  await timers[0]();
  await flush();
  assert.deepEqual(puts().map((call) => call.body), [{ broadcast: 'LiveVideo01' }]);
  assert.match(elements['youtube-message'].textContent, /filled in the stream key/);
  // Later polls in relay mode do nothing more, even while the key is still missing.
  await timers[0]();
  await flush();
  assert.equal(puts().length, 1);
}

// A destination the owner removed stays removed: polling never fills the key back in.
{
  let status = { enabled: true, mode: 'rtmps_relay', destination: { configured: true } };
  const youtube = { broadcast_id: 'LiveVideo01', title: 'Talk', life_cycle_status: 'live' };
  const { calls, timers } = await page({
    'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': () => [200, { ...status, youtube }],
    'PUT /room/yt': linked,
  });
  status = { enabled: true, mode: 'rtmps_relay', destination: { configured: false } };
  await timers[0]();
  await flush();
  await timers[0]();
  await flush();
  assert.equal(calls.filter((call) => !call.key.startsWith('GET ')).length, 0);
}

// Reloading a relay room without a destination does not fill one in either.
{
  const relayStatus = () => [200, {
    enabled: true, mode: 'rtmps_relay', destination: { configured: false },
    youtube: { broadcast_id: 'LiveVideo01', title: 'Talk', life_cycle_status: 'ready' },
  }];
  const { calls, timers } = await page({
    'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': relayStatus, 'PUT /room/yt': linked,
  });
  await timers[0]();
  await flush();
  assert.equal(calls.filter((call) => !call.key.startsWith('GET ')).length, 0);
}

// Captions on YouTube: shown for a linked broadcast, choices from the server, changes carry the CSRF token.
{
  const choices = [{ value: 'floor', label: 'Spoken language (en)' }, { value: 'fr', label: 'French' }];
  let captions = { language: null, state: 'off', reason: '', last_ok_at: null, choices };
  const status = () => [200, {
    enabled: true, mode: 'rtmps', destination: { configured: false },
    youtube: { broadcast_id: 'LiveVideo01', title: 'Talk', life_cycle_status: 'ready', captions },
  }];
  let answer = () => [200, { ...captions, language: 'fr', state: 'waiting' }];
  const { elements, calls } = await page({
    'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': status,
    'PUT /room/yt/captions': () => answer(),
  });
  const box = elements['youtube-captions'];
  const select = elements['youtube-captions-select'];
  assert.equal(box.hidden, false);
  assert.deepEqual(select.children.map((option) => option.value), ['', 'floor', 'fr']);
  assert.equal(select.value, '');
  assert.match(elements['youtube-captions-status'].textContent, /^Off/);

  select.value = 'fr';
  select.handlers.change();
  await flush();
  const put = calls.find((call) => call.key === 'PUT /room/yt/captions');
  assert.deepEqual(put.body, { language: 'fr' });
  assert.equal(put.headers['X-CSRF-Token'], 'csrf-123');
  assert.match(elements['youtube-captions-status'].textContent, /Waiting for speech/);

  // A refusal is shown and the select goes back to the language really in effect.
  answer = () => [409, { detail: 'Captions must be set up before the broadcast starts.' }];
  select.value = 'floor';
  select.handlers.change();
  await flush();
  assert.equal(select.value, 'fr');
  assert.equal(elements['youtube-message'].textContent, 'Captions must be set up before the broadcast starts.');
  assert.equal(elements['youtube-message'].className, 'alert alert-error');

  // Off sends null.
  answer = () => [200, { ...captions, language: null, state: 'off' }];
  select.value = '';
  select.handlers.change();
  await flush();
  assert.equal(calls.filter((call) => call.key === 'PUT /room/yt/captions').at(-1).body.language, null);
}

// A slow caption change locks the selector, and a status read that started before it cannot undo it.
{
  const choices = [{ value: 'floor', label: 'Spoken language (en)' }, { value: 'fr', label: 'French' }];
  const off = { language: null, state: 'off', reason: '', last_ok_at: null, choices };
  const statusWith = (captions) => [200, {
    enabled: true, mode: 'rtmps', destination: { configured: false },
    youtube: { broadcast_id: 'LiveVideo01', title: 'Talk', life_cycle_status: 'ready', captions },
  }];
  let statusAnswer = () => statusWith(off);
  let putAnswer = () => [200, { ...off, language: 'fr', state: 'waiting' }];
  const { elements, calls, timers } = await page({
    'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': () => statusAnswer(),
    'PUT /room/yt/captions': () => putAnswer(),
  });
  const select = elements['youtube-captions-select'];

  // The 60-second poll starts and is still waiting for the server, which saw captions as off.
  let finishStatus;
  statusAnswer = () => new Promise((resolve) => { finishStatus = () => resolve(statusWith(off)); });
  timers[0]();
  await flush();

  let finishPut;
  putAnswer = () => new Promise((resolve) => { finishPut = () => resolve([200, { ...off, language: 'fr', state: 'waiting' }]); });
  select.value = 'fr';
  select.handlers.change();
  await flush();
  assert.equal(select.disabled, true);
  finishPut();
  await flush();
  assert.equal(select.disabled, false);
  assert.equal(select.value, 'fr');

  finishStatus();
  await flush();
  assert.equal(select.value, 'fr');
  assert.match(elements['youtube-captions-status'].textContent, /Waiting for speech/);

  // A poll that fires during a change is skipped.
  putAnswer = () => new Promise((resolve) => { finishPut = () => resolve([200, { ...off, language: 'floor', state: 'waiting' }]); });
  select.value = 'floor';
  select.handlers.change();
  await flush();
  const reads = calls.filter((call) => call.key === 'GET /room/status').length;
  timers[0]();
  await flush();
  assert.equal(calls.filter((call) => call.key === 'GET /room/status').length, reads);
  finishPut();
  await flush();
  assert.equal(select.value, 'floor');
}

// A failing target explains itself; an unlinked room hides the control.
{
  const failing = { language: 'floor', state: 'failing', reason: 'YouTube rejected the caption (HTTP 403).', last_ok_at: null, choices: [] };
  const { elements } = await page({
    'GET /yt': connected, 'GET /yt/broadcasts': broadcasts,
    'GET /room/status': () => [200, { enabled: false, mode: 'whip', destination: {}, youtube: { broadcast_id: 'X', captions: failing } }],
  });
  assert.equal(elements['youtube-captions-status'].textContent, 'Not posting: YouTube rejected the caption (HTTP 403).');
}
{
  const { elements } = await page({ 'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': whipStatus });
  assert.equal(elements['youtube-captions'].hidden, true);
}

console.log('youtube-connect.js: ok');
