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

  // Test 4: Reconnect clears pending Stage-1 segmentStore so playback does not stall
  console.log('\nTest 4: TTS reconnect clears pending Stage-1 segment and resumes playback');
  {
    const sockets = [];
    const scheduledSeqs = [];
    const timers = [];
    let currentFrame = null;

    class FakeWS {
      constructor(url) { this.url = url; this.binaryType = ''; sockets.push(this); }
      close() {}
    }
    class FakeAudioContext {
      constructor() { this.state = 'suspended'; this.destination = {}; }
      createGain() { return { gain: { value: 1 }, connect() {} }; }
      createBuffer(_ch, len) { return { len, copyToChannel() {} }; }
      resume() { this.state = 'running'; return Promise.resolve(); }
      suspend() { this.state = 'suspended'; return Promise.resolve(); }
    }

    const doc = makeFakeDoc({
      audio_mode: 'tts',
      whep_url: '',
      tts_url: 'wss://example.com/ws/tts/5/es/ev-5-floor',
      caption_url: '',
      captions_enabled: false,
      token: makeFakeJwt(Math.floor(Date.now() / 1000) + 600),
      audio_delay_ms: 0,
      source_lang_code: 'es',
      target_lang_code: 'es',
      headless: false,
      target_origin: '*',
      allowed_origins: [],
    });

    mod.initEmbedPlayer({
      document: doc,
      window: { location: { protocol: 'https:', host: 'example.com' }, setTimeout, clearTimeout },
      WebSocket: FakeWS,
      AudioContext: FakeAudioContext,
      TTSParser: { parseFrame: () => currentFrame },
      AudioScheduler: {
        create() {
          return {
            scheduleBuffer(buf) { scheduledSeqs.push(buf.len); },
            reset() {},
          };
        },
      },
      setTimeout(fn, delay) {
        const entry = { fn, delay, cancelled: false };
        timers.push(entry);
        return entry;
      },
      clearTimeout(entry) {
        if (entry) entry.cancelled = true;
      },
    });

    sockets[0].onopen();
    await doc.getElementById('play-btn').listeners.click();
    await Promise.resolve();

    // Stage 1 (audio_pending: true, no audioBytes) arrives for seq 1 right before disconnect
    currentFrame = {
      header: { seq: 1, translation: 'Hola', error: null, audio_pending: true },
      audioBytes: new Uint8Array(0),
    };
    sockets[0].onmessage({ data: new ArrayBuffer(8) });
    assert(scheduledSeqs.length === 0, 'Stage 1 without audio does not schedule playback');

    // Socket drops unexpectedly and reconnects
    sockets[0].onclose({ code: 1006 });
    const reconnectTimer = timers.find((t) => !t.cancelled && t.delay >= 500 && t.delay <= 1000);
    assert(Boolean(reconnectTimer), 'Schedules reconnect timer after 1006');
    reconnectTimer.fn();
    assert(sockets.length === 2, 'Opens replacement TTS WebSocket');
    sockets[1].onopen();

    // New seq 2 with audio arrives after reconnect: must NOT be blocked by stale seq 1
    currentFrame = {
      header: { seq: 2, translation: 'Mundo', error: null, audio_pending: false },
      audioBytes: new Uint8Array([0, 0, 100, 0]),
    };
    sockets[1].onmessage({ data: new ArrayBuffer(8) });
    assert(scheduledSeqs.length === 1, 'Plays seq 2 after reconnect without stalling on pre-disconnect seq 1');
  }

  // Test 5: Dropped Stage-2 audio evicts oldest segment on overflow (>5) and also times out via seqWaitTimer
  console.log('\nTest 5: Dropped Stage-2 audio is evicted on overflow and timed out via seqWaitTimer');
  {
    const sockets = [];
    const scheduled = [];
    const timers = [];
    let currentFrame = null;

    class FakeWS {
      constructor(url) { this.url = url; this.binaryType = ''; sockets.push(this); }
      close() {}
    }
    class FakeAudioContext {
      constructor() { this.state = 'suspended'; this.destination = {}; }
      createGain() { return { gain: { value: 1 }, connect() {} }; }
      createBuffer(_ch, len) { return { len, copyToChannel() {} }; }
      resume() { this.state = 'running'; return Promise.resolve(); }
      suspend() { this.state = 'suspended'; return Promise.resolve(); }
    }

    const doc = makeFakeDoc({
      audio_mode: 'tts',
      whep_url: '',
      tts_url: 'wss://example.com/ws/tts/5/es/ev-5-floor',
      caption_url: '',
      captions_enabled: false,
      token: makeFakeJwt(Math.floor(Date.now() / 1000) + 600),
      audio_delay_ms: 0,
      source_lang_code: 'es',
      target_lang_code: 'es',
      headless: false,
      target_origin: '*',
      allowed_origins: [],
    });

    mod.initEmbedPlayer({
      document: doc,
      window: { location: { protocol: 'https:', host: 'example.com' }, setTimeout, clearTimeout },
      WebSocket: FakeWS,
      AudioContext: FakeAudioContext,
      TTSParser: { parseFrame: () => currentFrame },
      AudioScheduler: {
        create() {
          return {
            scheduleBuffer(buf) { scheduled.push(buf.len); },
            reset() {},
          };
        },
      },
      setTimeout(fn, delay) {
        const entry = { fn, delay, cancelled: false };
        timers.push(entry);
        return entry;
      },
      clearTimeout(entry) {
        if (entry) entry.cancelled = true;
      },
    });

    sockets[0].onopen();
    await doc.getElementById('play-btn').listeners.click();
    await Promise.resolve();

    // Stage 1 for seq 1 arrives without Stage 2; seqWaitTimer (65000ms) must be armed
    currentFrame = {
      header: { seq: 1, translation: 'One', error: null, audio_pending: true },
      audioBytes: new Uint8Array(0),
    };
    sockets[0].onmessage({ data: new ArrayBuffer(8) });
    const waitTimer = timers.find((t) => !t.cancelled && t.delay >= 60000);
    assert(Boolean(waitTimer), 'Arms 65s wait timer when Stage-1 segment has audioBuffer === null');

    // Also send seq 2..7 with audio to exceed depth 5: seq 1 must be evicted and seq 2 scheduled
    for (let s = 2; s <= 7; s++) {
      currentFrame = {
        header: { seq: s, translation: 'Seg ' + s, error: null, audio_pending: false },
        audioBytes: new Uint8Array([0, 0, 100, 0]),
      };
      sockets[0].onmessage({ data: new ArrayBuffer(8) });
    }
    assert(scheduled.length >= 1, 'Evicts stuck seq 1 on overflow (>5) and schedules seq 2');

    // Pump remaining via fallbackQueueTimer (30ms)
    let safety = 0;
    while (safety++ < 10) {
      const pumpTimer = timers.find((t) => !t.cancelled && t.delay === 30);
      if (!pumpTimer) break;
      pumpTimer.cancelled = true;
      pumpTimer.fn();
    }
    assert(scheduled.length === 6, 'Drains all remaining buffered segments (seq 2..7)');
  }

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
  process.exit(failed > 0 ? 1 : 0);
}

runTests().catch((err) => {
  console.error(err);
  process.exit(1);
});
