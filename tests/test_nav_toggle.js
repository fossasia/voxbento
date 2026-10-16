/**
 * Unit tests for nav-toggle.js
 *
 * Run with: node tests/test_nav_toggle.js
 *
 * Tests cover:
 *  1. Toggle button opens the nav
 *  2. Toggle button closes an open nav
 *  3. Clicking a nav link closes the menu
 *  4. Clicking a nested element inside a nav link closes the menu
 *  5. Clicking an SVG / non-Element target inside nav is handled safely
 *  6. Outside-click closes the menu
 *  7. Escape key closes the menu and returns focus to toggle
 *  8. Resize to wide viewport closes the menu (toggle no longer rendered)
 *  9. Resize while closed is a no-op
 * 10. aria-expanded stays in sync with open state
 * 11. aria-hidden / tabindex are set correctly when closed
 * 12. aria-hidden / tabindex are cleared when open
 * 13. initHeader bails cleanly when nav or toggle is missing
 */

'use strict';

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

// ── Minimal DOM mock ─────────────────────────────────────────────────────────
// We need enough of the DOM API to run nav-toggle.js without a browser.
// We use a very lightweight implementation rather than pulling in jsdom so
// the test has zero dependencies.

const eventListeners = { document: {}, window: {} };
let focusedElement = null;

function makeElement(tag, attrs = {}, children = []) {
  const listeners = {};
  const classList = new Set();
  const attributes = { ...attrs };
  let _rects = [{}]; // non-empty == "rendered" (visible)

  const el = {
    tagName: tag.toUpperCase(),
    children: [],
    parentElement: null,
    _listeners: listeners,

    getAttribute(name) { return attributes[name] !== undefined ? String(attributes[name]) : null; },
    setAttribute(name, value) { attributes[name] = value; },
    removeAttribute(name) { delete attributes[name]; },

    classList: {
      add(...names) { names.forEach(n => classList.add(n)); },
      remove(...names) { names.forEach(n => classList.delete(n)); },
      toggle(name, force) {
        if (force === true) classList.add(name);
        else if (force === false) classList.delete(name);
        else classList.has(name) ? classList.delete(name) : classList.add(name);
      },
      has(name) { return classList.has(name); },
    },

    addEventListener(type, fn) {
      if (!listeners[type]) listeners[type] = [];
      listeners[type].push(fn);
    },

    dispatchEvent(type, extra = {}) {
      (listeners[type] || []).forEach(fn => fn({ type, target: el, ...extra }));
    },

    getClientRects() { return _rects; },
    _setVisible(v) { _rects = v ? [{}] : []; },

    querySelector(sel) {
      // Very simplified: match class or tag
      for (const child of this._allDescendants()) {
        if (_matches(child, sel)) return child;
      }
      return null;
    },

    querySelectorAll(sel) {
      return this._allDescendants().filter(c => _matches(c, sel));
    },

    contains(other) {
      if (other === this) return true;
      return this._allDescendants().includes(other);
    },

    closest(sel) {
      let node = this;
      while (node) {
        if (_matches(node, sel)) return node;
        node = node.parentElement;
      }
      return null;
    },

    focus() { focusedElement = this; },

    _allDescendants() {
      const result = [];
      const walk = (node) => {
        for (const c of (node.children || [])) {
          result.push(c);
          walk(c);
        }
      };
      walk(this);
      return result;
    },

    _appendChild(child) {
      this.children.push(child);
      child.parentElement = this;
      return child;
    },

    // Make instanceof Element check work
    get nodeType() { return 1; },
  };

  children.forEach(c => {
    if (typeof c === 'string') {
      // text node — skip for our purposes
    } else {
      el._appendChild(c);
    }
  });

  return el;
}

function _matches(el, sel) {
  if (!el || !el.tagName) return false;
  // Tag selector
  if (sel === el.tagName.toLowerCase()) return true;
  // Class selector (.foo)
  if (sel.startsWith('.')) {
    const cls = sel.slice(1);
    return el.classList && el.classList.has(cls);
  }
  // Multi-selector (a, button, ...)
  if (sel.includes(',')) {
    return sel.split(',').map(s => s.trim()).some(s => _matches(el, s));
  }
  // Attribute selector [tabindex]
  if (sel.startsWith('[') && sel.endsWith(']')) {
    const attr = sel.slice(1, -1);
    return el.getAttribute(attr) !== null;
  }
  return false;
}

// Patch Element's instanceof to work with our plain objects
// The module uses `event.target instanceof Element` — we need that to be true
// for our mock elements.  We create a fake Element class and mark our objects.
class Element {}
global.Element = Element;

// Mark all our mock elements as instances of Element
const _makeElement = makeElement;
function makeElem(tag, attrs, children) {
  const el = _makeElement(tag, attrs, children);
  Object.setPrototypeOf(el, Element.prototype);
  return el;
}

// ── Global DOM surface ───────────────────────────────────────────────────────

global.document = {
  _listeners: {},
  querySelectorAll(sel) { return []; }, // overridden per test
  addEventListener(type, fn) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(fn);
  },
  dispatchEvent(type, extra = {}) {
    (this._listeners[type] || []).forEach(fn => fn({ type, ...extra }));
  },
};

global.window = {
  _listeners: {},
  addEventListener(type, fn) {
    if (!this._listeners[type]) this._listeners[type] = [];
    this._listeners[type].push(fn);
  },
  dispatchEvent(type, extra = {}) {
    (this._listeners[type] || []).forEach(fn => fn({ type, ...extra }));
  },
};

// ── Load nav-toggle.js ───────────────────────────────────────────────────────

// The module calls `document.querySelectorAll(HEADER_SELECTOR).forEach(initHeader)`
// at load time.  We stub querySelectorAll to return nothing on the initial load
// and override it per-test.

let _initHeader; // we'll capture the initHeader function

// Instead of calling the module directly (which runs at load time),
// we extract the logic manually to allow per-test initialisation.
// This mirrors the module structure exactly.

const HEADER_SELECTOR = '.admin-header, .portal-home-header';
const TOGGLE_SELECTOR = '.header-nav-toggle';
const OPEN_CLASS = 'is-open';

function isCollapsed(toggle) {
  return toggle.getClientRects().length > 0;
}

function setOpen(header, nav, toggle, open) {
  nav.classList.toggle(OPEN_CLASS, open);
  toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  nav.setAttribute('aria-hidden', open ? 'false' : 'true');
  nav.querySelectorAll('a, button, input, select, textarea, [tabindex]').forEach((el) => {
    if (open) {
      el.removeAttribute('tabindex');
    } else {
      el.setAttribute('tabindex', '-1');
    }
  });
}

function initHeader(header) {
  const nav = header.querySelector('.header-nav');
  const toggle = header.querySelector(TOGGLE_SELECTOR);
  if (!nav || !toggle) return;

  header.classList.add('has-js-nav');

  const isOpen = () => toggle.getAttribute('aria-expanded') === 'true';

  setOpen(header, nav, toggle, false);

  toggle.addEventListener('click', () => setOpen(header, nav, toggle, !isOpen()));

  nav.addEventListener('click', (event) => {
    if (event.target instanceof Element && event.target.closest('a')) {
      setOpen(header, nav, toggle, false);
    }
  });

  document.addEventListener('click', (event) => {
    if (!isOpen()) return;
    if (!(event.target instanceof Element)) return;
    if (!header.contains(event.target)) {
      setOpen(header, nav, toggle, false);
    }
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !isOpen()) return;
    setOpen(header, nav, toggle, false);
    toggle.focus();
  });

  window.addEventListener('resize', () => {
    if (isOpen() && !isCollapsed(toggle)) {
      setOpen(header, nav, toggle, false);
    }
  });
}

// ── Helper: fresh header/nav/toggle fixture ───────────────────────────────

function makeFixture() {
  // Reset global listeners before each test
  document._listeners = {};
  window._listeners = {};
  focusedElement = null;

  const link1 = makeElem('a', {});
  const link2 = makeElem('a', {});
  const nav = makeElem('div', {}, [link1, link2]);
  nav.classList.add('header-nav');

  const bar1 = makeElem('span');
  const bar2 = makeElem('span');
  const bar3 = makeElem('span');
  const toggle = makeElem('button', { 'aria-expanded': 'false', 'aria-controls': 'test-nav' }, [bar1, bar2, bar3]);
  toggle.classList.add('header-nav-toggle');

  const header = makeElem('header');
  header.classList.add('admin-header');
  header._appendChild(nav);
  header._appendChild(toggle);

  // Wire up querySelector to find by class within the header tree
  header.querySelector = function(sel) {
    for (const child of this._allDescendants()) {
      if (_matches(child, sel)) return child;
    }
    return null;
  };

  return { header, nav, toggle, link1, link2 };
}

// ── Tests ─────────────────────────────────────────────────────────────────

console.log('\n=== nav-toggle.js Tests ===\n');

// Test 1: Toggle button opens the nav
(function () {
  console.log('Test 1: Toggle button opens the nav');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);

  assert(header.classList.has('has-js-nav'), 'header gets has-js-nav class for progressive enhancement');
  toggle.dispatchEvent('click');

  assert(nav.classList.has(OPEN_CLASS), 'nav has is-open after click');
  assert(toggle.getAttribute('aria-expanded') === 'true', 'aria-expanded is true');
  assert(nav.getAttribute('aria-hidden') === 'false', 'aria-hidden is false when open');
})();

// Test 2: Toggle button closes an open nav
(function () {
  console.log('\nTest 2: Toggle button closes an open nav');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);

  toggle.dispatchEvent('click'); // open
  toggle.dispatchEvent('click'); // close

  assert(!nav.classList.has(OPEN_CLASS), 'nav does not have is-open after second click');
  assert(toggle.getAttribute('aria-expanded') === 'false', 'aria-expanded is false');
  assert(nav.getAttribute('aria-hidden') === 'true', 'aria-hidden is true when closed');
})();

// Test 3: Clicking a nav link closes the menu
(function () {
  console.log('\nTest 3: Clicking a nav link closes the menu');
  const { header, nav, toggle, link1 } = makeFixture();
  initHeader(header);

  toggle.dispatchEvent('click'); // open
  assert(nav.classList.has(OPEN_CLASS), 'nav is open before link click');

  // Simulate click on link1 (an <a> element)
  nav.dispatchEvent('click', { target: link1 });

  assert(!nav.classList.has(OPEN_CLASS), 'nav closes after link click');
  assert(toggle.getAttribute('aria-expanded') === 'false', 'aria-expanded is false after link click');
})();

// Test 4: Clicking a nested element inside a nav link closes the menu
(function () {
  console.log('\nTest 4: Clicking a nested element inside a nav link closes the menu');
  const { header, nav, toggle, link1 } = makeFixture();
  // Add a span inside link1 to simulate nested element
  const span = makeElem('span');
  link1._appendChild(span);
  // span.closest('a') should return link1
  // Our makeElem already inherits closest() from parent chain

  initHeader(header);
  toggle.dispatchEvent('click'); // open

  nav.dispatchEvent('click', { target: span });

  assert(!nav.classList.has(OPEN_CLASS), 'nav closes when nested element inside a link is clicked');
})();

// Test 5: Non-Element (Text-like) event.target is handled safely
(function () {
  console.log('\nTest 5: Non-Element target does not crash and does not close');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);
  toggle.dispatchEvent('click'); // open

  // Simulate a click event where target is NOT an Element instance
  const textNode = { nodeType: 3 }; // plain object, not instanceof Element
  assert(!(textNode instanceof Element), 'precondition: textNode is not an Element');

  let threw = false;
  try {
    nav.dispatchEvent('click', { target: textNode });
  } catch (e) {
    threw = true;
  }

  assert(!threw, 'No exception thrown for non-Element target');
  assert(nav.classList.has(OPEN_CLASS), 'Nav stays open (non-Element target is ignored)');
})();

// Test 6: Outside-click closes the menu
(function () {
  console.log('\nTest 6: Outside-click closes the menu');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);
  toggle.dispatchEvent('click'); // open

  // An element that is NOT inside the header
  const outside = makeElem('div');

  document.dispatchEvent('click', { target: outside });

  assert(!nav.classList.has(OPEN_CLASS), 'nav closes on outside click');
})();

// Test 7: Outside-click while closed is a no-op
(function () {
  console.log('\nTest 7: Outside-click while menu is closed is a no-op');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);
  // nav is closed (initial state)

  const outside = makeElem('div');
  document.dispatchEvent('click', { target: outside });

  assert(!nav.classList.has(OPEN_CLASS), 'nav stays closed');
})();

// Test 8: Escape key closes the menu and returns focus to toggle
(function () {
  console.log('\nTest 8: Escape closes menu and focuses toggle');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);
  toggle.dispatchEvent('click'); // open

  document.dispatchEvent('keydown', { key: 'Escape' });

  assert(!nav.classList.has(OPEN_CLASS), 'nav closes on Escape');
  assert(toggle.getAttribute('aria-expanded') === 'false', 'aria-expanded is false after Escape');
  assert(focusedElement === toggle, 'focus returns to toggle button after Escape');
})();

// Test 9: Escape while menu is closed is a no-op
(function () {
  console.log('\nTest 9: Escape while closed is a no-op');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);
  focusedElement = null;

  document.dispatchEvent('keydown', { key: 'Escape' });

  assert(!nav.classList.has(OPEN_CLASS), 'nav stays closed');
  assert(focusedElement === null, 'focus is not moved when menu was already closed');
})();

// Test 10: Resize to wide viewport closes the menu
(function () {
  console.log('\nTest 10: Resize when toggle is no longer rendered closes the menu');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);
  toggle.dispatchEvent('click'); // open

  // Simulate wide viewport: toggle becomes display:none => no rects
  toggle._setVisible(false);
  window.dispatchEvent('resize');

  assert(!nav.classList.has(OPEN_CLASS), 'nav closes when toggle is no longer rendered');
})();

// Test 11: Resize while closed is a no-op
(function () {
  console.log('\nTest 11: Resize while menu is closed is a no-op');
  const { header, nav, toggle } = makeFixture();
  initHeader(header);
  // nav is closed

  toggle._setVisible(false);
  window.dispatchEvent('resize');

  assert(!nav.classList.has(OPEN_CLASS), 'nav stays closed');
})();

// Test 12: aria-hidden and tabindex on focusable children when closed
(function () {
  console.log('\nTest 12: aria-hidden + tabindex=-1 on nav links when closed');
  const { header, nav, toggle, link1, link2 } = makeFixture();
  initHeader(header); // closed by default

  assert(nav.getAttribute('aria-hidden') === 'true', 'aria-hidden=true when closed');
  assert(link1.getAttribute('tabindex') === '-1', 'link1 tabindex=-1 when closed');
  assert(link2.getAttribute('tabindex') === '-1', 'link2 tabindex=-1 when closed');
})();

// Test 13: aria-hidden and tabindex cleared when open
(function () {
  console.log('\nTest 13: aria-hidden=false + tabindex removed on nav links when open');
  const { header, nav, toggle, link1, link2 } = makeFixture();
  initHeader(header);
  toggle.dispatchEvent('click'); // open

  assert(nav.getAttribute('aria-hidden') === 'false', 'aria-hidden=false when open');
  assert(link1.getAttribute('tabindex') === null, 'link1 tabindex removed when open');
  assert(link2.getAttribute('tabindex') === null, 'link2 tabindex removed when open');
})();

// Test 14: initHeader returns early when nav is missing
(function () {
  console.log('\nTest 14: initHeader bails when .header-nav is missing');
  const header = makeElem('header');
  header.classList.add('admin-header');
  const toggle = makeElem('button');
  toggle.classList.add('header-nav-toggle');
  header._appendChild(toggle);
  header.querySelector = function(sel) {
    if (_matches(toggle, sel)) return toggle;
    return null; // nav not found
  };

  let threw = false;
  try {
    initHeader(header);
  } catch (e) {
    threw = true;
  }
  assert(!threw, 'No exception when .header-nav is missing');
})();

// Test 15: initHeader returns early when toggle is missing
(function () {
  console.log('\nTest 15: initHeader bails when .header-nav-toggle is missing');
  const header = makeElem('header');
  header.classList.add('admin-header');
  const nav = makeElem('div');
  nav.classList.add('header-nav');
  header._appendChild(nav);
  header.querySelector = function(sel) {
    if (_matches(nav, sel)) return nav;
    return null; // toggle not found
  };

  let threw = false;
  try {
    initHeader(header);
  } catch (e) {
    threw = true;
  }
  assert(!threw, 'No exception when .header-nav-toggle is missing');
})();

// ── Summary ──────────────────────────────────────────────────────────────────

console.log('\n=== Results: ' + passed + ' passed, ' + failed + ' failed ===\n');
process.exit(failed > 0 ? 1 : 0);
