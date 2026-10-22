/**
 * Unit tests for the listener page's room/language deep-link helpers.
 *
 * Run with: node tests/test_listener_selection.js
 *
 * These cover the decisions behind `?room=`/`?lang=` links: which room a link
 * opens, and how the address bar is rewritten to match the current selection.
 */
'use strict';

const path = require('path');
const { pathToFileURL } = require('url');

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

function assertEqual(actual, expected, message) {
  assert(
    actual === expected,
    message +
      ' (expected ' + JSON.stringify(expected) + ', got ' + JSON.stringify(actual) + ')'
  );
}

const TWO_ROOMS = [{ id: 1 }, { id: 2 }];
const ONE_ROOM = [{ id: 7 }];

(async () => {
  const modulePath = path.join(
    __dirname,
    '..',
    'portal',
    'static',
    'js',
    'listener-selection.js'
  );
  const { resolveRoomId, buildSelectionUrl } = await import(
    pathToFileURL(modulePath).href
  );

  console.log('resolveRoomId');
  assertEqual(resolveRoomId(TWO_ROOMS, '2'), '2', 'picks the room named in the URL');
  assertEqual(resolveRoomId(TWO_ROOMS, '9999'), null, 'ignores an unknown room id');
  assertEqual(resolveRoomId(TWO_ROOMS, null), null, 'leaves several rooms unselected');
  assertEqual(resolveRoomId(ONE_ROOM, null), '7', 'auto-selects the only room');
  assertEqual(resolveRoomId(ONE_ROOM, '9999'), '7', 'falls back to the only room');
  assertEqual(resolveRoomId([], '1'), null, 'handles an event with no rooms');
  assertEqual(resolveRoomId(undefined, '1'), null, 'handles missing room data');
  assertEqual(resolveRoomId(TWO_ROOMS, 2), '2', 'accepts a non-string room id');

  console.log('buildSelectionUrl');
  assertEqual(
    buildSelectionUrl('https://x.test/listener/ev', 1, 'hi'),
    'https://x.test/listener/ev?room=1&lang=hi',
    'writes room and lang'
  );
  assertEqual(
    buildSelectionUrl('https://x.test/listener/ev?code=ROOM42', 1, 'hi'),
    'https://x.test/listener/ev?code=ROOM42&room=1&lang=hi',
    'preserves the join code'
  );
  assertEqual(
    buildSelectionUrl('https://x.test/listener/ev?room=9&lang=fr', 1, 'hi'),
    'https://x.test/listener/ev?room=1&lang=hi',
    'overwrites a stale selection'
  );
  assertEqual(
    buildSelectionUrl('https://x.test/listener/ev?room=1&lang=hi', 1, null),
    'https://x.test/listener/ev?room=1',
    'drops lang when no language is selected'
  );
  assertEqual(
    buildSelectionUrl('https://x.test/listener/ev?room=1&lang=hi', null, 'hi'),
    'https://x.test/listener/ev',
    'drops both when no room is selected'
  );
  assertEqual(
    buildSelectionUrl('https://x.test/listener/ev?code=ROOM42&room=1&lang=hi', null, null),
    'https://x.test/listener/ev?code=ROOM42',
    'keeps the join code while clearing the selection'
  );
  assertEqual(
    buildSelectionUrl('https://x.test/listener/ev', 1, 'floor'),
    'https://x.test/listener/ev?room=1&lang=floor',
    'supports the synthetic floor language'
  );

  console.log('\n=== Results: ' + passed + ' passed, ' + failed + ' failed ===\n');
  process.exit(failed > 0 ? 1 : 0);
})();
