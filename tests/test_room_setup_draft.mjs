import assert from 'node:assert/strict';

// Set up minimal global environment for Node.js module loading
class MockStorage {
  constructor() {
    this.store = {};
  }
  getItem(key) {
    return Object.prototype.hasOwnProperty.call(this.store, key) ? this.store[key] : null;
  }
  setItem(key, value) {
    this.store[key] = String(value);
  }
  removeItem(key) {
    delete this.store[key];
  }
  clear() {
    this.store = {};
  }
}

let querySelectorImpl = () => null;

globalThis.window = globalThis;
globalThis.document = {
  addEventListener() {},
  querySelectorAll() { return []; },
  querySelector(selector) { return querySelectorImpl(selector); },
  getElementById() { return null; },
  cookie: '',
};

const {
  applyDraftToForm,
  buildDraftFromForm,
  clearRoomDraft,
  getDraftStorageKey,
  loadRoomDraft,
  saveRoomDraft,
} = await import('../portal/static/js/room-setup.js');

const { initSetupDraftCleanup } = await import('../portal/static/js/admin.js');

const mockStorage = new MockStorage();

console.log('Testing Room Setup Wizard draft logic with DOM-based application code...');

// 1. Storage key generation rules
assert.equal(getDraftStorageKey(true, true, '7'), 'voxbento_setup_room_draft_7');
assert.equal(getDraftStorageKey(false, true, '7'), null, 'Edit mode must not use draft');
assert.equal(getDraftStorageKey(true, false, '7'), null, 'Standalone room creation must not use draft');
assert.equal(getDraftStorageKey(true, true, ''), null, 'Empty eventId must not use draft');

// 2. DOM-based test: simulate form elements and exercise buildDraftFromForm + saveRoomDraft
class FakeInput {
  constructor(value = '', checked = false) {
    this.value = value;
    this.checked = checked;
  }
}

class FakeSelect {
  constructor(value = '', options = []) {
    this.value = value;
    this.options = options.map((opt) => (typeof opt === 'string' ? { value: opt, textContent: opt } : opt));
  }
  querySelector(selector) {
    const match = selector.match(/option\[value="([^"]+)"\]/);
    if (match) {
      return this.options.find((opt) => opt.value === match[1]) || null;
    }
    return null;
  }
}

class FakeElement {
  constructor() {
    this.children = new Map();
  }
  querySelector(selector) {
    return this.children.get(selector) || null;
  }
  querySelectorAll() {
    return [...this.children.values()];
  }
}

// Build initial source form
const sourceForm = new FakeElement();
sourceForm.children.set('#rs-submission-id', new FakeInput('sub-token-uuid-12345'));
sourceForm.children.set('#rs-name', new FakeInput('Main Stage'));
sourceForm.children.set('#rs-speech', new FakeInput('local'));
sourceForm.children.set('#rs-translation', new FakeInput('local'));
sourceForm.children.set('#rs-voice', new FakeInput('supertonic'));

const sourceSpokenSelect = new FakeSelect('en', ['en', 'es', 'fr', 'de']);

const chosenCodes = ['es', 'fr', 'de'];
const deliveryMap = { es: 'interpreter', fr: 'ai_voice', de: 'ai_text' };
const deliveryOf = (code) => deliveryMap[code] || '';

// Call application's buildDraftFromForm
const generatedDraft = buildDraftFromForm({
  form: sourceForm,
  spokenSelect: sourceSpokenSelect,
  chosenCodes,
  deliveryOf,
  captionsWanted: true,
  currentStep: 2,
});

// Verify draft contains all expected fields
assert.equal(generatedDraft.submissionId, 'sub-token-uuid-12345');
assert.equal(generatedDraft.roomName, 'Main Stage');
assert.equal(generatedDraft.spokenLanguage, 'en');
assert.deepEqual(generatedDraft.listenerLanguages, ['es', 'fr', 'de']);
assert.deepEqual(generatedDraft.deliveries, { es: 'interpreter', fr: 'ai_voice', de: 'ai_text' });
assert.equal(generatedDraft.captionsWanted, true);
assert.equal(generatedDraft.speechEngine, 'local');
assert.equal(generatedDraft.translationEngine, 'local');
assert.equal(generatedDraft.voiceEngine, 'supertonic');
assert.equal(generatedDraft.currentStep, 2);

// Save generated draft into storage
const key7 = getDraftStorageKey(true, true, '7');
saveRoomDraft(mockStorage, key7, generatedDraft);

// 3. Test restoration via applyDraftToForm into a fresh DOM form
const targetForm = new FakeElement();
targetForm.children.set('#rs-submission-id', new FakeInput('initial-fresh-id'));
targetForm.children.set('#rs-name', new FakeInput(''));
targetForm.children.set('#rs-speech', new FakeSelect('auto', ['auto', 'local', 'whisper']));
targetForm.children.set('#rs-translation', new FakeSelect('auto', ['auto', 'local']));
targetForm.children.set('#rs-voice', new FakeSelect('auto', ['auto', 'supertonic']));

const targetSpokenSelect = new FakeSelect('auto', ['auto', 'en', 'es', 'fr']);

const chipsBox = new FakeElement();
const esChipInput = new FakeInput('es', false);
const frChipInput = new FakeInput('fr', false);
chipsBox.children.set('[data-rs-chip="es"] input', esChipInput);
chipsBox.children.set('[data-rs-chip="fr"] input', frChipInput);

const moreSelect = new FakeSelect('', [
  { value: 'de', textContent: 'German' },
  { value: 'it', textContent: 'Italian' },
]);

const addedChips = [];
const addChip = (code, label) => {
  addedChips.push({ code, label });
  const newInput = new FakeInput(code, false);
  chipsBox.children.set(`[data-rs-chip="${code}"] input`, newInput);
};

const retrievedDraft = loadRoomDraft(mockStorage, key7);
assert.notEqual(retrievedDraft, null);

// Include an unavailable language code 'xx' in draft to verify skipping unavailable codes
retrievedDraft.listenerLanguages.push('xx');

applyDraftToForm({
  form: targetForm,
  draft: retrievedDraft,
  chipsBox,
  moreSelect,
  spokenSelect: targetSpokenSelect,
  addChip,
});

// Assertions on restored form fields
assert.equal(targetForm.querySelector('#rs-submission-id').value, 'sub-token-uuid-12345');
assert.equal(targetForm.querySelector('#rs-name').value, 'Main Stage');
assert.equal(targetSpokenSelect.value, 'en');
assert.equal(targetForm.querySelector('#rs-speech').value, 'local');
assert.equal(targetForm.querySelector('#rs-translation').value, 'local');
assert.equal(targetForm.querySelector('#rs-voice').value, 'supertonic');

// Chips in catalog were checked
assert.equal(esChipInput.checked, true);
assert.equal(frChipInput.checked, true);

// 'de' from moreSelect was dynamically added and checked
assert.equal(addedChips.some((c) => c.code === 'de' && c.label === 'German'), true);
const deInput = chipsBox.querySelector('[data-rs-chip="de"] input');
assert.equal(deInput?.checked, true);

// Unavailable code 'xx' was NOT added as a chip
assert.equal(addedChips.some((c) => c.code === 'xx'), false);
assert.equal(chipsBox.querySelector('[data-rs-chip="xx"] input'), null);

// Legacy draft without submissionId must preserve the fresh form submissionId
const formWithFreshId = new FakeElement();
formWithFreshId.children.set('#rs-submission-id', new FakeInput('keep-this-fresh-id'));
applyDraftToForm({
  form: formWithFreshId,
  draft: { roomName: 'Legacy Draft' },
  chipsBox: new FakeElement(),
  spokenSelect: new FakeSelect('auto', ['auto']),
});
assert.equal(formWithFreshId.querySelector('#rs-submission-id').value, 'keep-this-fresh-id');

// 4. Event isolation
const key8 = getDraftStorageKey(true, true, '8');
assert.equal(loadRoomDraft(mockStorage, key8), null, 'Draft must be isolated per event ID');

// 5. Multi-tab draft cleanup in admin.js
const storageTabA = new MockStorage();
const storageTabB = new MockStorage();

const tokenA = 'token-tab-a-123';
const tokenB = 'token-tab-b-456';

storageTabA.setItem('voxbento_setup_room_draft_7', JSON.stringify({ roomName: 'Tab A Room' }));
storageTabA.setItem('voxbento_setup_room_draft_token_7', tokenA);

storageTabB.setItem('voxbento_setup_room_draft_7', JSON.stringify({ roomName: 'Tab B Room' }));
storageTabB.setItem('voxbento_setup_room_draft_token_7', tokenB);

// Simulate Tab A created room: page renders with Tab A's cleanup token
querySelectorImpl = (selector) => {
  if (selector === '[data-clear-room-draft]') {
    return {
      dataset: {
        clearRoomDraft: '7',
        clearRoomDraftToken: tokenA,
      },
    };
  }
  return null;
};

// In Tab B: Tab A's token does NOT match Tab B's stored token -> draft must NOT be cleared!
initSetupDraftCleanup(storageTabB);
assert.notEqual(storageTabB.getItem('voxbento_setup_room_draft_7'), null, 'Tab B draft must be preserved');
assert.equal(storageTabB.getItem('voxbento_setup_room_draft_token_7'), tokenB);

// In Tab A: Tab A's token matches -> Tab A draft IS cleared!
initSetupDraftCleanup(storageTabA);
assert.equal(storageTabA.getItem('voxbento_setup_room_draft_7'), null, 'Tab A draft must be cleared');
assert.equal(storageTabA.getItem('voxbento_setup_room_draft_token_7'), null);

// 6. Cookie cleanup failure retaining retry signal
const failingStorage = {
  getItem(key) {
    if (key.includes('voxbento_setup_room_draft_token_7')) return 'retry-token';
    return '{"roomName":"Retry Room"}';
  },
  removeItem() {
    throw new Error('Quota or permissions failure');
  },
};
globalThis.document.cookie = 'room_created_token=7%3Aretry-token';
initSetupDraftCleanup(failingStorage);
// Cookie must be retained (not cleared) when removeItem fails
assert.equal(globalThis.document.cookie.includes('room_created_token'), true);

// 7. Finish setup cleanup on destination load
const storageFinish = new MockStorage();
storageFinish.setItem('voxbento_setup_room_draft_7', JSON.stringify({ roomName: 'Done Room' }));
storageFinish.setItem('voxbento_setup_room_draft_token_7', 'token-done');

querySelectorImpl = (selector) => {
  if (selector === '[data-clear-setup-draft]') {
    return { dataset: { clearSetupDraft: '7' } };
  }
  return null;
};

initSetupDraftCleanup(storageFinish);
assert.equal(storageFinish.getItem('voxbento_setup_room_draft_7'), null, 'Finish setup must clear draft on destination load');
assert.equal(storageFinish.getItem('voxbento_setup_room_draft_token_7'), null);

// 8. Malformed URI cookie decoding resilience
const malformedStorage = new MockStorage();
malformedStorage.setItem('voxbento_setup_room_draft_7', JSON.stringify({ roomName: 'Malformed Test' }));
globalThis.document.cookie = 'room_created_token=7%E0%A4A';
// Must not throw URIError when handling malformed cookie
assert.doesNotThrow(() => {
  initSetupDraftCleanup(malformedStorage);
});

// 9. Storage getItem failure retaining cookie without throwing
const failingGetItemStorage = {
  getItem() {
    throw new Error('Access denied to storage item');
  },
  removeItem() {},
};
globalThis.document.cookie = 'room_created_token=7%3Afail-token';
assert.doesNotThrow(() => {
  initSetupDraftCleanup(failingGetItemStorage);
});
assert.equal(globalThis.document.cookie.includes('room_created_token'), true, 'Cookie must be retained when getItem throws');

// 10. Unguarded global invocation without storage argument
assert.doesNotThrow(() => {
  initSetupDraftCleanup();
});

console.log('All DOM-based room draft persistence and cleanup assertions passed successfully!');
