import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const downloader = readFileSync(new URL('../portal/static/js/download-model.js', import.meta.url), 'utf8')
  .replace('export function', 'function');

// Exercise the real submit/confirmation handlers with failed saves and triggers.
for (const [saveStatus, triggerStatus] of [[400, 200], [500, 200], [200, 500], [200, 200]]) {
  const elements = new Map();
  const calls = [];
  const alerts = [];
  const intervals = [];
  for (const id of ['room_ai_form', 'floor_translation_provider', 'nllb_download_modal',
    'nllb_proceed_btn', 'nllb_download_progress_container']) {
    elements.set(id, { hidden: true, handlers: {}, addEventListener(type, handler) { this.handlers[type] = handler; } });
  }
  elements.get('floor_translation_provider').value = 'local';
  const form = elements.get('room_ai_form');
  form.getAttribute = () => '/save';
  const context = vm.createContext({
    document: {
      body: { dataset: {} },
      getElementById: (id) => elements.get(id),
      querySelector: (selector) => (selector === 'meta[name="csrf-token"]' ? { content: 'csrf-test' } : null),
    },
    FormData: class {},
    console: { error() {}, debug() {} },
    alert: (message) => alerts.push(message),
    window: { location: { reload() { assert.fail('A failed save must preserve the form'); } } },
    setInterval: (callback) => intervals.push(callback),
    fetch: async (url, options = {}) => {
      calls.push(url);
      if (url.includes('trigger_download')) assert.equal(options.headers['X-CSRF-Token'], 'csrf-test');
      const status = url === '/save' ? saveStatus : url.includes('trigger_download') ? triggerStatus : 200;
      return { ok: status === 200, status, json: async () => ({ status: 'missing' }) };
    },
  });
  vm.runInContext(downloader + '\ninitLocalModelDownloader();', context);
  await form.handlers.submit({ preventDefault() {} });
  assert.equal(elements.get('nllb_download_modal').hidden, false);
  elements.get('nllb_proceed_btn').handlers.click();
  await new Promise(setImmediate);
  assert.equal(calls.some((url) => url.includes('trigger_download')), saveStatus === 200);
  const success = saveStatus === 200 && triggerStatus === 200;
  assert.equal(intervals.length, success ? 1 : 0);
  assert.equal(elements.get('nllb_download_progress_container').hidden, !success);
  assert.equal(alerts.length, success ? 0 : 1);
}

// Model filtering works with and without an enclosing details element.
const roomDetailSource = readFileSync(new URL('../portal/static/js/room-detail.js', import.meta.url), 'utf8');
// The room page shares one tab implementation with the booth page instead of keeping its own.
assert.match(roomDetailSource, /import \{ initTabs \} from '\.\/tabs\.js';/);
assert.doesNotMatch(roomDetailSource, /\bfunction\s+initTabs\b/);
// vm scripts cannot import, so run the page code without its import line (tabs.js has its own tests).
const roomDetail = roomDetailSource.replace(/^import .*$/m, '');
for (const details of [null, { hidden: false }]) {
  const provider = { value: 'local', addEventListener() {} };
  const options = [{ dataset: { provider: 'local' } }, { dataset: { provider: 'openai' } }];
  vm.runInNewContext(roomDetail, {
    document: {
      querySelector: () => null,
      getElementById: (id) => ({
        floor_transcription_provider: provider,
        floor_transcription_model: { options, closest: () => details },
      })[id],
    },
  });
  assert.equal(options[0].selected, true);
  assert.equal(options[1].disabled, true);
  if (details) assert.equal(details.hidden, true);
}
// A saved self-hosted model the server no longer lists isn't offered; an allowed one and a custom
// cloud model stay selected. If the model list fails to load, the saved model is kept for the server to check.
for (const [provider, saved, listLoads, expected, selected] of [
  ['self_hosted', 'removed-model', true, ['', 'llama3.2', 'qwen2.5:7b'], []],
  ['self_hosted', 'qwen2.5:7b', true, ['', 'llama3.2', 'qwen2.5:7b'], ['qwen2.5:7b']],
  ['self_hosted', 'qwen2.5:7b', false, ['', 'qwen2.5:7b'], ['qwen2.5:7b']],
  ['openai', 'my-custom-model', true, ['', 'my-custom-model', 'gpt-4o-mini'], ['my-custom-model']],
]) {
  const added = [];
  const transProvider = { value: provider, addEventListener() {} };
  const transModel = { replaceChildren(...opts) { added.length = 0; added.push(...opts); }, append(opt) { added.push(opt); } };
  const elements = {
    'room-detail': { dataset: { initialTranslationModel: saved } },
    floor_translation_provider: transProvider,
    floor_translation_model: transModel,
  };
  const catalog = { self_hosted: ['llama3.2', 'qwen2.5:7b'], openai: ['gpt-4o-mini'] };
  vm.runInNewContext(roomDetail, {
    console: { error() {} },
    initTabs() {},
    Option: class { constructor(text, value, defaultSelected, isSelected) { this.value = value; this.selected = Boolean(isSelected); } },
    fetch: async () => ({ ok: listLoads, json: async () => catalog }),
    document: { querySelector: () => null, querySelectorAll: () => [], getElementById: (id) => elements[id] },
  });
  await new Promise(setImmediate);
  assert.deepEqual(added.map((opt) => opt.value), expected);
  assert.deepEqual(added.filter((opt) => opt.selected).map((opt) => opt.value), selected);
}
// Switching service while the model list loads must not leave the old service's models in the list,
// whichever load finishes first. A newer load that fails must not throw away a list that loaded.
for (const [reverse, newerFails] of [[false, false], [true, false], [false, true]]) {
  const added = [];
  let onChange;
  const transProvider = { value: 'self_hosted', addEventListener(type, handler) { onChange = handler; } };
  const transModel = { replaceChildren(...opts) { added.length = 0; added.push(...opts); }, append(opt) { added.push(opt); } };
  const elements = {
    'room-detail': { dataset: { initialTranslationModel: 'qwen2.5:7b' } },
    floor_translation_provider: transProvider,
    floor_translation_model: transModel,
  };
  const pending = [];
  vm.runInNewContext(roomDetail, {
    console: { error() {} },
    initTabs() {},
    Option: class { constructor(text, value, defaultSelected, isSelected) { this.value = value; this.selected = Boolean(isSelected); } },
    fetch: () => new Promise((resolve) => pending.push(resolve)),
    document: { querySelector: () => null, querySelectorAll: () => [], getElementById: (id) => elements[id] },
  });
  transProvider.value = 'openai';
  onChange();
  const catalog = { self_hosted: ['llama3.2', 'qwen2.5:7b'], openai: ['gpt-4o-mini'] };
  for (const [i, resolve] of (reverse ? [...pending.entries()].reverse() : [...pending.entries()])) {
    resolve(newerFails && i === 1 ? { ok: false } : { ok: true, json: async () => catalog });
    await new Promise(setImmediate);
  }
  assert.deepEqual(added.map((opt) => opt.value), ['', 'gpt-4o-mini']);
  assert.deepEqual(added.filter((opt) => opt.selected).map((opt) => opt.value), []);
}
console.log('Room AI regression checks passed');
