import assert from 'node:assert/strict';

import { TRANSLATION_STATES, translationSummary } from '../portal/static/js/translation-health.js';

const base = { window_seconds: 300, translated: 0, failed: 0, stale: 0, complete: true };

assert.equal(translationSummary({ ...base, state: 'idle' }), 'Nothing translated in the last 5 minutes.');

assert.equal(translationSummary({ ...base, state: 'healthy', translated: 12 }), '12 translated in the last 5 minutes.');

assert.equal(
  translationSummary({ ...base, state: 'lagging', translated: 9, stale: 2, failed: 1 }),
  '9 translated, 2 dropped because the engine was busy, 1 failed in the last 5 minutes.',
);

assert.equal(
  translationSummary({ ...base, state: 'idle', complete: false }),
  'Nothing translated in the last 5 minutes. The server is very busy, so only its latest segments are counted.',
);

assert.match(
  translationSummary({ ...base, state: 'failing', failed: 4, complete: false }),
  /4 failed in the last 5 minutes\. The server is very busy, so only its latest segments are counted\.$/,
);

assert.equal(translationSummary({ state: 'healthy', translated: 3, failed: 0, stale: 0 }), '3 translated in the last 5 minutes.');

assert.deepEqual(Object.keys(TRANSLATION_STATES).sort(), ['failing', 'healthy', 'idle', 'lagging']);

console.log('translation health summary tests passed');
