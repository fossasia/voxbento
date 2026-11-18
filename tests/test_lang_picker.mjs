/**
 * Unit tests for the booth page's language picker and model filter
 * (portal/static/js/lang-picker.js, portal/static/js/model-filter.js).
 *
 * Run with: node tests/test_lang_picker.mjs
 */
import assert from 'node:assert/strict';

import { initLangPicker, matchLanguages } from '../portal/static/js/lang-picker.js';
import { syncModelOptions } from '../portal/static/js/model-filter.js';

// A just-enough DOM for the picker: elements, children, events and class toggles.
class FakeElement {
  constructor() {
    this.children = [];
    this.handlers = {};
    this.attributes = {};
    this.classes = new Set();
    this.hidden = false;
    this.value = '';
    this.textContent = '';
    this.classList = { toggle: (name, on) => (on ? this.classes.add(name) : this.classes.delete(name)) };
  }

  addEventListener(type, handler) { this.handlers[type] = handler; }
  replaceChildren(...children) { this.children = children; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  scrollIntoView() {}
  focus() { this.focused = true; }
}

globalThis.document = { createElement: () => new FakeElement() };

const LANGUAGES = [
  { code: 'de', name: 'German' },
  { code: 'en', name: 'English' },
  { code: 'es', name: 'Spanish' },
  { code: 'fr', name: 'French' },
];

function createPicker(selectedCodes = []) {
  const els = {
    tagsEl: new FakeElement(),
    searchEl: new FakeElement(),
    dropdownEl: new FakeElement(),
    hiddenEl: new FakeElement(),
  };
  initLangPicker({ ...els, languages: LANGUAGES, selectedCodes, inputName: 'translation_languages' });
  return els;
}

function key(name, { isComposing = false } = {}) {
  return { key: name, isComposing, prevented: false, preventDefault() { this.prevented = true; } };
}

const hiddenCodes = (els) => els.hiddenEl.children.map((input) => input.value);
const tagLabels = (els) => els.tagsEl.children.map((tag) => tag.children[0].textContent);

// matchLanguages
assert.deepEqual(matchLanguages(LANGUAGES, new Set(), 'fr').map((l) => l.code), ['fr']);
assert.deepEqual(matchLanguages(LANGUAGES, new Set(), 'ENG').map((l) => l.code), ['en'], 'name match ignores case');
assert.deepEqual(matchLanguages(LANGUAGES, new Set(), 'es').map((l) => l.code), ['es'], 'code match');
assert.deepEqual(matchLanguages(LANGUAGES, new Set(['de']), '').map((l) => l.code), ['en', 'es', 'fr'], 'selected are left out');
assert.equal(matchLanguages(LANGUAGES, new Set(), '', 2).length, 2, 'the list is capped');
assert.deepEqual(matchLanguages(LANGUAGES, new Set(), 'zzz'), [], 'no match');

// Saved choices render as tags and hidden form inputs.
{
  const els = createPicker(['en', 'fr']);
  assert.deepEqual(tagLabels(els), ['English (en)', 'French (fr)']);
  assert.deepEqual(hiddenCodes(els), ['en', 'fr']);
  assert.equal(els.hiddenEl.children[0].name, 'translation_languages');
  assert.equal(els.hiddenEl.children[0].type, 'hidden');
  assert.equal(els.tagsEl.children[0].children[1].attributes['aria-label'], 'Remove English');
}

// A saved code the list does not know is still submitted, just without a tag.
{
  const els = createPicker(['en', 'xx']);
  assert.deepEqual(tagLabels(els), ['English (en)']);
  assert.deepEqual(hiddenCodes(els), ['en', 'xx']);
}

// Typing suggests languages; choosing one adds it and closes the list.
{
  const els = createPicker(['en']);
  els.searchEl.value = 'ger';
  els.searchEl.handlers.input();
  assert.equal(els.dropdownEl.hidden, false);
  assert.deepEqual(els.dropdownEl.children.map((li) => li.textContent), ['German (de)']);

  const mousedown = { prevented: false, preventDefault() { this.prevented = true; } };
  els.dropdownEl.children[0].handlers.mousedown(mousedown);
  assert.equal(mousedown.prevented, true, 'mousedown keeps focus in the search box');
  assert.deepEqual(hiddenCodes(els), ['en', 'de']);
  assert.deepEqual(tagLabels(els), ['English (en)', 'German (de)']);
  assert.equal(els.searchEl.value, '');
  assert.equal(els.dropdownEl.hidden, true);
  assert.equal(els.searchEl.focused, true);
}

// No match hides the list.
{
  const els = createPicker();
  els.searchEl.value = 'zzz';
  els.searchEl.handlers.input();
  assert.equal(els.dropdownEl.hidden, true);
}

// The remove button drops the language from the tags and the form.
{
  const els = createPicker(['en', 'fr']);
  els.tagsEl.children[0].children[1].handlers.click();
  assert.deepEqual(hiddenCodes(els), ['fr']);
  assert.deepEqual(tagLabels(els), ['French (fr)']);
}

// Keyboard: arrows move through suggestions and Enter picks one.
{
  const els = createPicker();
  els.searchEl.handlers.focus();
  assert.equal(els.dropdownEl.children.length, 4);

  const down = key('ArrowDown');
  els.searchEl.handlers.keydown(down);
  assert.equal(down.prevented, true);
  assert.equal(els.dropdownEl.children[0].classes.has('lp-focused'), true);
  els.searchEl.handlers.keydown(key('ArrowDown'));
  assert.equal(els.dropdownEl.children[1].classes.has('lp-focused'), true);
  assert.equal(els.dropdownEl.children[0].classes.has('lp-focused'), false);
  els.searchEl.handlers.keydown(key('ArrowUp'));
  assert.equal(els.dropdownEl.children[0].classes.has('lp-focused'), true);

  els.searchEl.handlers.keydown(key('ArrowDown'));
  const enter = key('Enter');
  els.searchEl.handlers.keydown(enter);
  assert.equal(enter.prevented, true, 'Enter does not submit the form while picking');
  assert.deepEqual(hiddenCodes(els), ['en']);
}

// The Enter that confirms an IME composition is left to the input method.
{
  const els = createPicker();
  els.searchEl.handlers.focus();
  els.searchEl.handlers.keydown(key('ArrowDown'));
  const enter = key('Enter', { isComposing: true });
  els.searchEl.handlers.keydown(enter);
  assert.equal(enter.prevented, false);
  assert.deepEqual(hiddenCodes(els), [], 'a composing Enter picks nothing');
}

// Enter with nothing highlighted adds nothing; Escape closes; a closed list leaves Enter alone.
{
  const els = createPicker();
  els.searchEl.handlers.focus();
  els.searchEl.handlers.keydown(key('Enter'));
  assert.deepEqual(hiddenCodes(els), []);

  els.searchEl.handlers.keydown(key('Escape'));
  assert.equal(els.dropdownEl.hidden, true);

  const enter = key('Enter');
  els.searchEl.handlers.keydown(enter);
  assert.equal(enter.prevented, false, 'Enter submits the form when the list is closed');
}

// syncModelOptions shows only the chosen provider's models and fixes a stale selection.
{
  const option = (provider, selected = false) => ({ dataset: { provider }, selected, hidden: false, disabled: false });
  const options = [option('local', true), option('local'), option('openai'), option('openai')];

  syncModelOptions(options, 'openai', 'provider');
  assert.deepEqual(options.map((o) => o.hidden), [true, true, false, false]);
  assert.deepEqual(options.map((o) => o.disabled), [true, true, false, false]);
  assert.equal(options[2].selected, true, 'the first model of the new provider is selected');

  options[3].selected = true;
  options[2].selected = false;
  syncModelOptions(options, 'openai', 'provider');
  assert.equal(options[2].selected, false, 'a visible saved choice is kept');

  syncModelOptions(options, 'local', 'provider');
  assert.deepEqual(options.map((o) => o.hidden), [false, false, true, true]);

  syncModelOptions(options, 'unknown', 'provider');
  assert.ok(options.every((o) => o.hidden && o.disabled), 'a provider without models hides all');
}

console.log('lang-picker and model-filter tests passed');
