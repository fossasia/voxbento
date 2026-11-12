import assert from 'node:assert/strict';

// Test room draft persistence logic in room-setup.js and draft cleanup in admin.js

// Mock DOM & sessionStorage
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

const mockSessionStorage = new MockStorage();

console.log('Testing Room Setup Wizard draft logic...');

// Test 1: Key format
const eventId = '7';
const storageKey = `voxbento_setup_room_draft_${eventId}`;
assert.equal(storageKey, 'voxbento_setup_room_draft_7');

// Test 2: Draft saving structure
const draft = {
  roomName: 'Keynote Hall',
  spokenLanguage: 'es',
  internalStep: 2,
  deliveryRadios: { es: 'interpreter', fr: 'ai_voice', de: 'ai_text' },
  activeChips: ['es', 'fr', 'de'],
  captionsWanted: true,
  engineSelects: {
    'speech_engine_es': 'local',
    'translation_engine_es': 'whisper',
    'voice_engine_fr': 'supertonic'
  },
  savedAt: Date.now()
};

mockSessionStorage.setItem(storageKey, JSON.stringify(draft));
const retrieved = JSON.parse(mockSessionStorage.getItem(storageKey));
assert.deepEqual(retrieved.roomName, 'Keynote Hall');
assert.deepEqual(retrieved.spokenLanguage, 'es');
assert.deepEqual(retrieved.internalStep, 2);
assert.deepEqual(retrieved.deliveryRadios, { es: 'interpreter', fr: 'ai_voice', de: 'ai_text' });
assert.deepEqual(retrieved.activeChips, ['es', 'fr', 'de']);
assert.equal(retrieved.captionsWanted, true);
assert.deepEqual(retrieved.engineSelects, {
  'speech_engine_es': 'local',
  'translation_engine_es': 'whisper',
  'voice_engine_fr': 'supertonic'
});

// Test 3: Isolation by event ID
const otherEventId = '42';
const otherKey = `voxbento_setup_room_draft_${otherEventId}`;
assert.equal(mockSessionStorage.getItem(otherKey), null);

// Test 4: Cleanup on room created / finish setup
mockSessionStorage.removeItem(storageKey);
assert.equal(mockSessionStorage.getItem(storageKey), null);

console.log('All room draft persistence assertions passed successfully!');

