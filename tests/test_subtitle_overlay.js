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

  class FakeWebSocket {
    constructor(url) {
      createdWsUrl = url;
      this.url = url;
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
    replaceChildren() {},
  };

  const documentStub = {
    getElementById(id) {
      if (id === 'overlay') return rootElement;
      if (id === 'overlay-lines') return linesBox;
      return null;
    },
    createElement() {
      return {
        classList: { add() {} },
        className: '',
        textContent: '',
      };
    },
  };

  const windowStub = {
    location: {
      protocol: 'http:',
      host: 'localhost:8000',
      search: search,
    },
    addEventListener() {},
  };

  const context = vm.createContext({
    document: documentStub,
    window: windowStub,
    WebSocket: FakeWebSocket,
    URLSearchParams: URLSearchParams,
    parseInt: parseInt,
    Number: Number,
    Math: Math,
    setTimeout: () => 1,
    clearTimeout: () => {},
    console: console,
  });

  // Strip export keyword for vm execution
  const runnableCode = scriptCode.replace(/^export\s+/gm, '');
  vm.runInContext(runnableCode, context);

  return { createdWsUrl };
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

// Test 2: token from query string fallback
{
  const { createdWsUrl } = runOverlay(
    { boothId: 'pycon-1-en' },
    '?token=query.token.here'
  );
  assert(
    createdWsUrl === 'ws://localhost:8000/ws/captions/pycon-1-en?token=query.token.here',
    `overlay attaches query param token when dataset token is empty: got ${createdWsUrl}`
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

console.log(`\n${passed} passed, ${failed} failed\n`);
if (failed > 0) process.exit(1);

