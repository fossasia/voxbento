/**
 * Unit tests for the OBS / vMix subtitle overlay client (portal/static/js/subtitle-overlay.js).
 *
 * Run with: node tests/test_subtitle_overlay.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

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

const scriptCode = fs.readFileSync(
  path.join(__dirname, '..', 'portal', 'static', 'js', 'subtitle-overlay.js'),
  'utf8'
);

function runOverlay(dataset = {}, search = '') {
  let createdWsUrl = null;
  let activeWs = null;

  class FakeWebSocket {
    constructor(url) {
      createdWsUrl = url;
      this.url = url;
      activeWs = this;
    }
  }

  const rootElement = {
    dataset: dataset,
    classList: {
      toggle() {},
    },
    style: {
      setProperty() {},
    },
  };

  const linesBox = {
    children: [],
    replaceChildren(...children) {
      this.children = children;
    },
  };

  const documentStub = {
    getElementById(id) {
      if (id === 'overlay') return rootElement;
      if (id === 'overlay-lines') return linesBox;
      return null;
    },
    createElement() {
      const el = {
        classList: {
          classes: new Set(),
          add(c) { this.classes.add(c); },
          contains(c) { return this.classes.has(c); },
        },
        className: '',
        textContent: '',
        style: { setProperty() {} },
      };
      return el;
    },
  };

  let messageListener = null;
  const windowStub = {
    location: {
      protocol: 'http:',
      host: 'localhost:8000',
      search: search,
      origin: 'http://localhost:8000',
      href: 'http://localhost:8000/overlay/pycon/1/en?code=secret',
    },
    addEventListener(evt, fn) {
      if (evt === 'message') messageListener = fn;
    },
  };

  windowStub.parent = windowStub;
  let timerFn = null;
  let timerDelay = 0;
  const setTimeoutStub = (fn, ms) => {
    timerFn = fn;
    timerDelay = ms;
    return 1;
  };
  const clearTimeoutStub = () => {
    timerFn = null;
  };

  const context = vm.createContext({
    fetch: () => {},
    AbortController,
    URL,
    document: documentStub,
    window: windowStub,
    WebSocket: FakeWebSocket,
    URLSearchParams: URLSearchParams,
    parseInt: parseInt,
    Number: Number,
    Math: Math,
    setTimeout: setTimeoutStub,
    clearTimeout: clearTimeoutStub,
    console: console,
  });

  // Strip export keyword for vm execution
  const runnableCode = scriptCode.replace(/^export\s+/gm, '');
  vm.runInContext(runnableCode, context);

  return {
    createdWsUrl,
    activeWs,
    linesBox,
    handleCaption: context.handleCaption,
    triggerTimeout: () => { if (timerFn) timerFn(); },
    getTimerDelay: () => timerDelay,
  };
}

console.log('\n── Subtitle overlay WebSocket token auth ──');

// Test 1: root.dataset.token attaches ?token= to WebSocket URL
{
  const { createdWsUrl } = runOverlay(
    { boothId: 'pycon-1-en', token: 'jwt.token.here' },
    ''
  );
  assert(
    createdWsUrl === 'ws://localhost:8000/ws/captions/pycon-1-en?token=jwt.token.here',
    `overlay attaches dataset token to caption socket URL: got ${createdWsUrl}`
  );
}

// Test 2: a token in the page URL is ignored; only the server-rendered token is used
{
  const { createdWsUrl } = runOverlay(
    { boothId: 'pycon-1-en' },
    '?token=query.token.here'
  );
  assert(
    createdWsUrl === 'ws://localhost:8000/ws/captions/pycon-1-en',
    `overlay ignores a page URL token: got ${createdWsUrl}`
  );
}

// Test 3: no token attaches no query string
{
  const { createdWsUrl } = runOverlay(
    { boothId: 'pycon-1-en' },
    ''
  );
  assert(
    createdWsUrl === 'ws://localhost:8000/ws/captions/pycon-1-en',
    `overlay does not attach token query when none provided: got ${createdWsUrl}`
  );
}

console.log('\n── Real-time subtitles updates (Issue #1072) ──');

// Test 4: handles partial captions and updates display in real time
{
  const { activeWs, linesBox } = runOverlay({ boothId: 'pycon-1-en' }, '');
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'partial', text: 'Hello everyone' }),
  });
  assert(linesBox.children.length === 1, 'partial caption creates 1 visible line');
  assert(linesBox.children[0].textContent === 'Hello everyone', 'partial caption text matches');
  assert(!linesBox.children[0].classList.contains('overlay-line--older'), 'partial caption is newest');
}

// Test 5: final caption replaces partial and retains committed lines
{
  const { activeWs, linesBox } = runOverlay({ boothId: 'pycon-1-en' }, '');
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'partial', text: 'Hello' }),
  });
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'final', text: 'Hello everyone.' }),
  });
  assert(linesBox.children.length === 1, 'finalized caption replaces partial with 1 line');
  assert(linesBox.children[0].textContent === 'Hello everyone.', 'final caption text committed');
}

// Test 6: handles translated_caption messages for translated booths
{
  const { activeWs, linesBox } = runOverlay({ boothId: 'pycon-1-es' }, '');
  activeWs.onmessage({
    data: JSON.stringify({
      type: 'translated_caption',
      status: 'final',
      text: 'Bienvenidos a todos.',
    }),
  });
  assert(linesBox.children.length === 1, 'translated_caption updates display');
  assert(linesBox.children[0].textContent === 'Bienvenidos a todos.', 'translated_caption text matches');
}

// Test 7: multiple incoming captions update visible lines and style older lines
{
  const { activeWs, linesBox } = runOverlay({ boothId: 'pycon-1-es' }, '');
  activeWs.onmessage({
    data: JSON.stringify({
      type: 'translated_caption',
      status: 'final',
      text: 'Primera frase.',
    }),
  });
  activeWs.onmessage({
    data: JSON.stringify({
      type: 'translated_caption',
      status: 'final',
      text: 'Segunda frase.',
    }),
  });

  assert(linesBox.children.length === 2, '2 lines displayed for consecutive captions');
  assert(linesBox.children[0].textContent === 'Primera frase.', 'first line is older');
  assert(linesBox.children[0].classList.contains('overlay-line--older'), 'older line has older class');
  assert(linesBox.children[1].textContent === 'Segunda frase.', 'second line is newest');
  assert(!linesBox.children[1].classList.contains('overlay-line--older'), 'newest line has no older class');
}

// Test 8: line limit (default 2) caps displayed lines to the newest ones
{
  const { activeWs, linesBox } = runOverlay({ boothId: 'pycon-1-en' }, '?lines=2');
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'final', text: 'Line 1' }),
  });
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'final', text: 'Line 2' }),
  });
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'final', text: 'Line 3' }),
  });

  assert(linesBox.children.length === 2, 'line limit restricts visible lines to 2');
  assert(linesBox.children[0].textContent === 'Line 2', 'oldest within limit is Line 2');
  assert(linesBox.children[1].textContent === 'Line 3', 'newest line is Line 3');
}

// Test 9: status clear clears active partial caption
{
  const { activeWs, linesBox } = runOverlay({ boothId: 'pycon-1-en' }, '');
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'partial', text: 'Incomplete thoughts' }),
  });
  assert(linesBox.children.length === 1, 'partial line present before clear');
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'clear', text: '' }),
  });
  assert(linesBox.children.length === 0, 'partial line cleared after clear status');
}

// Test 10: silence timeout (8s) clears stale captions
{
  const { activeWs, linesBox, triggerTimeout, getTimerDelay } = runOverlay({ boothId: 'pycon-1-en' }, '');
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'final', text: 'Will disappear soon' }),
  });
  assert(linesBox.children.length === 1, 'caption visible initially');
  assert(getTimerDelay() === 8000, 'idle clear timer set to 8000ms');

  triggerTimeout();
  assert(linesBox.children.length === 0, 'captions cleared after idle timeout');
}

// Test 11: the same segment as caption + translated_caption shows once
{
  const { activeWs, linesBox } = runOverlay({ boothId: 'pycon-1-en' }, '?lines=4');
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'final', text: 'First line', segment_id: 'a' }),
  });
  activeWs.onmessage({
    data: JSON.stringify({ type: 'caption', status: 'final', text: 'Second line', segment_id: 'b' }),
  });
  activeWs.onmessage({
    data: JSON.stringify({ type: 'translated_caption', text: 'Second line', segment_id: 'b' }),
  });
  assert(linesBox.children.length === 2, 'duplicate segment_id does not add a line');
  assert(linesBox.children[0].textContent === 'First line', 'earlier caption is not evicted by the duplicate');

  // Test captions carry no segment_id and are always appended.
  activeWs.onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', text: 'Test' }) });
  activeWs.onmessage({ data: JSON.stringify({ type: 'caption', status: 'final', text: 'Test' }) });
  assert(linesBox.children.length === 4, 'captions without segment_id are not deduplicated');
}

console.log(`\n${passed} passed, ${failed} failed\n`);
if (failed > 0) process.exit(1);
