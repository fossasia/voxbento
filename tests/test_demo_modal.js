'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '../portal/static/js/demo-modal.js'),
  'utf8',
);

class FakeElement {
  constructor(id = '') {
    this.id = id;
    this.attributes = {};
    this.classNames = new Set();
    this.listeners = new Map();
    this.required = false;
    this.disabled = false;
    this.value = '';
    this.textContent = '';
    this.classList = {
      add: (...names) => names.forEach((name) => this.classNames.add(name)),
      remove: (...names) => names.forEach((name) => this.classNames.delete(name)),
      contains: (name) => this.classNames.has(name),
    };
  }

  addEventListener(type, handler) {
    const handlers = this.listeners.get(type) || [];
    handlers.push(handler);
    this.listeners.set(type, handlers);
  }

  dispatch(type) {
    const event = {
      type,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
    };
    for (const handler of this.listeners.get(type) || []) {
      handler.call(this, event);
    }
    return event;
  }

  querySelector(selector) {
    return selector === 'span' ? this.label : null;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }
}

function createModal(fetchImpl = () => Promise.resolve({ json: async () => ({ ok: false }) })) {
  const ids = [
    'demo-modal',
    'demo-request-form',
    'demo-step-1',
    'demo-step-2',
    'demo-submit-btn',
    'demo-spinner',
    'demo-success',
    'demo-error',
    'demo-email',
    'demo-firstname',
    'demo-lastname',
    'demo-phonecode',
    'demo-phone',
    'demo-company',
    'demo-country',
    'demo-industry',
    'demo-size',
    'demo-language',
  ];
  const elements = new Map(ids.map((id) => [id, new FakeElement(id)]));
  const openButton = new FakeElement('open-demo');
  const closeButton = new FakeElement('close-demo');
  const timers = new Map();
  let nextTimerId = 0;
  const submitButton = elements.get('demo-submit-btn');
  submitButton.label = new FakeElement('submit-label');

  const document = {
    body: new FakeElement('body'),
    getElementById: (id) => elements.get(id) || null,
    querySelectorAll(selector) {
      if (selector === '[data-demo-modal-open]') return [openButton];
      if (selector === '[data-demo-modal-close]') return [closeButton];
      return [];
    },
  };
  const context = vm.createContext({
    AbortController,
    DOMException,
    document,
    fetch: fetchImpl,
    console: { error() {} },
    setTimeout(callback, delay) {
      const id = ++nextTimerId;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    window: { location: { reload() {} } },
  });
  vm.runInContext(source, context);

  return {
    elements,
    openButton,
    closeButton,
    document,
    timers,
    submit() {
      return elements.get('demo-request-form').dispatch('submit');
    },
  };
}

test('step-two fields validate while visible and reset after closing and reopening', () => {
  const modal = createModal();
  const stepTwoFields = [
    'demo-firstname',
    'demo-lastname',
    'demo-phone',
    'demo-company',
    'demo-country',
    'demo-language',
  ];

  modal.openButton.dispatch('click');
  assert.equal(modal.elements.get('demo-modal').getAttribute('aria-hidden'), 'false');
  assert.equal(modal.elements.get('demo-step-1').classList.contains('is-hidden'), false);

  modal.submit();
  assert.equal(modal.elements.get('demo-step-2').classList.contains('is-hidden'), false);
  for (const id of stepTwoFields) {
    assert.equal(modal.elements.get(id).required, true, `${id} should be required in step two`);
  }

  modal.closeButton.dispatch('click');
  assert.equal(modal.elements.get('demo-modal').getAttribute('aria-hidden'), 'true');
  modal.openButton.dispatch('click');
  assert.equal(modal.elements.get('demo-step-1').classList.contains('is-hidden'), false);
  assert.equal(modal.elements.get('demo-step-2').classList.contains('is-hidden'), true);
  for (const id of stepTwoFields) {
    assert.equal(modal.elements.get(id).required, false, `${id} should not block step one`);
  }
});

test('a timed-out demo request shows an error and restores the retry controls', async () => {
  const modal = createModal((_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener(
      'abort',
      () => reject(new DOMException('The operation was aborted.', 'AbortError')),
      { once: true },
    );
  }));
  modal.openButton.dispatch('click');
  modal.submit();
  modal.submit();

  assert.equal(modal.elements.get('demo-submit-btn').disabled, true);
  assert.equal(modal.elements.get('demo-spinner').classList.contains('is-active'), true);
  const [timeoutId, timeout] = [...modal.timers.entries()][0];
  assert.equal(timeout.delay, 15000);
  modal.timers.delete(timeoutId);
  timeout.callback();
  await new Promise((resolve) => setImmediate(resolve));

  const error = modal.elements.get('demo-error');
  assert.equal(error.classList.contains('is-visible'), true);
  assert.match(error.textContent, /timed out/i);
  assert.equal(modal.elements.get('demo-submit-btn').disabled, false);
  assert.equal(modal.elements.get('demo-spinner').classList.contains('is-active'), false);
});
