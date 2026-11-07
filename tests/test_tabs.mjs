/**
 * Unit tests for the admin tab bar (portal/static/js/tabs.js), used by the booth page.
 *
 * Run with: node tests/test_tabs.mjs
 */
import assert from 'node:assert/strict';

import { initTabs } from '../portal/static/js/tabs.js';

// A just-enough DOM: attributes, class toggles, events and focus.
class FakeElement {
  constructor(attributes = {}) {
    this.attributes = { ...attributes };
    this.handlers = {};
    this.classes = new Set();
    this.hidden = false;
    this.tabIndex = 0;
    this.focused = false;
    this.classList = { toggle: (name, on) => (on ? this.classes.add(name) : this.classes.delete(name)) };
  }

  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(type, handler) { this.handlers[type] = handler; }
  focus() { this.focused = true; }
}

const NAMES = ['overview', 'captions', 'settings'];

// A page with a tab bar, one panel per tab and one link that opens a tab.
function createPage({ hash = '', stored = null, storage = {} } = {}) {
  const tabs = NAMES.map((name) => new FakeElement({ 'data-booth-tab': name }));
  const panels = NAMES.map((name) => new FakeElement({ 'data-booth-panel': name }));
  const link = new FakeElement({ 'data-booth-tab-link': 'captions' });
  const tabList = new FakeElement();
  tabList.hidden = true;
  tabList.querySelectorAll = () => tabs;

  const written = {};
  const replaced = [];
  globalThis.document = {
    querySelector: (selector) => (selector === '[data-booth-tabs]' ? tabList : null),
    querySelectorAll: (selector) => ({
      '[data-booth-panel]': panels,
      '[data-booth-tab-link]': [link],
    })[selector] || [],
  };
  globalThis.location = { hash, pathname: '/booths/7/', search: '?saved=captions' };
  globalThis.history = { replaceState: (_state, _title, url) => replaced.push(url) };
  globalThis.sessionStorage = {
    getItem: (key) => (key in storage ? storage[key] : stored),
    setItem: (key, value) => { written[key] = value; },
  };

  initTabs({ name: 'booth', storageKey: 'booth-tab:7' });
  return { tabs, panels, link, tabList, written, replaced };
}

const selected = (page) => page.tabs.filter((tab) => tab.attributes['aria-selected'] === 'true').map((tab) => tab.attributes['data-booth-tab']);
const shown = (page) => page.panels.filter((panel) => !panel.hidden).map((panel) => panel.attributes['data-booth-panel']);

function key(name) {
  return { key: name, prevented: false, preventDefault() { this.prevented = true; } };
}

// The first tab opens by default, the bar appears and only its panel shows.
{
  const page = createPage();
  assert.equal(page.tabList.hidden, false);
  assert.deepEqual(selected(page), ['overview']);
  assert.deepEqual(shown(page), ['overview']);
  assert.deepEqual(page.tabs.map((tab) => tab.tabIndex), [0, -1, -1], 'only the open tab is in the tab order');
  assert.ok(page.tabs[0].classes.has('is-active'));
  assert.deepEqual(page.replaced, ['/booths/7/?saved=captions#overview'], 'the hash follows the tab');
}

// A #hash in the link chooses the tab, which is how a save redirects back to it.
{
  const page = createPage({ hash: '#captions', stored: 'settings' });
  assert.deepEqual(shown(page), ['captions']);
  assert.equal(page.written['booth-tab:7'], 'captions');
  assert.deepEqual(page.replaced, [], 'the hash is already right');
}

// Without a hash the remembered tab opens; one that no longer exists falls back to the first.
assert.deepEqual(shown(createPage({ stored: 'settings' })), ['settings']);
assert.deepEqual(shown(createPage({ hash: '#nonsense' })), ['overview']);
assert.deepEqual(shown(createPage({ stored: 'gone' })), ['overview']);

// Clicking a tab opens its panel and remembers it.
{
  const page = createPage();
  page.tabs[2].handlers.click();
  assert.deepEqual(selected(page), ['settings']);
  assert.deepEqual(shown(page), ['settings']);
  assert.equal(page.written['booth-tab:7'], 'settings');
  assert.equal(page.replaced.at(-1), '/booths/7/?saved=captions#settings');
  assert.equal(page.tabs[2].focused, false, 'a click does not steal focus');
}

// Arrow keys move between tabs and wrap round; Home and End jump to the ends.
{
  const page = createPage();
  const press = (index, name) => {
    const e = key(name);
    page.tabs[index].handlers.keydown(e);
    return e;
  };
  assert.equal(press(0, 'ArrowRight').prevented, true);
  assert.deepEqual(selected(page), ['captions']);
  assert.equal(page.tabs[1].focused, true, 'the keyboard moves focus with the tab');
  press(2, 'ArrowRight');
  assert.deepEqual(selected(page), ['overview'], 'wraps to the first tab');
  press(0, 'ArrowLeft');
  assert.deepEqual(selected(page), ['settings'], 'wraps to the last tab');
  press(1, 'Home');
  assert.deepEqual(selected(page), ['overview']);
  press(0, 'End');
  assert.deepEqual(selected(page), ['settings']);
  assert.equal(press(0, 'a').prevented, false, 'other keys are left alone');
  assert.deepEqual(selected(page), ['settings']);
}

// A link elsewhere on the page opens a tab and moves focus to it.
{
  const page = createPage();
  const e = key('click');
  page.link.handlers.click(e);
  assert.equal(e.prevented, true);
  assert.deepEqual(shown(page), ['captions']);
  assert.equal(page.tabs[1].focused, true);
}

// Storage that throws (private mode) does not break the tabs.
{
  const page = createPage();
  globalThis.sessionStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
  const debug = console.debug;
  console.debug = () => {}; // tabs.js logs the blocked write; keep the test output clean
  page.tabs[1].handlers.click();
  console.debug = debug;
  assert.deepEqual(shown(page), ['captions']);
}

// A page without a tab bar (a coordinator's view) is left alone.
{
  globalThis.document = { querySelector: () => null, querySelectorAll: () => [] };
  assert.equal(initTabs({ name: 'booth', storageKey: 'x' }), undefined);
}

console.log('tabs tests passed');
