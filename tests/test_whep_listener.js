/**
 * Unit tests for the WHEP client's reported state (portal/static/js/whep-listener.js).
 *
 * Run with: node tests/test_whep_listener.js
 *
 * Runs the real script with a stubbed RTCPeerConnection and a fetch that
 * fails, so the retry flag can be checked without a media server.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

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

let lastPc = null;
class FakePeerConnection {
  constructor() {
    lastPc = this;
    this.listeners = {};
    this.connectionState = 'new';
    this.iceConnectionState = 'new';
    this.signalingState = 'stable';
    this.iceGatheringState = 'complete';
    this.localDescription = null;
  }
  addTransceiver() {}
  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }
  removeEventListener() {}
  async createOffer() {
    return { type: 'offer', sdp: 'v=0' };
  }
  async setLocalDescription(desc) {
    this.localDescription = desc;
  }
  async setRemoteDescription() {}
  close() {
    this.connectionState = 'closed';
  }
}

let failFetch = true;
const context = vm.createContext({
  RTCPeerConnection: FakePeerConnection,
  AbortController,
  fetch: () => failFetch
    ? Promise.reject(new Error('WHEP endpoint unreachable'))
    : Promise.resolve({ ok: true, headers: { get: () => null }, text: async () => 'v=0' }),
  setTimeout,
  clearTimeout,
  console,
});
const source = fs.readFileSync(path.join(__dirname, '..', 'portal', 'static', 'js', 'whep-listener.js'), 'utf8');
const WhepListener = vm.runInContext(source + '\nWhepListener;', context);

function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function run() {
  console.log('\n── Retry flag ──');

  const states = [];
  let onRecovery = () => {};
  const onState = (s) => {
    states.push(s);
    onRecovery(s);
  };
  WhepListener.start({ whepUrl: 'http://mtx/floor/whep', audioEl: null, onState });
  await flush();
  await flush();

  const last = states[states.length - 1];
  assert(last && last.retrying === true, 'a failed WHEP request reports retrying');

  failFetch = false;
  const recovered = new Promise((resolve) => {
    const deadline = setTimeout(() => resolve(null), 1000);
    onRecovery = (s) => {
      if (s.retrying === false) {
        clearTimeout(deadline);
        resolve(s);
      }
    };
  });
  assert(await recovered, 'a successful retry clears retrying');

  failFetch = true;
  WhepListener.start({ whepUrl: 'http://mtx/floor/whep', audioEl: null, onState: (s) => states.push(s) });
  await flush();
  await flush();
  assert(states[states.length - 1].retrying === true, 'a new failed session reports retrying before stop');

  WhepListener.stop();
  const stopped = states[states.length - 1];
  assert(stopped.peerConnection === 'closed', 'stop reports a closed connection');
  assert(stopped.retrying === false, 'stop clears retrying, so a stopped client is not shown as reconnecting');

  console.log('\n── Blocked autoplay ──');

  let allowPlay = false;
  const audioEl = {
    paused: true,
    srcObject: null,
    muted: false,
    play() {
      if (!allowPlay) {
        const err = new Error('play() needs a user gesture');
        err.name = 'NotAllowedError';
        return Promise.reject(err);
      }
      this.paused = false;
      return Promise.resolve();
    },
  };
  const playStates = [];
  failFetch = false;
  WhepListener.start({ whepUrl: 'http://mtx/floor/whep', audioEl, onState: (s) => playStates.push(s) });
  await flush();
  lastPc.listeners.track({ track: { kind: 'audio' }, streams: [{ id: 'remote' }] });
  await flush();
  assert(playStates[playStates.length - 1].playbackBlocked === true, 'a blocked play() is reported as playbackBlocked');

  allowPlay = true;
  WhepListener.resume();
  await flush();
  const resumed = playStates[playStates.length - 1];
  assert(resumed.playbackBlocked === false && resumed.audioActive === true, 'resume() after a tap starts playback');

  allowPlay = false;
  audioEl.paused = true;
  lastPc.listeners.track({ track: { kind: 'audio' }, streams: [{ id: 'remote' }] });
  await flush();
  WhepListener.stop();
  assert(playStates[playStates.length - 1].playbackBlocked === false, 'stop clears playbackBlocked');

  console.log('\n── Moving playback to another element (#1397) ──');

  function fakeAudio() {
    return {
      paused: true,
      srcObject: null,
      play() {
        this.paused = false;
        return Promise.resolve();
      },
      pause() {
        this.paused = true;
      },
    };
  }
  const standby = fakeAudio();
  const player = fakeAudio();
  const moveStates = [];
  const client = vm.runInContext('createWhepClient()', context);
  client.start({ whepUrl: 'http://mtx/th/whep', audioEl: standby, onState: (s) => moveStates.push(s) });
  client.setAudioElement(standby);
  assert(standby.srcObject === null && moveStates.length === 0, 'moving to the same element is a no-op');
  client.setAudioElement(player);
  assert(player.srcObject === null, 'moving before the stream arrives attaches nothing yet');
  client.setAudioElement(standby);
  await flush();
  const remote = { id: 'booth' };
  lastPc.listeners.track({ track: { kind: 'audio' }, streams: [remote] });
  await flush();
  assert(standby.srcObject === remote && standby.paused === false, 'the stream first plays on the standby element');

  client.setAudioElement(player, true);
  await flush();
  assert(standby.srcObject === null && standby.paused === true, 'the standby element is emptied and paused');
  assert(player.srcObject === remote && player.paused === false, 'the same stream plays on the player, no new session');
  assert(moveStates[moveStates.length - 1].audioActive === true, 'the move reports the player as active');

  const other = fakeAudio();
  client.setAudioElement(other, false);
  await flush();
  assert(other.srcObject === remote && other.paused === true, 'autoplay false keeps the new element paused');
  assert(player.srcObject === null, 'the old player no longer holds the stream');
  client.stop();

  // A switch can time out before the new stream has its track; the listener's pause must still hold.
  const earlyPlayer = fakeAudio();
  const early = vm.runInContext('createWhepClient()', context);
  early.start({ whepUrl: 'http://mtx/th/whep', audioEl: fakeAudio() });
  await flush();
  early.setAudioElement(earlyPlayer, false);
  lastPc.listeners.track({ track: { kind: 'audio' }, streams: [{ id: 'late' }] });
  await flush();
  assert(earlyPlayer.srcObject.id === 'late' && earlyPlayer.paused === true, 'a paused move before the track keeps the player paused');
  early.stop();

  // With a listener delay the stream reaches the element through a Web Audio delay line.
  class FakeAudioContext {
    constructor() {
      this.state = 'running';
    }
    resume() {
      return Promise.resolve();
    }
    close() {
      return Promise.resolve();
    }
    createMediaStreamSource() {
      return { connect() {}, disconnect() {} };
    }
    createDelay() {
      return { delayTime: {}, connect() {}, disconnect() {} };
    }
    createMediaStreamDestination() {
      return { stream: { id: 'delayed', getTracks: () => [] } };
    }
  }
  const delayed = vm.createContext({
    ...context,
    window: { AudioContext: FakeAudioContext },
    document: { createElement: fakeAudio, addEventListener() {}, removeEventListener() {} },
  });
  vm.runInContext(source, delayed);
  const delayedClient = vm.runInContext('createWhepClient()', delayed);
  const delayedStandby = fakeAudio();
  const delayedPlayer = fakeAudio();
  delayedClient.start({ whepUrl: 'http://mtx/th/whep', audioEl: delayedStandby, audioDelayMs: 500 });
  await flush();
  lastPc.listeners.track({ track: { kind: 'audio' }, streams: [remote] });
  await flush();
  const filled = delayedStandby.srcObject;
  delayedClient.setAudioElement(delayedPlayer, true);
  await flush();
  assert(
    delayedPlayer.srcObject === filled && delayedStandby.srcObject === null,
    'a delayed stream moves to the player with its filled delay line, not a new empty one',
  );
  delayedClient.stop();

  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed > 0 ? 1 : 0);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
