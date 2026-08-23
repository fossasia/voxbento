/**
 * Unit tests for AudioScheduler — the jitter-buffered playback queue.
 *
 * Run with: node tests/test_audio_scheduler.js
 *
 * Uses a minimal AudioContext mock since we're testing scheduling logic,
 * not actual Web Audio API behaviour.
 */
'use strict';

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

function assertApprox(actual, expected, tolerance, message) {
  const diff = Math.abs(actual - expected);
  if (diff > tolerance) {
    console.error(
      '  FAIL: ' + message + ' (expected ~' + expected + ', got ' + actual + ')'
    );
    failed++;
  } else {
    console.log('  PASS: ' + message);
    passed++;
  }
}

// ── Minimal AudioContext mock ────────────────────────────────────────────

function MockAudioContext() {
  this._currentTime = 0;
  this.sampleRate = 24000;
  this.state = 'running';
  this.destination = {};
}

Object.defineProperty(MockAudioContext.prototype, 'currentTime', {
  get: function () {
    return this._currentTime;
  },
});

MockAudioContext.prototype.createBuffer = function (channels, length, rate) {
  return {
    duration: length / rate,
    numberOfChannels: channels,
    sampleRate: rate,
    length: length,
    getChannelData: function () {
      return new Float32Array(length);
    },
    copyToChannel: function () {},
  };
};

MockAudioContext.prototype.createBufferSource = function () {
  return {
    buffer: null,
    loop: false,
    connect: function () {},
    start: function () {},
    stop: function () {},
    onended: null,
  };
};

MockAudioContext.prototype.createGain = function () {
  return {
    gain: {
      value: 1,
      setValueAtTime: function () {},
      linearRampToValueAtTime: function () {},
      cancelScheduledValues: function () {},
    },
    connect: function () {},
    disconnect: function () {},
  };
};

MockAudioContext.prototype.createDelay = function () {
  return {
    delayTime: { value: 0 },
    connect: function () {},
    disconnect: function () {},
  };
};

MockAudioContext.prototype.createMediaStreamSource = function () {
  return { connect: function () {}, disconnect: function () {} };
};

MockAudioContext.prototype.close = function () {
  this.state = 'closed';
  return Promise.resolve();
};

MockAudioContext.prototype.resume = function () {
  this.state = 'running';
  return Promise.resolve();
};

// ── Load the module ──────────────────────────────────────────────────────

// We need window.AudioScheduler so we emulate the global
global.window = global.window || {};
global.setTimeout = global.setTimeout;
global.clearTimeout = global.clearTimeout;

// Load the module (it attaches to window.AudioScheduler)
require('../static/js/audio-scheduler.js');

const createAudioScheduler = window.AudioScheduler.create;

// ── Tests ────────────────────────────────────────────────────────────────

console.log('\n=== AudioScheduler Tests ===\n');

// Test 1: First buffer schedules at now + jitterBuffer
(function () {
  console.log('Test 1: First buffer schedules at now + jitterBufferSec');
  const ctx = new MockAudioContext();
  ctx._currentTime = 10.0;

  const scheduler = createAudioScheduler(ctx, {
    jitterBufferSec: 0.25,
    comfortNoiseEnabled: false,
  });

  const fakeBuffer = ctx.createBuffer(1, 24000, 24000); // 1 second
  fakeBuffer.duration = 1.0;

  const timing = scheduler.scheduleBuffer(fakeBuffer);
  assertApprox(timing.startTime, 10.25, 0.01, 'startTime = now + 0.25');
  assertApprox(timing.endTime, 11.25, 0.01, 'endTime = startTime + 1.0');
})();

// Test 2: Consecutive buffers schedule seamlessly (no gap)
(function () {
  console.log('\nTest 2: Consecutive buffers schedule seamlessly');
  const ctx = new MockAudioContext();
  ctx._currentTime = 5.0;

  const scheduler = createAudioScheduler(ctx, {
    jitterBufferSec: 0.25,
    comfortNoiseEnabled: false,
  });

  const buf1 = ctx.createBuffer(1, 24000, 24000);
  buf1.duration = 1.0;
  const buf2 = ctx.createBuffer(1, 12000, 24000);
  buf2.duration = 0.5;

  const t1 = scheduler.scheduleBuffer(buf1);
  // Simulate time NOT advancing (both arrive in same tick)
  const t2 = scheduler.scheduleBuffer(buf2);

  assertApprox(t2.startTime, t1.endTime, 0.001, 'Second buffer starts exactly when first ends');
  assertApprox(t2.endTime, t1.endTime + 0.5, 0.001, 'Second buffer endTime is correct');
})();

// Test 3: After a gap, re-anchors with jitter buffer
(function () {
  console.log('\nTest 3: After a gap, re-anchors with jitter buffer');
  const ctx = new MockAudioContext();
  ctx._currentTime = 0.0;

  const scheduler = createAudioScheduler(ctx, {
    jitterBufferSec: 0.25,
    comfortNoiseEnabled: false,
  });

  const buf1 = ctx.createBuffer(1, 24000, 24000);
  buf1.duration = 1.0;
  scheduler.scheduleBuffer(buf1);

  // Simulate time advancing past the end of buf1
  ctx._currentTime = 5.0;

  const buf2 = ctx.createBuffer(1, 24000, 24000);
  buf2.duration = 1.0;
  const t2 = scheduler.scheduleBuffer(buf2);

  assertApprox(t2.startTime, 5.25, 0.01, 'After gap, re-anchors at now + jitter');
  assertApprox(t2.endTime, 6.25, 0.01, 'End time accounts for duration');
})();

// Test 4: isIdle() returns correct state
(function () {
  console.log('\nTest 4: isIdle() returns correct state');
  const ctx = new MockAudioContext();
  ctx._currentTime = 0.0;

  const scheduler = createAudioScheduler(ctx, {
    jitterBufferSec: 0.25,
    comfortNoiseEnabled: false,
  });

  assert(scheduler.isIdle(), 'Initially idle (no buffers scheduled)');

  const buf = ctx.createBuffer(1, 24000, 24000);
  buf.duration = 1.0;
  scheduler.scheduleBuffer(buf);

  assert(!scheduler.isIdle(), 'Not idle after scheduling a buffer');

  ctx._currentTime = 2.0; // past the end
  assert(scheduler.isIdle(), 'Idle after buffer has played');
})();

// Test 5: reset() clears state
(function () {
  console.log('\nTest 5: reset() clears state');
  const ctx = new MockAudioContext();
  ctx._currentTime = 0.0;

  const scheduler = createAudioScheduler(ctx, {
    jitterBufferSec: 0.25,
    comfortNoiseEnabled: false,
  });

  const buf = ctx.createBuffer(1, 24000, 24000);
  buf.duration = 1.0;
  scheduler.scheduleBuffer(buf);

  scheduler.reset();

  assert(scheduler.isIdle(), 'Idle after reset');
  assertApprox(scheduler.getNextScheduledTime(), 0, 0.001, 'nextScheduledTime reset to 0');

  // New buffer after reset should re-anchor
  ctx._currentTime = 10.0;
  const buf2 = ctx.createBuffer(1, 24000, 24000);
  buf2.duration = 1.0;
  const t = scheduler.scheduleBuffer(buf2);
  assertApprox(t.startTime, 10.25, 0.01, 'After reset, re-anchors correctly');
})();

// Test 6: Empty/null buffers are handled gracefully
(function () {
  console.log('\nTest 6: Empty/null buffers are handled gracefully');
  const ctx = new MockAudioContext();
  ctx._currentTime = 0.0;

  const scheduler = createAudioScheduler(ctx, {
    jitterBufferSec: 0.25,
    comfortNoiseEnabled: false,
  });

  const t1 = scheduler.scheduleBuffer(null);
  assert(t1.startTime === 0 && t1.endTime === 0, 'Null buffer returns zero timing');

  const zeroBuf = ctx.createBuffer(1, 1, 24000);
  zeroBuf.duration = 0;
  const t2 = scheduler.scheduleBuffer(zeroBuf);
  assert(t2.startTime === 0 && t2.endTime === 0, 'Zero-duration buffer returns zero timing');
})();

// Test 7: Multiple rapid buffers chain correctly
(function () {
  console.log('\nTest 7: Multiple rapid buffers chain correctly');
  const ctx = new MockAudioContext();
  ctx._currentTime = 0.0;

  const scheduler = createAudioScheduler(ctx, {
    jitterBufferSec: 0.1,
    comfortNoiseEnabled: false,
  });

  var timings = [];
  for (var i = 0; i < 5; i++) {
    var buf = ctx.createBuffer(1, 12000, 24000);
    buf.duration = 0.5;
    timings.push(scheduler.scheduleBuffer(buf));
  }

  // First starts at now + jitter
  assertApprox(timings[0].startTime, 0.1, 0.01, 'First buffer at now + jitter');

  // Each subsequent buffer should seamlessly follow
  for (var j = 1; j < 5; j++) {
    assertApprox(
      timings[j].startTime,
      timings[j - 1].endTime,
      0.001,
      'Buffer ' + j + ' starts exactly when buffer ' + (j - 1) + ' ends'
    );
  }

  // Total scheduled time: 0.1 + 5 * 0.5 = 2.6
  assertApprox(timings[4].endTime, 2.6, 0.01, 'Total scheduled end time is correct');
})();

// ── Summary ──────────────────────────────────────────────────────────────

console.log('\n=== Results: ' + passed + ' passed, ' + failed + ' failed ===\n');
process.exit(failed > 0 ? 1 : 0);
