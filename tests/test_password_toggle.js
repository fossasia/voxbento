/**
 * Unit tests for the accessible password show/hide toggle.
 *
 * Run with: node tests/test_password_toggle.js
 */
const fs = require('fs');
const path = require('path');
const passwordToggleModulePath = require.resolve('../portal/static/js/password-toggle.js');

global.document = {
  readyState: 'complete',
  querySelectorAll: () => [],
  getElementById: () => null,
};
delete require.cache[passwordToggleModulePath];
const { setupPasswordToggle } = require(passwordToggleModulePath);
delete global.document;

let passed = 0;
let failed = 0;

function assert(condition, message) {
  if (condition) {
    passed++;
    console.log('  PASS: ' + message);
  } else {
    failed++;
    console.error('  FAIL: ' + message);
  }
}

function createMockElement(tagName, attrs = {}) {
  const listeners = {};
  const classes = new Set(attrs.className ? attrs.className.split(' ').filter(Boolean) : []);
  const attributes = { ...attrs };
  delete attributes.className;

  return {
    tagName: tagName.toUpperCase(),
    type: attrs.type || '',
    value: attrs.value !== undefined ? attrs.value : '',
    attributes,
    classList: {
      contains: (c) => classes.has(c),
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, force) => {
        if (force === undefined) {
          classes.has(c) ? classes.delete(c) : classes.add(c);
        } else if (force) {
          classes.add(c);
        } else {
          classes.delete(c);
        }
      },
    },
    getAttribute: (name) => attributes[name],
    setAttribute: (name, val) => {
      attributes[name] = String(val);
    },
    addEventListener: (event, handler) => {
      listeners[event] = listeners[event] || [];
      listeners[event].push(handler);
    },
    click: function () {
      let defaultPrevented = false;
      const event = {
        type: 'click',
        target: this,
        preventDefault: () => {
          defaultPrevented = true;
        },
        defaultPrevented: () => defaultPrevented,
      };
      (listeners['click'] || []).forEach((h) => h(event));
      return defaultPrevented;
    },
    querySelector: function (sel) {
      if (sel === '.password-toggle-icon--show') return this._showIcon || null;
      if (sel === '.password-toggle-icon--hide') return this._hideIcon || null;
      return null;
    },
  };
}

console.log('Running Password Toggle tests...');

// Test 1: Check rendered template button attributes
const templateHtml = fs.readFileSync(path.join(__dirname, '../portal/templates/login.html'), 'utf-8');
const toggleBtnTagMatch = templateHtml.match(/<button\b[^>]*id=["']toggle-password["'][^>]*>/i);
assert(toggleBtnTagMatch !== null, 'Toggle button is defined in login.html');
assert(/\btype=["']button["']/i.test(toggleBtnTagMatch[0]), 'Rendered toggle button in login.html has type="button" to prevent form submission');
assert(/\baria-label=["']Show password["']/i.test(toggleBtnTagMatch[0]), 'Rendered toggle button has aria-label="Show password" by default');

// Test 2: Hidden by default in DOM state
const showIcon = createMockElement('svg', {
  className: 'password-toggle-icon password-toggle-icon--show',
});
const hideIcon = createMockElement('svg', {
  className: 'password-toggle-icon password-toggle-icon--hide hidden',
});
const passwordInput = createMockElement('input', {
  type: 'password',
  value: 'myP@ssw0rd!#',
});
const toggleBtn = createMockElement('button', {
  type: 'button',
  'aria-label': 'Show password',
  title: 'Show password',
});
toggleBtn._showIcon = showIcon;
toggleBtn._hideIcon = hideIcon;

setupPasswordToggle(passwordInput, toggleBtn);

assert(passwordInput.type === 'password', 'Password is hidden by default (type="password")');
assert(toggleBtn.getAttribute('aria-label') === 'Show password', 'Accessible name is "Show password" by default');
assert(!showIcon.classList.contains('hidden'), 'Show eye icon is visible by default');
assert(hideIcon.classList.contains('hidden'), 'Hide eye icon is hidden by default');

// Test 3: First click reveals password
const prevented1 = toggleBtn.click();
assert(prevented1, 'Click event prevents default action');
assert(passwordInput.type === 'text', 'Clicking reveals password (type="text")');
assert(passwordInput.value === 'myP@ssw0rd!#', 'Password value is preserved after revealing');
assert(toggleBtn.getAttribute('aria-label') === 'Hide password', 'Accessible name updates to "Hide password"');
assert(toggleBtn.getAttribute('title') === 'Hide password', 'Title updates to "Hide password"');
assert(showIcon.classList.contains('hidden'), 'Show eye icon is hidden when password is visible');
assert(!hideIcon.classList.contains('hidden'), 'Hide eye icon is visible when password is visible');

// Test 4: Second click hides password again
const prevented2 = toggleBtn.click();
assert(prevented2, 'Second click event prevents default action');
assert(passwordInput.type === 'password', 'Clicking again hides password (type="password")');
assert(passwordInput.value === 'myP@ssw0rd!#', 'Password value is preserved after hiding');
assert(toggleBtn.getAttribute('aria-label') === 'Show password', 'Accessible name reverts to "Show password"');
assert(toggleBtn.getAttribute('title') === 'Show password', 'Title reverts to "Show password"');
assert(!showIcon.classList.contains('hidden'), 'Show eye icon is visible again');
assert(hideIcon.classList.contains('hidden'), 'Hide eye icon is hidden again');

// Test 5: Gracefully handles missing elements
let threw = false;
try {
  setupPasswordToggle(null, null);
  setupPasswordToggle(passwordInput, null);
  setupPasswordToggle(null, toggleBtn);
} catch {
  threw = true;
}
assert(!threw, 'setupPasswordToggle gracefully handles null/missing elements without throwing');

// Test 6: module startup wiring handles loading and complete document states
const createAutoToggleElements = () => {
  const firstInput = createMockElement('input', { type: 'password', value: 'first-secret' });
  const secondInput = createMockElement('input', { type: 'password', value: 'second-secret' });
  const firstBtn = createMockElement('button', {
    type: 'button',
    'aria-label': 'Show password',
    title: 'Show password',
    'aria-controls': 'password',
  });
  firstBtn._showIcon = createMockElement('svg', {
    className: 'password-toggle-icon password-toggle-icon--show',
  });
  firstBtn._hideIcon = createMockElement('svg', {
    className: 'password-toggle-icon password-toggle-icon--hide hidden',
  });
  const secondBtn = createMockElement('button', {
    type: 'button',
    'aria-label': 'Show password',
    title: 'Show password',
    'aria-controls': 'password_confirm',
  });
  secondBtn._showIcon = createMockElement('svg', {
    className: 'password-toggle-icon password-toggle-icon--show',
  });
  secondBtn._hideIcon = createMockElement('svg', {
    className: 'password-toggle-icon password-toggle-icon--hide hidden',
  });
  return { firstInput, secondInput, firstBtn, secondBtn };
};

const loadingElements = createAutoToggleElements();
const loadingListeners = {};
global.document = {
  readyState: 'loading',
  querySelectorAll: (sel) =>
    sel === '.password-toggle-btn'
      ? [loadingElements.firstBtn, loadingElements.secondBtn]
      : [],
  getElementById: (id) =>
    ({ password: loadingElements.firstInput, password_confirm: loadingElements.secondInput })[id] || null,
  addEventListener: (event, handler) => {
    loadingListeners[event] = handler;
  },
};
delete require.cache[passwordToggleModulePath];
require(passwordToggleModulePath);
assert(typeof loadingListeners.DOMContentLoaded === 'function', 'Loading document waits for DOMContentLoaded');
loadingListeners.DOMContentLoaded();
loadingElements.firstBtn.click();
assert(loadingElements.firstInput.type === 'text', 'DOMContentLoaded startup reveals the first field');
assert(loadingElements.secondInput.type === 'password', 'DOMContentLoaded startup leaves the second field hidden');
assert(loadingElements.firstBtn.getAttribute('aria-label') === 'Hide password', 'DOMContentLoaded startup updates the first label');
assert(loadingElements.secondBtn.getAttribute('aria-label') === 'Show password', 'DOMContentLoaded startup preserves the second label');
delete global.document;

const completeElements = createAutoToggleElements();
global.document = {
  readyState: 'complete',
  querySelectorAll: (sel) =>
    sel === '.password-toggle-btn'
      ? [completeElements.firstBtn, completeElements.secondBtn]
      : [],
  getElementById: (id) =>
    ({ password: completeElements.firstInput, password_confirm: completeElements.secondInput })[id] || null,
};
delete require.cache[passwordToggleModulePath];
require(passwordToggleModulePath);

completeElements.secondBtn.click();
assert(completeElements.secondInput.type === 'text', 'Complete document startup reveals only the second field');
assert(completeElements.firstInput.type === 'password', 'Toggling the second field leaves the first field hidden');
assert(completeElements.secondBtn.getAttribute('aria-label') === 'Hide password', 'Complete document startup updates only the second label');
assert(completeElements.firstBtn.getAttribute('aria-label') === 'Show password', 'Untouched first toggle keeps its label');
delete global.document;

// Test 7: every password page reuses the login toggle markup and loads the shared module
// accept_invite.html renders only one branch (elif), so #toggle-password appears in both
// branches but never twice in one response; other pages must use each toggle id once.
const togglePages = [
  {
    file: 'register.html',
    pairs: [
      ['toggle-password', 'password'],
      ['toggle-password-confirm', 'password_confirm'],
    ],
    uniqueIds: true,
  },
  { file: 'reset_password.html', pairs: [['toggle-password', 'password']], uniqueIds: true },
  {
    file: 'accept_invite.html',
    pairs: [
      ['toggle-password', 'password'],
      ['toggle-password-confirm', 'password_confirm'],
    ],
    uniqueIds: false,
  },
  { file: 'login.html', pairs: [['toggle-password', 'password']], uniqueIds: true },
];
for (const page of togglePages) {
  const pageHtml = fs.readFileSync(path.join(__dirname, '../portal/templates', page.file), 'utf-8');
  assert(
    pageHtml.includes("path='js/password-toggle.js'") && pageHtml.includes('type="module"'),
    `${page.file} loads the shared password-toggle.js module`
  );
  for (const [btnId, inputId] of page.pairs) {
    const btnTags = pageHtml.match(new RegExp(`<button\\b[^>]*id=["']${btnId}["'][^>]*>`, 'gi')) || [];
    assert(btnTags.length > 0, `${page.file} has a #${btnId} toggle button`);
    if (page.uniqueIds) {
      assert(btnTags.length === 1, `${page.file} uses #${btnId} exactly once`);
    }
    for (const tag of btnTags) {
      assert(/\btype=["']button["']/i.test(tag), `${page.file} #${btnId} has type="button"`);
      assert(/\baria-label=["']Show password["']/i.test(tag), `${page.file} #${btnId} starts labelled "Show password"`);
      assert(
        new RegExp(`aria-controls=["']${inputId}["']`, 'i').test(tag),
        `${page.file} #${btnId} controls only #${inputId}`
      );
    }
    const inputTags = pageHtml.match(new RegExp(`<input\\b[^>]*id=["']${inputId}["'][^>]*>`, 'gi')) || [];
    assert(inputTags.length > 0, `${page.file} has a #${inputId} password field`);
    for (const tag of inputTags) {
      assert(/\btype=["']password["']/i.test(tag), `${page.file} #${inputId} starts hidden`);
    }
  }
}

console.log(`\nPassword Toggle tests completed: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
