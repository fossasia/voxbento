/**
 * Unit tests for the home-page live demo helpers (portal/static/js/live-demo-core.js).
 *
 * Run with: node tests/test_live_demo_core.mjs
 */
import assert from 'node:assert/strict';

import {
  FRAME_SAMPLES,
  addGlossaryEntry,
  findTermRanges,
  glossaryTerms,
  SCRIPTS,
  createDownsampler,
  doneMessage,
  formatClock,
  frameLevel,
  pickListen,
  routeSummary,
  toggleTarget,
} from '../portal/static/js/live-demo-core.js';

function run(name, fn) {
  fn();
  console.log(`ok - ${name}`);
}

run('downsampler turns 48 kHz into 100 ms frames of 16 kHz', () => {
  const frames = [];
  const ds = createDownsampler(48000, (frame) => frames.push(frame));
  ds.push(new Float32Array(48000).fill(0.5));
  assert.equal(frames.length, 10);
  assert.equal(frames[0].length, FRAME_SAMPLES);
  assert.ok(Math.abs(frames[0][0] - 0.5 * 0x7fff) <= 1);
});

run('downsampler handles a rate that is not a whole multiple', () => {
  const frames = [];
  const ds = createDownsampler(44100, (frame) => frames.push(frame));
  for (let i = 0; i < 442; i++) ds.push(new Float32Array(100).fill(-1));
  assert.equal(frames.length, 10);
  assert.equal(frames[9][0], -0x8000);
});

run('downsampler clamps out-of-range samples', () => {
  const frames = [];
  const ds = createDownsampler(16000, (frame) => frames.push(frame));
  ds.push(new Float32Array(FRAME_SAMPLES).fill(3));
  assert.equal(frames[0][0], 0x7fff);
});

run('frame level is 0 for silence and capped at 1', () => {
  assert.equal(frameLevel(new Int16Array(10)), 0);
  assert.equal(frameLevel(new Int16Array(10).fill(30000)), 1);
});

run('targets toggle in order, skip the spoken language and respect the limit', () => {
  assert.deepEqual(toggleTarget(['es'], 'fr', 'en', 3), ['es', 'fr']);
  assert.deepEqual(toggleTarget(['es', 'fr'], 'es', 'en', 3), ['fr']);
  assert.deepEqual(toggleTarget(['es'], 'en', 'en', 3), ['es']);
  assert.deepEqual(toggleTarget(['es', 'fr', 'de'], 'ja', 'en', 3), ['es', 'fr', 'de']);
});

run('listen keeps a voiced choice, else falls back to the first voiced target', () => {
  const voiced = new Set(['es', 'ja']);
  assert.equal(pickListen('ja', ['es', 'ja'], voiced), 'ja');
  assert.equal(pickListen('fr', ['th', 'es'], voiced), 'es');
  assert.equal(pickListen(null, ['th'], voiced), null);
});

run('route summary names the languages', () => {
  const names = new Map([['en', 'English'], ['ko', 'Korean'], ['ja', 'Japanese']]);
  assert.deepEqual(routeSummary('en', ['ko', 'ja'], names), { from: 'English', to: 'Korean · Japanese' });
  assert.equal(routeSummary('en', [], names).to, 'choose languages');
});

run('every script has lines to read', () => {
  for (const script of Object.values(SCRIPTS)) assert.ok(script.lines.length >= 2);
});

run('clock and end messages', () => {
  assert.equal(formatClock(65.2), '1:06');
  assert.equal(formatClock(-3), '0:00');
  assert.match(doneMessage('time_limit', 120), /120-second/);
  assert.equal(doneMessage('stopped', 120), '');
});

run('term ranges are whole words, case-insensitive, longest first', () => {
  const text = 'Welcome to VoxBento at the keynote stage, not voxbentos.';
  assert.deepEqual(findTermRanges(text, ['voxbento', 'keynote', 'keynote stage']), [[11, 19], [27, 40]]);
});

run('term ranges work in scripts without spaces', () => {
  assert.deepEqual(findTermRanges('欢迎来到VoxBento展位', ['VoxBento']), [[4, 12]]);
  assert.deepEqual(findTermRanges('東京で会いましょう', ['東京']), [[0, 2]]);
});

run('glossary terms per row', () => {
  const glossary = [
    { term: 'VoxBento', lang: 'all', translation: 'VoxBento' },
    { term: 'keynote', lang: 'fr', translation: 'séance plénière' },
  ];
  assert.deepEqual(glossaryTerms(glossary, null), ['VoxBento', 'keynote']);
  assert.deepEqual(glossaryTerms(glossary, 'fr'), ['VoxBento', 'séance plénière']);
  assert.deepEqual(glossaryTerms(glossary, 'es'), ['VoxBento']);
});

run('adding glossary entries replaces the same term and language and respects the limit', () => {
  let { entries } = addGlossaryEntry([], { term: ' keynote ', lang: 'fr', translation: 'plénière' }, 2);
  ({ entries } = addGlossaryEntry(entries, { term: 'Keynote', lang: 'fr', translation: 'séance plénière' }, 2));
  assert.deepEqual(entries, [{ term: 'Keynote', lang: 'fr', translation: 'séance plénière' }]);
  ({ entries } = addGlossaryEntry(entries, { term: 'VoxBento', lang: 'all', translation: '' }, 2));
  const full = addGlossaryEntry(entries, { term: 'Eventyay', lang: 'all' }, 2);
  assert.equal(full.entries.length, 2);
  assert.match(full.error, /up to 2/);
  assert.match(addGlossaryEntry([], { term: '  ' }, 2).error, /Type a term/);
});
