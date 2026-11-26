/**
 * Mission Control's floor language picker (portal/static/js/floor-language.js, #1397).
 *
 * Run with: node tests/test_floor_language.mjs
 */
import assert from 'node:assert/strict';

import { renderFloorLanguage } from '../portal/static/js/floor-language.js';

function fakeNode(tag) {
  return {
    tag,
    children: [],
    listeners: {},
    value: '',
    disabled: false,
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); },
    addEventListener(type, fn) { this.listeners[type] = fn; },
  };
}

function fakeDoc(csrf = 'csrf-1') {
  return {
    createElement: (tag) => fakeNode(tag),
    createTextNode: (text) => ({ text }),
    querySelector: (selector) => (selector === 'meta[name="csrf-token"]' && csrf ? { content: csrf } : null),
  };
}

const room = {
  id: 7,
  floor_language: {
    value: 'en',
    choices: [{ code: 'en', name: 'English' }, { code: 'th', name: 'Thai' }],
  },
};

function render(fetchImpl, csrf) {
  const messages = [];
  const label = renderFloorLanguage(room, { doc: fakeDoc(csrf), fetchImpl, notify: (m) => messages.push(m) });
  const select = label.children.find((node) => node.tag === 'select');
  return { label, select, messages };
}

let passed = 0;
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`  PASS: ${name}`);
}

const realDebug = console.debug;
console.debug = () => {};

await test('lists the choices and shows the current language', async () => {
  const { label, select } = render(async () => ({ ok: true }));
  assert.equal(label.tag, 'label');
  assert.equal(select.id, 'mc-floor-language-7');
  assert.deepEqual(select.children.map((o) => [o.value, o.textContent]), [['en', 'English'], ['th', 'Thai']]);
  assert.equal(select.value, 'en');
});

await test('a change posts the language with the CSRF token', async () => {
  const calls = [];
  const { select, messages } = render(async (url, options) => {
    calls.push([url, options]);
    assert.equal(select.disabled, true, 'disabled while the request runs');
    return { ok: true };
  });
  select.value = 'th';
  await select.listeners.change();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/rooms/7/floor-language');
  assert.equal(calls[0][1].method, 'POST');
  assert.equal(calls[0][1].headers['X-CSRF-Token'], 'csrf-1');
  assert.deepEqual(JSON.parse(calls[0][1].body), { language_code: 'th' });
  assert.equal(select.value, 'th');
  assert.equal(select.disabled, false);
  assert.deepEqual(messages, []);
});

await test('a refused change goes back to the saved language and says why', async () => {
  const { select, messages } = render(async () => ({
    ok: false, status: 400, json: async () => ({ detail: 'Pick a room language.' }),
  }));
  select.value = 'th';
  await select.listeners.change();
  assert.equal(select.value, 'en');
  assert.equal(select.disabled, false);
  assert.deepEqual(messages, ["Couldn't change the floor language. Pick a room language."]);
});

await test('a refusal without a readable reason gives the status', async () => {
  const { select, messages } = render(async () => ({
    ok: false, status: 502, json: async () => { throw new SyntaxError('not json'); },
  }), '');
  select.value = 'th';
  await select.listeners.change();
  assert.equal(select.value, 'en');
  assert.deepEqual(messages, ["Couldn't change the floor language. HTTP 502"]);
});

await test('a validation error list is not shown as text', async () => {
  const { select, messages } = render(async () => ({
    ok: false, status: 422, json: async () => ({ detail: [{ msg: 'bad' }] }),
  }));
  select.value = 'th';
  await select.listeners.change();
  assert.deepEqual(messages, ["Couldn't change the floor language. HTTP 422"]);
});

await test('a network failure keeps the last saved language', async () => {
  let fail = false;
  const { select, messages } = render(async () => {
    if (fail) throw new TypeError('Failed to fetch');
    return { ok: true };
  });
  select.value = 'th';
  await select.listeners.change();
  fail = true;
  select.value = 'en';
  await select.listeners.change();
  assert.equal(select.value, 'th', 'back to the last language the server accepted');
  assert.deepEqual(messages, ["Couldn't change the floor language. Failed to fetch"]);
});

await test('without a notifier a failure still restores the select', async () => {
  const label = renderFloorLanguage(room, { doc: fakeDoc(), fetchImpl: async () => ({ ok: false, status: 500, json: async () => ({}) }) });
  const select = label.children.find((node) => node.tag === 'select');
  select.value = 'th';
  await select.listeners.change();
  assert.equal(select.value, 'en');
});

console.debug = realDebug;
console.log(`\n${passed} passed, 0 failed\n`);
