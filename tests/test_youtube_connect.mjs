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
      dataset: { statusUrl: '/yt', broadcastsUrl: '/yt/broadcasts', broadcastUrl: '/room/yt', roomStatusUrl: '/room/status' },
      querySelectorAll: (selector) => (selector === '[data-youtube-state]' ? panels : []),
    }),
    'youtube-broadcast-select': element({ value: '' }),
  };
  for (const id of ['youtube-message', 'youtube-channel', 'youtube-broadcast-form', 'youtube-broadcast-link',
    'youtube-status-line', 'youtube-unlink', 'youtube-signin-again', 'youtube-disconnect']) {
    elements[id] = element();
  }
  const calls = [];
  const timers = [];
  const context = vm.createContext({
    document: { hidden: false, getElementById: (id) => elements[id], querySelector: () => null },
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
      calls.push({ key, body: options.body ? JSON.parse(options.body) : undefined });
      const [status, body] = routes[key] ? routes[key]() : [404, { detail: 'not found' }];
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

// Relay mode switched on after linking: the broadcast is linked again once, so the key is filled in.
{
  const relayStatus = () => [200, {
    enabled: true, mode: 'rtmps_relay', destination: { configured: false },
    youtube: { broadcast_id: 'LiveVideo01', title: 'Talk', life_cycle_status: 'ready' },
  }];
  const { elements, calls, timers } = await page({
    'GET /yt': connected, 'GET /yt/broadcasts': broadcasts, 'GET /room/status': relayStatus,
    'PUT /room/yt': () => [200, { broadcast_id: 'LiveVideo01', title: 'Talk', destination_configured: true }],
  });
  assert.deepEqual(calls.filter((call) => call.key === 'PUT /room/yt').map((call) => call.body), [{ broadcast: 'LiveVideo01' }]);
  assert.match(elements['youtube-message'].textContent, /filled in the stream key/);
  await timers[0]();
  await flush();
  assert.equal(calls.filter((call) => call.key === 'PUT /room/yt').length, 1);
}

console.log('youtube-connect.js: ok');
