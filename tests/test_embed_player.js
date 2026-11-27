'use strict';

const fs = require('fs');
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

function makeFakeDoc(configObj) {
  const elements = new Map();
  function makeEl(id, extra = {}) {
    const el = {
      id,
      textContent: '',
      className: '',
      classList: {
        add() {},
        remove() {},
      },
      children: [],
      listeners: {},
      disabled: false,
      hidden: false,
      volume: 1,
      scrollHeight: 0,
      scrollTop: 0,
      clientHeight: 0,
      addEventListener(ev, fn) { this.listeners[ev] = fn; },
      appendChild(c) { this.children.push(c); },
      removeChild(c) {
        const idx = this.children.indexOf(c);
        if (idx >= 0) this.children.splice(idx, 1);
      },
      play() { return Promise.resolve(); },
      pause() {},
      ...extra,
    };
    elements.set(id, el);
    return el;
  }

  makeEl('embed-config', { textContent: JSON.stringify(configObj) });
  makeEl('embed-audio');
  makeEl('play-btn');
  makeEl('status-dot');
  makeEl('status-text');
  if (configObj.captions_enabled) {
    makeEl('captions-box');
    makeEl('embed-caption-history');
    makeEl('embed-caption-current');
  }

  return {
    elements,
    getElementById(id) { return elements.get(id) || null; },
    createElement() { return { textContent: '' }; },
  };
}

function makeFakeJwt(expSeconds) {
  const header = Buffer.from(JSON.stringify({ alg: 'HS256' })).toString('base64url');
  const payload = Buffer.from(JSON.stringify({ kind: 'listener', exp: expSeconds })).toString('base64url');
  return `${header}.${payload}.sig`;
}

async function runTests() {
  const mod = await import(
    pathToFileURL(path.join(__dirname, '..', 'portal', 'static', 'js', 'embed-player.js')).href
  );

  // Test 1: isJwtExpired helper
  console.log('\nTest 1: isJwtExpired');
  assert(mod.isJwtExpired(makeFakeJwt(1000), 2000) === true, 'Expired JWT returns true');
  assert(mod.isJwtExpired(makeFakeJwt(3000), 2000) === false, 'Valid JWT returns false');

  // Test 2: TTS mode connects /ws/tts, decodes PCM, supports play/pause/set_volume/delay/expiry
  console.log('\nTest 2: TTS mode playback, volume, pause, delay, and auth expiry');
  {
    const sockets = [];
    const posted = [];
    const scheduledBuffers = [];
    let schedulerResetCount = 0;
    let resumeCalls = 0;
    let suspendCalls = 0;
    let masterGain = null;
    const messageListeners = [];

    class FakeWS {
      constructor(url) {
        this.url = url;
        this.binaryType = '';
        sockets.push(this);
      }
      close() {}
    }

    class FakeAudioContext {
      constructor() {
        this.state = 'suspended';
        this.destination = { label: 'dest' };
      }
      createGain() {
        masterGain = { gain: { value: 1 }, connect() {} };
        return masterGain;
      }
      createBuffer(ch, len, rate) {
        return { ch, len, rate, copyToChannel() {} };
      }
      resume() {
        resumeCalls++;
        this.state = 'running';
        return Promise.resolve();
      }
      suspend() {
        suspendCalls++;
        this.state = 'suspended';
        return Promise.resolve();
      }
    }

    const doc = makeFakeDoc({
      audio_mode: 'tts',
      whep_url: '',
      tts_url: 'wss://example.com/ws/tts/5/es/ev-5-floor',
      caption_url: 'wss://example.com/ws/captions/ev-5-es',
      captions_enabled: false,
      token: makeFakeJwt(Math.floor(Date.now() / 1000) + 600),
      audio_delay_ms: 0,
      source_lang_code: 'es',
      target_lang_code: 'es',
      headless: true,
      target_origin: '*',
      allowed_origins: [],
    });

    const fakeWin = {
      location: { protocol: 'https:', host: 'example.com' },
      parent: { postMessage(msg) { posted.push(msg); } },
      addEventListener(ev, fn) { if (ev === 'message') messageListeners.push(fn); },
      setTimeout,
      clearTimeout,
    };

    mod.initEmbedPlayer({
      document: doc,
      window: fakeWin,
      WebSocket: FakeWS,
      AudioContext: FakeAudioContext,
      TTSParser: {
        parseFrame() {
          return {
            header: { seq: 1, translation: 'Hola', error: null, audio_pending: false },
            audioBytes: new Uint8Array([0, 0, 100, 0]),
          };
        },
      },
      AudioScheduler: {
        create(_ctx, opts) {
          assert(opts.destination === masterGain, 'AudioScheduler receives master GainNode');
          return {
            scheduleBuffer(buf) { scheduledBuffers.push(buf); },
            reset() { schedulerResetCount++; },
          };
        },
      },
    });

    assert(sockets.length === 1 && sockets[0].url.includes('/ws/tts/5/es/ev-5-floor?token='), 'Opens /ws/tts with token');
    sockets[0].onopen();
    assert(posted.some((m) => m.type === 'booth_state' && m.payload.state === 'connected'), 'Emits connected booth_state');

    // Frame arriving before play is ignored
    sockets[0].onmessage({ data: new ArrayBuffer(8) });
    assert(scheduledBuffers.length === 0, 'Does not schedule audio before play');

    // Parent play + set_volume + frame
    messageListeners[0]({ origin: 'https://parent', data: { source: 'voxbento-parent', type: 'set_volume', volume: 0.4 } });
    await messageListeners[0]({ origin: 'https://parent', data: { source: 'voxbento-parent', type: 'play' } });
    await Promise.resolve();
    assert(resumeCalls === 1, 'Resumes AudioContext on play');
    assert(masterGain.gain.value === 0.4, 'Applies set_volume to master GainNode');

    sockets[0].onmessage({ data: new ArrayBuffer(8) });
    assert(scheduledBuffers.length === 1, 'Schedules decoded PCM buffer when playing');

    // Pause suspends AudioContext and resets scheduler
    messageListeners[0]({ origin: 'https://parent', data: { source: 'voxbento-parent', type: 'pause' } });
    assert(suspendCalls === 1 && schedulerResetCount === 1, 'Pause suspends AudioContext and resets scheduler');

    // 4003 close stops reconnects and marks expired
    sockets[0].onclose({ code: 4003 });
    assert(doc.getElementById('status-text').textContent === 'Session expired', 'Marks expired on 4003');
    assert(posted.some((m) => m.type === 'error' && m.payload.code === 'session_expired'), 'Emits session_expired error');
  }

  // Test 3: Captions-only ("none") mode + translated_caption without language_code
  console.log('\nTest 3: Captions-only mode and translated_caption handling');
  {
    const sockets = [];
    class FakeWS {
      constructor(url) { this.url = url; sockets.push(this); }
      close() {}
    }
    const doc = makeFakeDoc({
      audio_mode: 'none',
      whep_url: '',
      tts_url: '',
      caption_url: 'wss://example.com/ws/captions/ev-5-de',
      captions_enabled: true,
      token: makeFakeJwt(Math.floor(Date.now() / 1000) + 600),
      audio_delay_ms: 0,
      source_lang_code: 'de',
      target_lang_code: 'de',
      headless: false,
      target_origin: '*',
      allowed_origins: [],
    });

    mod.initEmbedPlayer({
      document: doc,
      window: {
        location: { protocol: 'https:', host: 'example.com' },
        setTimeout,
        clearTimeout,
      },
      WebSocket: FakeWS,
    });

    assert(doc.getElementById('play-btn').hidden === true, 'Hides play button in captions-only mode');
    assert(sockets.length === 1 && sockets[0].url.includes('/ws/captions/ev-5-de?token='), 'Connects only captionWs');
    sockets[0].onopen();
    assert(doc.getElementById('status-text').textContent === 'Captions only', 'Shows Captions only status');
    sockets[0].onmessage({ data: JSON.stringify({ type: 'translated_caption', status: 'final', text: 'Hallo Welt' }) });
    assert(doc.getElementById('embed-caption-history').children.length === 1, 'Renders translated_caption without language_code');
    assert(doc.getElementById('embed-caption-history').children[0].textContent === 'Hallo Welt', 'Rendered text matches');
  }

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
  process.exit(failed > 0 ? 1 : 0);
}

runTests().catch((err) => {
  console.error(err);
  process.exit(1);
});
