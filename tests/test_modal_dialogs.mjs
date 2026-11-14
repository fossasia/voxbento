/**
 * Unit tests for the API key and OAuth app dialogs (#1243): portal/static/js/modal-dialog.js,
 * portal/static/js/developer.js and the API key dialogs in portal/static/js/admin.js.
 *
 * Run with: node tests/test_modal_dialogs.mjs
 */
import assert from 'node:assert/strict';
import { mock, test } from 'node:test';

// A just-enough DOM: ids, classes, events, focus, and dialogs that record showModal()/close().
class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
    };
    this.dataset = {};
    this.attributes = {};
    this.handlers = {};
    this.children = {};
    this.disabled = false;
    this.isConnected = true;
    this.value = '';
    this.textContent = '';
    this.innerHTML = '';
    this.style = {};
  }

  after(element) { this.next = element; element.isConnected = true; }
  remove() { this.isConnected = false; }
  select() { this.selected = true; }

  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  hasAttribute(name) { return name in this.attributes; }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  dispatch(type) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
    (this.handlers[type] || []).forEach((handler) => handler(event));
    return event;
  }
  click() {
    if (this.disabled) return;
    this.onclick?.();
    this.dispatch('click');
  }
  focus() { document.activeElement = this; }
  querySelector(selector) {
    if (selector === 'button:disabled') return Object.values(this.children).find((child) => child.disabled) || null;
    return this.children[selector] || null;
  }
}

class FakeDialog extends FakeElement {
  constructor(id) {
    super(id);
    this.calls = [];
  }
  get open() { return this.hasAttribute('open'); }
  showModal() { this.calls.push('showModal'); this.setAttribute('open', ''); }
  close() {
    if (!this.open) return;
    this.calls.push('close');
    this.removeAttribute('open');
    this.dispatch('close');
  }
  get offsetWidth() { this.calls.push(`reflow:${this.classes.has('active')}`); return 400; }
}

let elements = {};
let selectors = {};
let lists = {};
let windowHandlers = {};

function resetDom() {
  elements = {};
  selectors = {};
  lists = {};
  windowHandlers = {};
  globalThis.addEventListener = (type, handler) => (windowHandlers[type] ||= []).push(handler);
  globalThis.document = {
    activeElement: null,
    body: new FakeElement('body'),
    getElementById: (id) => elements[id] || null,
    querySelector: (selector) => selectors[selector] || null,
    querySelectorAll: (selector) => lists[selector] || [],
    addEventListener() {},
    createElement: () => new FakeElement(),
    execCommand: () => false,
  };
  document.body.dataset.managementPrefix = '/admin';
  document.activeElement = document.body;
}

// Give a fake its page id. It returns nothing, so the fake itself is never built from a call that takes an id.
function register(id, element, parent) {
  element.id = id;
  elements[id] = element;
  if (parent) parent.children[`#${id}`] = element;
}

const fireWindow = (type, extra = {}) => (windowHandlers[type] || []).forEach((handler) => handler({ type, ...extra }));
const fade = () => new Promise((resolve) => setTimeout(resolve, 250));
const tick = () => new Promise((resolve) => setImmediate(resolve));

function setClipboard(writeText) {
  // Node 20 has no global navigator, newer Node has one without a clipboard.
  Object.defineProperty(globalThis, 'navigator', { value: { clipboard: { writeText } }, configurable: true });
}

resetDom();
globalThis.window = globalThis;
const { openDialog, closeDialog, initDialog } = await import('../portal/static/js/modal-dialog.js');
const { copyToClipboard, initDeveloperDashboard } = await import('../portal/static/js/developer.js');
await import('../portal/static/js/admin.js');
const integrations = window.adminAPIKeys;

// ---------------------------------------------------------------------------
// modal-dialog.js
// ---------------------------------------------------------------------------

test('openDialog opens modally, fades in after a reflow and focuses the target', () => {
  resetDom();
  const opener = new FakeElement();
  register('opener', opener);
  const dialog = new FakeDialog('d');
  const field = new FakeElement('field');
  opener.focus();

  openDialog(dialog, field);
  openDialog(dialog, field);

  assert.deepEqual(dialog.calls, ['showModal', 'reflow:false'], 'showModal once, reflow before .active');
  assert.ok(dialog.classes.has('active'));
  assert.equal(document.activeElement, field);
});

test('openDialog without a focus target leaves focus to the browser', () => {
  resetDom();
  const dialog = new FakeDialog('d');
  openDialog(dialog);
  assert.equal(dialog.open, true);
  assert.equal(document.activeElement, document.body);
});

test('closeDialog fades out, closes, then returns focus to the opener', async () => {
  resetDom();
  const opener = new FakeElement('opener');
  opener.focus();
  const dialog = new FakeDialog('d');
  openDialog(dialog, new FakeElement('field'));

  const first = closeDialog(dialog);
  assert.equal(closeDialog(dialog), first, 'a second close joins the running fade');
  assert.ok(!dialog.classes.has('active'), '.active goes first so the fade plays');
  assert.equal(dialog.open, true, 'still open during the fade');
  await first;

  assert.deepEqual(dialog.calls.slice(-1), ['close']);
  assert.equal(document.activeElement, opener);
  await closeDialog(dialog);
  assert.equal(dialog.calls.filter((call) => call === 'close').length, 1, 'closing a closed dialog does nothing');
});

test('closeDialog skips an opener that left the page', async () => {
  resetDom();
  const opener = new FakeElement('opener');
  opener.focus();
  const dialog = new FakeDialog('d');
  const field = new FakeElement('field');
  openDialog(dialog, field);
  opener.isConnected = false;
  await closeDialog(dialog);
  assert.equal(document.activeElement, field);
});

test('without <dialog> support the popup still opens and closes through the open attribute', async () => {
  resetDom();
  const opener = new FakeElement('opener');
  opener.focus();
  const popup = new FakeElement('old-browser');
  const field = new FakeElement('field');

  openDialog(popup, field);
  assert.equal(popup.hasAttribute('open'), true);
  assert.ok(popup.classes.has('active'));
  assert.equal(document.activeElement, field);

  await closeDialog(popup);
  assert.equal(popup.hasAttribute('open'), false);
  assert.equal(document.activeElement, opener);
});

test('initDialog: Escape fades out only when canClose allows it', async () => {
  resetDom();
  let busy = true;
  const dialog = new FakeDialog('d');
  initDialog(dialog, () => !busy);
  openDialog(dialog);

  const blocked = dialog.dispatch('cancel');
  assert.equal(blocked.defaultPrevented, true);
  await fade();
  assert.equal(dialog.open, true, 'Escape waits while busy');

  busy = false;
  dialog.dispatch('cancel');
  await fade();
  assert.equal(dialog.open, false);
});

test('initDialog: a browser-forced close drops .active so the next open fades in', () => {
  resetDom();
  const dialog = new FakeDialog('d');
  initDialog(dialog);
  openDialog(dialog);
  assert.equal(dialog.dispatch('cancel').defaultPrevented, true, 'canClose defaults to allowing Escape');
  dialog.close();
  assert.ok(!dialog.classes.has('active'));
});

// ---------------------------------------------------------------------------
// developer.js
// ---------------------------------------------------------------------------

function dashboard({ afterCreate = false } = {}) {
  resetDom();
  const createButton = new FakeElement();
  register('btn-create-app', createButton);
  const createDialog = new FakeDialog();
  register('modal-create-app', createDialog);
  const appName = new FakeElement();
  register('app_name', appName);
  const createCancel = new FakeElement('create-cancel');
  createDialog.children['[data-dialog-close]'] = createCancel;
  const createForm = new FakeElement('create-form');
  createDialog.children.form = createForm;
  const copyButtons = [];
  let newAppDialog = null;
  let newAppCopy = null;
  let newAppDone = null;
  if (afterCreate) {
    newAppDialog = new FakeDialog();
    register('modal-new-secret', newAppDialog);
    newAppCopy = new FakeElement('new-app-copy');
    newAppCopy.dataset.copyText = 'copied-value';
    newAppDone = new FakeElement('new-app-done');
    newAppDialog.children['.secret-box-danger'] = newAppCopy;
    newAppDialog.children['[data-dialog-close]'] = newAppDone;
    copyButtons.push(newAppCopy);
  }
  lists['[data-copy-text]'] = copyButtons;
  initDeveloperDashboard();
  return { createButton, createDialog, appName, createCancel, createForm, newAppDialog, newAppCopy, newAppDone };
}

test('dashboard: Create New App opens the create dialog; Cancel and Escape close it', async () => {
  const page = dashboard();
  page.createButton.focus();

  page.createButton.click();
  assert.deepEqual(page.createDialog.calls[0], 'showModal');
  assert.equal(document.activeElement, page.appName);

  page.createCancel.click();
  await fade();
  assert.equal(page.createDialog.open, false);
  assert.equal(document.activeElement, page.createButton, 'focus goes back to Create New App');

  page.createButton.click();
  page.createDialog.dispatch('cancel');
  await fade();
  assert.equal(page.createDialog.open, false);
});

test('dashboard: the secret dialog opens on load and closes from its button or Escape', async () => {
  const page = dashboard({ afterCreate: true });
  assert.equal(page.newAppDialog.open, true);
  assert.deepEqual(page.newAppDialog.calls[0], 'showModal');
  assert.equal(document.activeElement, page.newAppCopy);
  assert.equal(page.createDialog.open, false, 'only the secret dialog opens');

  page.newAppDone.click();
  await fade();
  assert.equal(page.newAppDialog.open, false);

  const again = dashboard({ afterCreate: true });
  again.newAppDialog.dispatch('cancel');
  await fade();
  assert.equal(again.newAppDialog.open, false);
});

test('dashboard: the secret dialog stays closed when back/forward restores the page', () => {
  const restored = mock.method(performance, 'getEntriesByType', () => [{ type: 'back_forward' }]);
  try {
    const page = dashboard({ afterCreate: true });
    assert.deepEqual(page.newAppDialog.calls, []);
    page.newAppDialog.dispatch('cancel');
    assert.equal(page.newAppDialog.open, false);
  } finally {
    restored.mock.restore();
  }
});

test('dashboard: a second Create App click while the first is on its way is dropped', () => {
  const page = dashboard();
  assert.equal(page.createForm.dispatch('submit').defaultPrevented, false, 'the first submit goes through');
  assert.equal(page.createForm.dispatch('submit').defaultPrevented, true, 'no second app with a secret nobody sees');
});

test('dashboard: a page restored from the back/forward cache can create an app again', () => {
  const page = dashboard();
  page.createForm.dispatch('submit');
  fireWindow('pageshow', { persisted: false });
  assert.equal(page.createForm.dispatch('submit').defaultPrevented, true, 'a normal page load changes nothing');
  fireWindow('pageshow', { persisted: true });
  assert.equal(page.createForm.dispatch('submit').defaultPrevented, false);
});

test('dashboard: a page without the dialogs (no approved account) still loads', () => {
  resetDom();
  assert.doesNotThrow(() => initDeveloperDashboard());
});

test('dashboard: clicking a copy button copies its data-copy-text', async () => {
  const page = dashboard({ afterCreate: true });
  const written = [];
  setClipboard(async (text) => { written.push(text); });
  page.newAppCopy.click();
  await tick();
  assert.deepEqual(written, ['copied-value']);
});

test('copyToClipboard: success shows Copied! and resets after two seconds', async () => {
  resetDom();
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    setClipboard(async () => {});
    const button = new FakeElement('copy');
    button.dataset.copyText = 'client_1';
    const tooltip = new FakeElement('tip');
    tooltip.textContent = 'Copy to clipboard';
    button.children['.copy-tooltip'] = tooltip;

    await copyToClipboard(button);
    assert.ok(button.classes.has('copied'));
    assert.equal(tooltip.textContent, 'Copied!');
    assert.ok(tooltip.classes.has('copied-tooltip'));

    mock.timers.tick(2000);
    assert.ok(!button.classes.has('copied'));
    assert.equal(tooltip.textContent, 'Copy to clipboard');
    assert.ok(!tooltip.classes.has('copied-tooltip'));
  } finally {
    mock.timers.reset();
  }
});

test('copyToClipboard: works on a button without a tooltip', async () => {
  resetDom();
  mock.timers.enable({ apis: ['setTimeout'] });
  try {
    setClipboard(async () => {});
    const button = new FakeElement('copy');
    await copyToClipboard(button);
    assert.ok(button.classes.has('copied'));
    mock.timers.tick(2000);
    assert.ok(!button.classes.has('copied'));
  } finally {
    mock.timers.reset();
  }
});

function copyButton() {
  const button = new FakeElement('copy');
  button.dataset.copyText = 'copied-value';
  const tooltip = new FakeElement('tip');
  tooltip.textContent = 'Copy to clipboard';
  button.children['.copy-tooltip'] = tooltip;
  return { button, tooltip };
}

// Plain http outside localhost has no navigator.clipboard.
function noClipboard() {
  Object.defineProperty(globalThis, 'navigator', { value: {}, configurable: true });
}

test('copyToClipboard: without navigator.clipboard it copies through a textarea next to the button', async () => {
  resetDom();
  noClipboard();
  const copied = [];
  document.execCommand = (command) => {
    copied.push([command, document.activeElement.value, document.activeElement.selected]);
    return true;
  };
  const { button } = copyButton();

  await copyToClipboard(button);

  assert.deepEqual(copied, [['copy', 'copied-value', true]]);
  assert.equal(button.next.isConnected, false, 'the textarea is removed again');
  assert.equal(button.next.style.position, 'fixed');
  assert.equal(document.activeElement, button, 'focus goes back to the copy button');
  assert.ok(button.classes.has('copied'));
});

test('copyToClipboard: a denied clipboard logs and falls back to the textarea', async () => {
  resetDom();
  const denied = new Error('NotAllowedError');
  setClipboard(async () => { throw denied; });
  document.execCommand = () => true;
  const logged = mock.method(console, 'error', () => {});
  const { button } = copyButton();

  await copyToClipboard(button);

  assert.deepEqual(logged.mock.calls[0].arguments, ['navigator.clipboard.writeText rejected, trying the textarea fallback', denied]);
  assert.ok(button.classes.has('copied'));
  logged.mock.restore();
});

for (const [label, execCommand] of [
  ['the textarea copy is refused', () => false],
  ['the textarea copy throws', () => { throw new Error('blocked'); }],
]) {
  test(`copyToClipboard: shows Copy failed when ${label}`, async () => {
    resetDom();
    mock.timers.enable({ apis: ['setTimeout'] });
    const logged = mock.method(console, 'error', () => {});
    try {
      noClipboard();
      document.execCommand = execCommand;
      const { button, tooltip } = copyButton();

      await copyToClipboard(button);
      assert.ok(button.classes.has('copy-failed'));
      assert.ok(!button.classes.has('copied'));
      assert.equal(tooltip.textContent, 'Copy failed');
      assert.equal(logged.mock.calls.length, 1, 'the failure is logged');

      mock.timers.tick(2000);
      assert.ok(!button.classes.has('copy-failed'));
      assert.equal(tooltip.textContent, 'Copy to clipboard');
    } finally {
      logged.mock.restore();
      mock.timers.reset();
    }
  });
}

// ---------------------------------------------------------------------------
// admin.js: event API key dialogs
// ---------------------------------------------------------------------------

function response(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (body instanceof Error) throw body;
      return body;
    },
  };
}

function eventPage(routes) {
  resetDom();
  const requests = [];
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    requests.push({ url, method, options });
    const route = routes[method];
    return typeof route === 'function' ? route() : route;
  };
  const alerts = [];
  globalThis.alert = (message) => alerts.push(message);
  selectors['meta[name="csrf-token"]'] = { content: 'csrf-1' };

  const section = new FakeElement();
  register('api-keys-section', section);
  section.dataset.eventId = '7';
  const container = new FakeElement();
  register('api-keys-container', container);
  const form = new FakeElement();
  register('api-key-form', form);
  const generateButton = new FakeElement();
  register('api-key-generate-btn', generateButton);

  const createDialog = new FakeDialog();
  register('api-key-modal', createDialog);
  const name = new FakeElement();
  register('api-key-name', name, createDialog);
  const cancel = new FakeElement();
  register('api-key-cancel-btn', cancel, createDialog);
  const submit = new FakeElement('submit');
  createDialog.children.submit = submit;
  selectors['#api-key-form button[type="submit"]'] = submit;

  const successDialog = new FakeDialog();
  register('api-key-success-modal', successDialog);
  const rawValue = new FakeElement();
  register('api-key-raw-value', rawValue, successDialog);
  const copyButton = new FakeElement();
  register('api-key-copy-btn', copyButton, successDialog);

  const revokeDialog = new FakeDialog();
  register('api-key-revoke-modal', revokeDialog);
  const revokeMessage = new FakeElement();
  register('api-key-revoke-message', revokeMessage, revokeDialog);
  register('api-key-revoke-funny', new FakeElement(), revokeDialog);
  const revokeCancel = new FakeElement();
  register('api-key-revoke-cancel-btn', revokeCancel, revokeDialog);
  const revokeConfirm = new FakeElement();
  register('api-key-revoke-confirm-btn', revokeConfirm, revokeDialog);

  integrations.init();
  return {
    requests, alerts, container, form, generateButton, createDialog, name, cancel, submit,
    successDialog, rawValue, copyButton, revokeDialog, revokeMessage, revokeCancel, revokeConfirm,
  };
}

const LISTED_ROW = { id: 3, name: 'Main', preview: 'vb_ab…', created_at: '2026-11-01T00:00:00Z', active: true };

test('api keys: Generate API Key opens the dialog empty with focus in the name field', async () => {
  const page = eventPage({ GET: response(200, []) });
  page.name.value = 'old';
  page.generateButton.focus();

  integrations.showCreateModal();
  assert.deepEqual(page.createDialog.calls[0], 'showModal');
  assert.equal(page.name.value, '');
  assert.equal(document.activeElement, page.name);

  await integrations.closeModal('api-key-modal');
  assert.equal(page.createDialog.open, false);
  assert.equal(document.activeElement, page.generateButton);
});

test('api keys: showModal/closeModal ignore unknown ids', async () => {
  eventPage({ GET: response(200, []) });
  assert.doesNotThrow(() => integrations.showModal('nope'));
  const closing = integrations.closeModal('nope');
  assert.ok(closing instanceof Promise, 'callers await closeModal');
  await closing;
});

test('api keys: generate posts once, blocks Escape while running, then shows the key', async () => {
  let finish;
  const page = eventPage({
    GET: response(200, []),
    POST: () => new Promise((resolve) => { finish = () => resolve(response(200, { raw_key: 'vb_raw_123' })); }),
  });
  page.generateButton.focus();
  integrations.showCreateModal();
  page.name.value = 'Integration';
  page.copyButton.textContent = '✓ Copied';

  const running = integrations.generateKey();
  assert.equal(page.submit.disabled, true);
  assert.equal(page.cancel.disabled, true);
  page.createDialog.dispatch('cancel');
  await fade();
  assert.equal(page.createDialog.open, true, 'Escape waits for the request');

  finish();
  await running;

  const posts = page.requests.filter((r) => r.method === 'POST');
  assert.equal(posts.length, 1, 'one POST');
  assert.equal(posts[0].url, '/admin/api/events/7/api-keys');
  assert.equal(posts[0].options.headers['X-CSRF-Token'], 'csrf-1');
  assert.deepEqual(JSON.parse(posts[0].options.body), { name: 'Integration' });
  assert.equal(page.createDialog.open, false);
  assert.equal(page.successDialog.open, true);
  assert.equal(page.rawValue.value, 'vb_raw_123');
  assert.equal(page.copyButton.textContent, 'Copy');
  assert.equal(document.activeElement, page.copyButton);
  assert.equal(page.submit.disabled, false);
  assert.equal(page.cancel.disabled, false);

  await integrations.closeModal('api-key-success-modal');
  assert.equal(document.activeElement, page.generateButton, 'Done returns focus to Generate API Key');
  assert.equal(page.rawValue.value, 'vb_raw_123', 'closing does not clear anything');
});

for (const [label, reply, expected] of [
  ['a 4xx with a detail', response(400, { detail: 'Name is required' }), 'Name is required'],
  ['a 5xx with bad JSON', response(500, new SyntaxError('Unexpected token')), 'Failed to generate'],
  ['a 200 with bad JSON', response(200, new SyntaxError('Unexpected token <')), 'Unexpected token <'],
]) {
  test(`api keys: generate with ${label} alerts and keeps the dialog usable`, async () => {
    const page = eventPage({ GET: response(200, []), POST: reply });
    integrations.showCreateModal();
    await integrations.generateKey();
    assert.deepEqual(page.alerts, [expected]);
    assert.equal(page.createDialog.open, true);
    assert.equal(page.successDialog.open, false);
    assert.equal(page.submit.disabled, false);
    assert.equal(page.cancel.disabled, false);
  });
}

test('api keys: revoke asks first, sends one DELETE and shows the empty list', async () => {
  let finish;
  const lists = [[LISTED_ROW], []];
  const page = eventPage({
    GET: () => response(200, lists.shift()),
    DELETE: () => new Promise((resolve) => { finish = () => resolve(response(204, null)); }),
  });
  await tick();
  assert.match(page.container.innerHTML, /data-key-id="3"/, 'the key is listed');
  const rowButton = new FakeElement('row-revoke');
  rowButton.focus();

  integrations.revokeKey('3', 'Main');
  assert.deepEqual(page.revokeDialog.calls[0], 'showModal');
  assert.match(page.revokeMessage.textContent, /'Main'/);
  assert.equal(document.activeElement, page.revokeCancel, 'focus starts on Cancel');

  page.revokeConfirm.click();
  page.revokeConfirm.click();
  assert.equal(page.revokeConfirm.disabled, true);
  assert.equal(page.revokeCancel.disabled, true);
  page.revokeDialog.dispatch('cancel');

  // The row (and its Revoke button) is replaced when the list reloads.
  rowButton.isConnected = false;
  document.activeElement = document.body;
  finish();
  await fade();
  await tick();

  const deletes = page.requests.filter((r) => r.method === 'DELETE');
  assert.equal(deletes.length, 1, 'one DELETE');
  assert.equal(deletes[0].url, '/admin/api/events/7/api-keys/3');
  assert.equal(deletes[0].options.headers['X-CSRF-Token'], 'csrf-1');
  assert.equal(page.revokeDialog.open, false);
  assert.equal(page.container.innerHTML, '<p class="muted">No API keys generated yet.</p>');
  assert.equal(document.activeElement, page.generateButton, 'focus lands on Generate API Key');
  assert.equal(page.revokeConfirm.disabled, false);
  assert.equal(integrations.revokeKeyId, null);
});

test('api keys: after revoke, focus that is still on the page is left alone', async () => {
  const page = eventPage({ GET: response(200, []), DELETE: response(204, null) });
  const rowButton = new FakeElement('row-revoke');
  rowButton.focus();
  integrations.revokeKey('3', 'Main');
  await integrations.confirmRevokeKey();
  assert.equal(document.activeElement, rowButton);
  assert.deepEqual(page.revokeDialog.calls, ['showModal', 'reflow:false', 'close']);
});

test('api keys: an older list answer that lands last does not redraw the table', async () => {
  const answers = [];
  const page = eventPage({ GET: () => new Promise((resolve) => answers.push(resolve)) });
  const newer = integrations.loadKeys();
  answers[1](response(200, []));
  await newer;
  answers[0](response(200, [LISTED_ROW]));
  await tick();
  assert.equal(page.container.innerHTML, '<p class="muted">No API keys generated yet.</p>');
});

test('api keys: a failed list request shows the error', async () => {
  const page = eventPage({ GET: response(500, {}) });
  const logged = mock.method(console, 'error', () => {});
  await integrations.loadKeys();
  assert.equal(page.container.innerHTML, '<p class="text-danger">Failed to load API keys.</p>');
  logged.mock.restore();
});

test('api keys: an older list request that fails last does not replace the table', async () => {
  const answers = [];
  const page = eventPage({ GET: () => new Promise((resolve) => answers.push(resolve)) });
  const logged = mock.method(console, 'error', () => {});
  const newer = integrations.loadKeys();
  answers[1](response(200, []));
  await newer;
  answers[0](response(500, {}));
  await tick();
  assert.equal(page.container.innerHTML, '<p class="muted">No API keys generated yet.</p>');
  logged.mock.restore();
});

test('api keys: a revoke started while the last one reloads the list keeps its own key', async () => {
  const gets = [];
  const page = eventPage({ GET: () => new Promise((resolve) => gets.push(resolve)), DELETE: () => response(200, null) });
  gets[0](response(200, []));
  await tick();

  integrations.revokeKey('3', 'Main');
  const first = integrations.confirmRevokeKey();
  await fade();
  await tick();
  assert.equal(page.revokeDialog.open, false, 'the first revoke is done and its list is reloading');

  integrations.revokeKey('4', 'Other');
  gets[1](response(200, []));
  await first;
  assert.equal(integrations.revokeKeyId, '4');
  assert.equal(page.revokeConfirm.disabled, false);
  assert.equal(document.activeElement, page.revokeCancel, 'focus stays in the second dialog');

  const second = integrations.confirmRevokeKey();
  await fade();
  await tick();
  gets[2](response(200, []));
  await second;
  assert.deepEqual(page.requests.filter((r) => r.method === 'DELETE').map((r) => r.url), [
    '/admin/api/events/7/api-keys/3',
    '/admin/api/events/7/api-keys/4',
  ]);
});

test('api keys: Revoke can be tried again after a failed request', async () => {
  const replies = [response(500, {}), response(200, null)];
  const page = eventPage({ GET: response(200, []), DELETE: () => replies.shift() });
  integrations.revokeKey('3', 'Main');
  await integrations.confirmRevokeKey();
  assert.equal(page.revokeDialog.open, true);
  await integrations.confirmRevokeKey();
  assert.deepEqual(page.requests.filter((r) => r.method === 'DELETE').map((r) => r.url), [
    '/admin/api/events/7/api-keys/3',
    '/admin/api/events/7/api-keys/3',
  ]);
  assert.equal(page.revokeDialog.open, false);
});

test('api keys: Cancel on revoke closes without a request', async () => {
  const page = eventPage({ GET: response(200, []) });
  integrations.revokeKey('3', 'Main');
  await integrations.closeModal('api-key-revoke-modal');
  assert.deepEqual(page.revokeDialog.calls, ['showModal', 'reflow:false', 'close']);
  assert.equal(page.requests.filter((r) => r.method === 'DELETE').length, 0);
});

test('api keys: a failed revoke alerts and leaves the dialog open', async () => {
  const page = eventPage({ GET: response(200, []), DELETE: response(500, {}) });
  integrations.revokeKey('3', 'Main');
  await integrations.confirmRevokeKey();
  assert.deepEqual(page.alerts, ['Error revoking key: Failed to revoke']);
  assert.equal(page.revokeDialog.open, true);
  assert.equal(page.revokeConfirm.disabled, false);
  assert.equal(page.revokeCancel.disabled, false);
});
