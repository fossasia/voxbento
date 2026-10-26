'use strict';

// Run with: node --test tests/test_whep_lifecycle.js
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../portal/static/js/whep-listener.js'), 'utf8');
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function response(location = '/sessions/1', body = Promise.resolve('answer')) {
  return { ok: true, status: 201, headers: { get: () => location }, text: () => body };
}
function harness(options = {}) {
  const peers = [], requests = [], states = [], timers = new Map(), logs = [];
  let timerId = 0;
  class Peer {
    constructor() {
      this.connectionState = 'new';
      this.iceConnectionState = 'new';
      this.signalingState = 'stable';
      this.iceGatheringState = 'complete';
      this.listeners = new Map();
      this.answers = [];
      peers.push(this);
    }
    addTransceiver() {}
    addEventListener(type, handler) { this.listeners.set(type, handler); }
    removeEventListener(type) { this.listeners.delete(type); }
    dispatch(type, event = {}) { this.listeners.get(type)?.(event); }
    async createOffer() { return this.offer ? this.offer.promise : { type: 'offer', sdp: 'offer' }; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription(value) { this.answers.push(value); }
    close() { this.connectionState = 'closed'; }
  }
  const audio = { srcObject: null, muted: false, paused: true, play() { this.paused = false; return Promise.resolve(); } };
  const context = vm.createContext({
    RTCPeerConnection: Peer, URL, AbortController,
    fetch(url, options) {
      if (options.method === 'DELETE') { requests.push({ url, options }); return Promise.resolve({ ok: true }); }
      const pending = deferred();
      requests.push({ url, options, ...pending });
      // Deliberately allow settlement after abort to exercise already delivered responses.
      return pending.promise;
    },
    setTimeout(fn, ms) { const id = ++timerId; fn.timeoutMs = ms; timers.set(id, fn); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  const client = vm.runInContext(source + '\ncreateWhepClient();', context);
  const start = (url = 'https://media.test/a/whep', audioEl = audio) => client.start({
    whepUrl: url, audioEl, audioDelayMs: options.delay || 0, onState: (s) => states.push(s), onLog: (s) => logs.push(s),
  });
  const posts = () => requests.filter((r) => r.options.method === 'POST');
  const deletes = () => requests.filter((r) => r.options.method === 'DELETE').map((r) => r.url);
  const runTimers = (maxDelay = 8000) => {
    const pending = [...timers.entries()].filter(([, fn]) => fn.timeoutMs <= maxDelay);
    pending.forEach(([id, fn]) => { timers.delete(id); fn(); });
  };
  return { client, context, start, audio, peers, requests, posts, deletes, states, timers, logs, runTimers };
}

test('a pending request failure after Stop cannot retry', async () => {
  const h = harness(); h.start(); await flush();
  const request = h.posts()[0]; h.client.stop();
  request.reject(new Error('network lost')); await flush();
  assert.equal(h.states.at(-1).retrying, false);
  assert.equal(h.timers.size, 0);
  h.runTimers(); await flush();
  assert.equal(h.posts().length, 1);
  assert.equal(request.options.signal.aborted, true);
});

test('a late success after Stop releases its WHEP resource without applying the answer', async () => {
  const h = harness(); h.start(); await flush();
  h.client.stop(); h.posts()[0].resolve(response('/sessions/late')); await flush();
  assert.equal(h.peers[0].answers.length, 0);
  assert.deepEqual(h.deletes(), ['https://media.test/sessions/late']);
  assert.equal(h.states.at(-1).peerConnection, 'closed');
});

test('an old answer body settling after a new source starts cannot change the new peer', async () => {
  const h = harness(), body = deferred(); h.start(); await flush();
  h.posts()[0].resolve(response('old-session', body.promise)); await flush();
  h.start('https://other.test/b/whep'); await flush();
  h.posts()[1].resolve(response('/sessions/new')); await flush();
  body.resolve('old answer'); await flush();
  assert.equal(h.peers[0].answers.length, 0);
  assert.equal(h.peers[1].answers.length, 1);
  assert.equal(h.peers[1].answers[0].sdp, 'answer');
  assert.deepEqual(h.deletes(), ['https://media.test/a/old-session']);
  h.client.stop();
  assert.deepEqual(h.deletes(), ['https://media.test/a/old-session', 'https://other.test/sessions/new']);
});

test('failure of a replaced source cannot schedule retries or notify its replacement', async () => {
  const h = harness(); h.start(); await flush();
  h.start('https://other.test/b/whep'); await flush();
  const timersBefore = h.timers.size;
  const statesBefore = h.states.length;
  const logsBefore = [...h.logs];
  h.posts()[0].reject(new Error('old failure')); await flush();
  assert.equal(h.timers.size, timersBefore);
  assert.equal(h.states.length, statesBefore);
  assert.deepEqual(h.logs, logsBefore);
  assert.equal(h.posts()[1].options.signal.aborted, false);
  h.client.stop();
});

test('late peer events cannot attach old audio or reconnect', async () => {
  const h = harness(); h.start(); await flush();
  const old = h.peers[0]; h.start('https://media.test/b/whep'); await flush();
  const timersBefore = h.timers.size;
  old.dispatch('track', { track: { kind: 'audio' }, streams: [{ id: 'old' }] });
  old.connectionState = 'failed'; old.dispatch('connectionstatechange'); old.dispatch('iceconnectionstatechange');
  assert.equal(h.audio.srcObject, null);
  assert.equal(h.timers.size, timersBefore);
  h.client.stop();
});

test('Stop during offer creation prevents any WHEP POST', async () => {
  const h = harness(), offer = deferred();
  // Set the asynchronous offer before the negotiation continuation runs.
  const Peer = h.context.RTCPeerConnection;
  Peer.prototype.createOffer = () => offer.promise;
  h.start(); h.client.stop();
  offer.resolve({ type: 'offer', sdp: 'late' }); await flush();
  assert.equal(h.posts().length, 0);
  assert.equal(h.timers.size, 0);
});

test('current failures retry once and a successful attempt resets the reported state', async () => {
  const h = harness(); h.start(); await flush();
  h.posts()[0].reject(new Error('offline')); await flush();
  h.peers[0].connectionState = 'failed'; h.peers[0].dispatch('connectionstatechange');
  assert.equal(h.timers.size, 1);
  assert.equal(h.states.at(-1).retrying, true);
  h.runTimers(); await flush();
  h.posts()[1].resolve(response()); await flush();
  assert.equal(h.states.at(-1).retrying, false);
  assert.equal(h.peers[1].answers.length, 1);
  h.client.stop(); h.client.stop();
  assert.deepEqual(h.deletes(), ['https://media.test/sessions/1']);
});

test('Start detaches the previous audio element as well as its peer', async () => {
  const h = harness(); h.start(); await flush();
  h.peers[0].dispatch('track', { track: { kind: 'audio' }, streams: [{ id: 'a' }] });
  assert.notEqual(h.audio.srcObject, null);
  const nextAudio = { ...h.audio, srcObject: null };
  h.start('https://media.test/b/whep', nextAudio); await flush();
  assert.equal(h.audio.srcObject, null);
  h.client.stop();
});

test('Stop during ICE gathering removes its timeout and listener', async () => {
  const h = harness();
  const Peer = h.context.RTCPeerConnection;
  const original = Peer.prototype.setLocalDescription;
  Peer.prototype.setLocalDescription = async function (value) {
    this.iceGatheringState = 'gathering';
    await original.call(this, value);
  };
  h.start(); await flush();
  assert.equal(h.timers.size, 1);
  h.client.stop(); await flush();
  assert.equal(h.timers.size, 0);
  assert.equal(h.peers[0].listeners.has('icegatheringstatechange'), false);
  assert.equal(h.posts().length, 0);
});

test('an invalid answer is reported and retried, with its session released on teardown', async () => {
  const h = harness();
  h.context.RTCPeerConnection.prototype.setRemoteDescription = async () => { throw new Error('invalid SDP'); };
  h.start(); await flush();
  h.posts()[0].resolve(response()); await flush();
  assert.equal(h.states.at(-1).retrying, true);
  assert.match(h.logs.at(-2), /invalid SDP/);
  h.client.stop();
  assert.deepEqual(h.deletes(), ['https://media.test/sessions/1']);
  assert.equal(h.timers.size, 0);
});

function audioHarness(options = {}) {
  const h = harness({ delay: 1000 });
  const contexts = [], nodes = [], listeners = new Map(), sinks = [];
  class Node {
    constructor(kind) { this.kind = kind; this.connections = []; nodes.push(this); }
    connect(target) { this.connections.push(target); }
    disconnect() { this.connections = []; }
  }
  class AudioContext {
    constructor() {
      this.state = options.suspended ? 'suspended' : 'running';
      this.destination = new Node('speakers');
      contexts.push(this);
    }
    resume() { return options.suspended ? Promise.resolve() : Promise.resolve().then(() => { this.state = 'running'; }); }
    close() { this.state = 'closed'; return Promise.resolve(); }
    createMediaStreamSource(stream) { const node = new Node('source'); node.input = stream; return node; }
    createDelay() { const node = new Node('delay'); node.delayTime = { value: 0 }; return node; }
    createMediaStreamDestination() {
      const node = new Node('output');
      const track = { stopped: false, stop() { this.stopped = true; } };
      node.stream = { id: 'delayed', getTracks: () => [track] };
      return node;
    }
  }
  h.context.window = options.unavailable ? {} : { AudioContext };
  h.context.document = {
    createElement() {
      const sink = { srcObject: null, muted: false, paused: true, play() { this.paused = false; return Promise.resolve(); }, pause() { this.paused = true; } };
      sinks.push(sink);
      return sink;
    },
    addEventListener(type, handler) { listeners.set(type, handler); },
    removeEventListener(type) { listeners.delete(type); },
  };
  const inputTrack = { stopped: false, stop() { this.stopped = true; } };
  const stream = { id: 'original', getTracks: () => [inputTrack] };
  const track = () => h.peers.at(-1).dispatch('track', { track: { kind: 'audio' }, streams: [stream] });
  return { ...h, contexts, nodes, listeners, sinks, stream, inputTrack, track };
}

test('delayed audio reaches only the controlled media element, never a separate speaker output', async () => {
  const h = audioHarness(); h.start(); await flush(); h.track();
  const output = h.nodes.find((n) => n.kind === 'output');
  assert.ok(output, 'the delay needs a media-stream output');
  assert.equal(h.audio.srcObject, output.stream);
  assert.equal(h.audio.muted, false);
  const delay = h.nodes.find((n) => n.kind === 'delay');
  assert.deepEqual(delay.connections, [output]);
  assert.equal(delay.delayTime.value, 1);
  assert.equal(h.nodes.some((n) => n.connections.includes(h.contexts[0].destination)), false);
  h.client.stop();
  assert.equal(output.stream.getTracks()[0].stopped, true);
  assert.equal(h.inputTrack.stopped, false);
});

test('changing delay preserves pause, mute and volume including the return to direct playback', async () => {
  const h = audioHarness(); h.start(); await flush(); h.track();
  h.audio.paused = true; h.audio.muted = true; h.audio.volume = 0.25;
  h.client.setAudioDelayMs(2000);
  assert.equal(h.audio.paused, true);
  assert.equal(h.audio.muted, true);
  assert.equal(h.audio.volume, 0.25);
  assert.equal(h.states.at(-1).audioActive, false);
  h.client.setAudioDelayMs(0);
  assert.equal(h.audio.srcObject, h.stream);
  assert.equal(h.audio.paused, true);
  assert.equal(h.audio.muted, true);
  assert.equal(h.audio.volume, 0.25);
  h.client.stop();
});

test('a suspended delay context is not reported as audible playback', async () => {
  const h = audioHarness({ suspended: true }); h.start(); await flush(); h.track();
  assert.equal(h.audio.paused, false);
  assert.equal(h.states.at(-1).audioActive, false);
  h.client.stop();
  assert.equal(h.listeners.size, 0);
});

test('without Web Audio, delayed playback falls back to the controlled original stream', async () => {
  const h = audioHarness({ unavailable: true }); h.start(); await flush(); h.track();
  assert.equal(h.audio.srcObject, h.stream);
  assert.equal(h.audio.muted, false);
  assert.equal(h.states.at(-1).audioActive, true);
  h.client.stop();
});

test('network recovery preserves TTS overlay muting', async () => {
  const h = audioHarness(); h.start(); await flush(); h.track();
  h.audio.muted = true;
  h.peers[0].connectionState = 'failed'; h.peers[0].dispatch('connectionstatechange');
  h.runTimers(); await flush(); h.track();
  assert.equal(h.audio.muted, true);
  h.client.stop();
});

test('network recovery does not resume audio the listener paused', async () => {
  const h = audioHarness(); h.start(); await flush(); h.track();
  h.audio.paused = true;
  h.peers[0].connectionState = 'failed'; h.peers[0].dispatch('connectionstatechange');
  h.runTimers(); await flush(); h.track();
  assert.equal(h.audio.paused, true);
  assert.equal(h.states.at(-1).audioActive, false);
  h.client.stop();
});

// Chromium needs a playing remote-stream sink to keep delivering samples to Web Audio.
test('delayed playback keeps an inaudible receiver sink alive and releases it on Stop', async () => {
  const h = audioHarness(); h.start(); await flush(); h.track();
  const sink = h.sinks[0];
  assert.ok(sink, 'a muted sink must keep the incoming WebRTC stream rendering');
  assert.equal(sink.srcObject, h.stream);
  assert.equal(sink.muted, true);
  assert.equal(sink.paused, false);
  h.client.stop();
  assert.equal(sink.srcObject, null);
  assert.equal(sink.paused, true);
});

test('session cleanup rejects foreign origins, unsafe schemes and malformed URLs', async () => {
  for (const location of ['https://portal.test/api/events/demo', 'http://media.test/session', 'data:text/plain,session', 'https://[', '//foreign.test/session', 'https://user:password@media.test/session']) {
    const h = harness(); h.start(); await flush();
    h.posts()[0].resolve(response(location)); await flush();
    h.client.stop();
    assert.deepEqual(h.deletes(), [], location);
  }
});

test('valid cleanup omits cookies and refuses redirects', async () => {
  const h = harness(); h.start(); await flush();
  h.posts()[0].resolve(response('/sessions/valid')); await flush(); h.client.stop();
  assert.deepEqual(h.deletes(), ['https://media.test/sessions/valid']);
  const cleanup = h.requests.find((r) => r.options.method === 'DELETE');
  assert.equal(cleanup.options.credentials, 'omit');
  assert.equal(cleanup.options.redirect, 'error');
});

test('a hung WHEP request is aborted and retried, while Stop cancels its deadline', async () => {
  const h = harness(); h.start(); await flush();
  const request = h.posts()[0];
  h.runTimers(15000); await flush();
  assert.equal(request.options.signal.aborted, true);
  assert.equal(h.states.at(-1).retrying, true);
  h.runTimers(); await flush();
  assert.equal(h.posts().length, 2);
  h.client.stop();
  assert.equal(h.timers.size, 0);
});

test('a hung answer body also times out and a late answer cannot establish playback', async () => {
  const h = harness(), body = deferred(); h.start(); await flush();
  h.posts()[0].resolve(response('/sessions/hung', body.promise)); await flush();
  h.runTimers(15000); await flush();
  body.resolve('late answer'); await flush();
  assert.equal(h.peers[0].answers.length, 0);
  assert.equal(h.states.at(-1).retrying, true);
  h.runTimers(); await flush();
  assert.deepEqual(h.deletes(), ['https://media.test/sessions/hung']);
  h.client.stop();
});

test('Pause during the reconnect gap stays paused when the replacement track arrives', async () => {
  const h = audioHarness(); h.start(); await flush(); h.track();
  h.peers[0].connectionState = 'failed'; h.peers[0].dispatch('connectionstatechange');
  h.runTimers(); await flush();
  h.audio.paused = true;
  h.track();
  assert.equal(h.audio.paused, true);
  assert.equal(h.states.at(-1).audioActive, false);
  h.client.stop();
});

test('later AudioContext suspension reinstalls gesture recovery and Stop removes it', async () => {
  const h = audioHarness(); h.start(); await flush(); h.track(); await flush();
  const ctx = h.contexts[0];
  ctx.state = 'suspended'; ctx.onstatechange?.();
  assert.equal(h.states.at(-1).audioActive, false);
  assert.ok(h.listeners.has('click'));
  h.listeners.get('click')(); await flush();
  assert.equal(ctx.state, 'running');
  assert.equal(h.states.at(-1).audioActive, true);
  assert.equal(h.listeners.size, 0);
  ctx.state = 'suspended'; ctx.onstatechange?.();
  h.client.stop();
  assert.equal(ctx.onstatechange, null);
  assert.equal(h.listeners.size, 0);
});
