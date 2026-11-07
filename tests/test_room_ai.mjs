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
    document: { body: { dataset: {} }, getElementById: (id) => elements.get(id) },
    FormData: class {},
    console: { error() {}, debug() {} },
    alert: (message) => alerts.push(message),
    window: { location: { reload() { assert.fail('A failed save must preserve the form'); } } },
    setInterval: (callback) => intervals.push(callback),
    fetch: async (url) => {
      calls.push(url);
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
const roomDetail = readFileSync(new URL('../portal/static/js/room-detail.js', import.meta.url), 'utf8');
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
console.log('Room AI regression checks passed');
