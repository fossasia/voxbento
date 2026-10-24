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

class FakePeerConnection {
  constructor() {
    this.connectionState = 'new';
    this.iceConnectionState = 'new';
    this.signalingState = 'stable';
    this.iceGatheringState = 'complete';
    this.localDescription = null;
  }
  addTransceiver() {}
  addEventListener() {}
  removeEventListener() {}
  async createOffer() {
    return { type: 'offer', sdp: 'v=0' };
  }
  async setLocalDescription(desc) {
    this.localDescription = desc;
  }
  close() {
    this.connectionState = 'closed';
  }
}

const context = vm.createContext({
  RTCPeerConnection: FakePeerConnection,
  fetch: () => Promise.reject(new Error('WHEP endpoint unreachable')),
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
  WhepListener.start({ whepUrl: 'http://mtx/floor/whep', audioEl: null, onState: (s) => states.push(s) });
  await flush();
  await flush();

  const last = states[states.length - 1];
  assert(last && last.retrying === true, 'a failed WHEP request reports retrying');

  WhepListener.stop();
  const stopped = states[states.length - 1];
  assert(stopped.peerConnection === 'closed', 'stop reports a closed connection');
  assert(stopped.retrying === false, 'stop clears retrying, so a stopped client is not shown as reconnecting');

  console.log('\n' + passed + ' passed, ' + failed + ' failed');
  process.exit(failed > 0 ? 1 : 0);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
