/**
 * Unit tests for the accessible password show/hide toggle.
 *
 * Run with: node tests/test_password_toggle.js
 */
const fs = require('fs');
const path = require('path');
const { setupPasswordToggle, setupAllPasswordToggles } = require('../portal/static/js/password-toggle.js');

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

// Test 6: setupAllPasswordToggles wires each toggle to its own field
const autoFirstInput = createMockElement('input', { type: 'password', value: 'first-secret' });
const autoSecondInput = createMockElement('input', { type: 'password', value: 'second-secret' });
const autoFirstBtn = createMockElement('button', {
  type: 'button',
  'aria-label': 'Show password',
  title: 'Show password',
  'aria-controls': 'password',
});
autoFirstBtn._showIcon = createMockElement('svg', {
  className: 'password-toggle-icon password-toggle-icon--show',
});
autoFirstBtn._hideIcon = createMockElement('svg', {
  className: 'password-toggle-icon password-toggle-icon--hide hidden',
});
const autoSecondBtn = createMockElement('button', {
  type: 'button',
  'aria-label': 'Show password',
  title: 'Show password',
  'aria-controls': 'password_confirm',
});
autoSecondBtn._showIcon = createMockElement('svg', {
  className: 'password-toggle-icon password-toggle-icon--show',
});
autoSecondBtn._hideIcon = createMockElement('svg', {
  className: 'password-toggle-icon password-toggle-icon--hide hidden',
});
global.document = {
  querySelectorAll: (sel) => (sel === '.password-toggle-btn' ? [autoFirstBtn, autoSecondBtn] : []),
  getElementById: (id) => ({ password: autoFirstInput, password_confirm: autoSecondInput })[id] || null,
};
setupAllPasswordToggles();
delete global.document;

autoFirstBtn.click();
assert(autoFirstInput.type === 'text', 'Auto-wired toggle reveals its own field');
assert(autoSecondInput.type === 'password', 'Toggling one field leaves the other field hidden');
assert(autoFirstBtn.getAttribute('aria-label') === 'Hide password', 'Auto-wired toggle updates its own label');
assert(autoSecondBtn.getAttribute('aria-label') === 'Show password', 'Untouched toggle keeps its label');

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
