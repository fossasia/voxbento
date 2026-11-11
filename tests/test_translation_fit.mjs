/**
 * Unit tests for the translation service picker's language filter
 * (portal/static/js/translation-fit.js), used by the room setup form.
 *
 * Run with: node tests/test_translation_fit.mjs
 */
import assert from 'node:assert/strict';

import { syncTranslationOptions, translatesAll } from '../portal/static/js/translation-fit.js';

const SUPPORT = { local: ['en', 'fr', 'de'] };

// A just-enough <select>: options that can be hidden and disabled, and one chosen value.
function makeSelect(values, chosen) {
  const select = {
    options: values.map((value) => ({ value, hidden: false, disabled: false })),
    value: chosen,
    get selectedOptions() {
      return this.options.filter((option) => option.value === this.value);
    },
  };
  return select;
}

const offered = (select) => select.options.filter((option) => !option.hidden && !option.disabled).map((o) => o.value);

// A service with a fixed list must cover every language; one without a list translates anything.
assert.equal(translatesAll(SUPPORT, 'local', ['en', 'fr']), true);
assert.equal(translatesAll(SUPPORT, 'local', ['en', 'az']), false);
assert.equal(translatesAll(SUPPORT, 'local', []), true);
assert.equal(translatesAll(SUPPORT, 'openai', ['en', 'az']), true);

// Every service fits: nothing is hidden and the choice stays.
let select = makeSelect(['openai', 'local'], 'local');
syncTranslationOptions(select, SUPPORT, ['en', 'fr']);
assert.deepEqual(offered(select), ['openai', 'local']);
assert.equal(select.value, 'local');

// The chosen service can't translate a language: it is hidden and the first one that fits is chosen.
syncTranslationOptions(select, SUPPORT, ['en', 'az']);
assert.deepEqual(offered(select), ['openai']);
assert.equal(select.value, 'openai');

// A hidden service comes back when the language that ruled it out is removed, without changing the choice.
syncTranslationOptions(select, SUPPORT, ['en', 'de']);
assert.deepEqual(offered(select), ['openai', 'local']);
assert.equal(select.value, 'openai');

// The language spoken counts too.
select = makeSelect(['local', 'openai'], 'local');
syncTranslationOptions(select, SUPPORT, ['az', 'fr']);
assert.deepEqual(offered(select), ['openai']);
assert.equal(select.value, 'openai');

// Nothing fits (the built-in translator is the only service): the choice is left for the server to explain.
select = makeSelect(['local'], 'local');
syncTranslationOptions(select, SUPPORT, ['en', 'az']);
assert.deepEqual(offered(select), ['local']);
assert.equal(select.value, 'local');

console.log('translation-fit tests passed');
