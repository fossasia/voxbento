'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../portal/static/js/subtitle-overlay.js'), 'utf8');
const context = vm.createContext({ document: { getElementById: () => null }, URL, URLSearchParams, AbortController });
vm.runInContext(source.replace(/^export\s+/gm, ''), context);

function setup(responses = []) {
  const sockets = [], timers = new Map(), requests = [], statuses = [], captions = [];
  let id = 0;
  const schedule = (fn, ms) => { timers.set(++id, { fn, ms }); return id; };
  class Socket {
    constructor(url) { this.url = url; sockets.push(this); }
    close() { this.closed = true; this.onclose?.({ code: 1000 }); }
  }
  const stop = context.startConnection({
    boothId: 'conference-1-ar', initialToken: 'expired',
    location: { href: 'https://example.test/overlay/conference/1/ar?code=secret&lines=2', protocol: 'https:', host: 'example.test' },
    onCaption: data => captions.push(data), onStatus: status => statuses.push(status),
    WebSocketClass: Socket, schedule, cancel: id => timers.delete(id),
    fetchSession: async (url, options) => {
      requests.push({ url, options });
      const next = responses.shift();
      if (next instanceof Error) throw next;
      return { ok: next.status === 200, headers: { get: () => next.retryAfter ?? null }, json: async () => ({ token: next.token || 'fresh', booth_id: next.booth || 'conference-1-ar' }), ...next };
    },
  });
  async function tick() {
    const entry = [...timers.entries()].sort((a, b) => a[1].ms - b[1].ms)[0];
    assert.ok(entry, 'a recovery timer exists');
    timers.delete(entry[0]);
    entry[1].fn();
    // Drain the fetch and response.json continuations.
    await new Promise(resolve => setImmediate(resolve));
  }
  return { sockets, timers, requests, statuses, captions, stop, tick };
}

test('expired token refreshes authorization and resumes Arabic captions', async () => {
  const c = setup([{ status: 200 }]);
  c.sockets[0].onopen();
  c.sockets[0].onclose({ code: 4001 });
  await c.tick();
  assert.equal(c.requests[0].url, 'https://example.test/overlay/conference/1/ar/session?code=secret');
  assert.equal(c.requests[0].options.cache, 'no-store');
  assert.equal(c.requests[0].options.credentials, 'same-origin');
  assert.match(c.sockets[1].url, /token=fresh$/);
  c.sockets[1].onopen();
  c.sockets[1].onmessage({ data: JSON.stringify({ type: 'translated_caption', text: 'مرحباً بالعالم' }) });
  assert.equal(c.captions[0].text, 'مرحباً بالعالم');
  assert.equal(c.statuses.at(-1), 'receiving');
  c.stop();
  assert.equal(c.timers.size, 0);
});

for (const status of [401, 403, 404]) {
  test(`revoked access (${status}) stops instead of looping`, async () => {
    const c = setup([{ status }]);
    c.sockets[0].onopen();
    c.sockets[0].onclose({ code: 4001 });
    await c.tick();
    assert.equal(c.sockets.length, 1);
    assert.equal(c.timers.size, 0);
    assert.equal(c.statuses.at(-1), 'denied');
  });
}

test('repeated token rejection and wrong scope stop', async () => {
  const c = setup([{ status: 200 }]);
  c.sockets[0].onclose({ code: 4001 });
  await c.tick();
  c.sockets[1].onclose({ code: 4001 });
  assert.equal(c.timers.size, 0);
  const other = setup();
  other.sockets[0].onclose({ code: 4003 });
  assert.equal(other.timers.size, 0);
});

test('HTTP throttling respects Retry-After, then reconnects', async () => {
  const c = setup([{ status: 429, retryAfter: '60' }, { status: 200 }]);
  c.sockets[0].onclose({ code: 1006 });
  await c.tick();
  assert.equal([...c.timers.values()][0].ms, 60000);
  await c.tick();
  assert.equal(c.sockets.length, 2);
  c.stop();
});

test('network and server failures back off and recover', async () => {
  const c = setup([new Error('offline'), { status: 503 }, { status: 200 }]);
  c.sockets[0].onclose({ code: 1006 });
  await c.tick();
  assert.equal([...c.timers.values()][0].ms, 2000);
  await c.tick();
  assert.equal([...c.timers.values()][0].ms, 4000);
  await c.tick();
  assert.equal(c.sockets.length, 2);
  c.stop();
});

test('policy limits wait and stop cleans up pending reconnects', () => {
  const c = setup();
  c.sockets[0].onclose({ code: 1008 });
  assert.equal([...c.timers.values()][0].ms, 60000);
  c.stop();
  assert.equal(c.timers.size, 0);
});

test('a refresh cannot switch the selected channel', async () => {
  const c = setup([{ status: 200, booth: 'other-2-en' }]);
  c.sockets[0].onclose({ code: 1006 });
  await c.tick();
  assert.equal(c.sockets.length, 1);
  assert.equal(c.statuses.at(-1), 'denied');
});
