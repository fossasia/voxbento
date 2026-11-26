/**
 * Unit tests for the listener's source choice (portal/static/js/channel-source.js, #1397).
 *
 * Run with: node tests/test_channel_source.mjs
 *
 * Drives the real controller with fake WHEP clients, a fake clock and hand-run
 * timers, so the handover guard and the make-before-break switch can be checked
 * without a browser or media server.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

// channel-source.js is a classic script that declares one global.
const FILE = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'portal', 'static', 'js', 'channel-source.js');
const ChannelSource = vm.runInThisContext(fs.readFileSync(FILE, 'utf8') + '\n;ChannelSource', { filename: FILE });
const { HANDOVER_GUARD_MS, SWITCH_TIMEOUT_MS, chooseSource } = ChannelSource;
const createChannelSource = ChannelSource.create;

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

// ── Fake clock and timers ─────────────────────────────────────────────────

let clock = 0;
const timers = new Set();
globalThis.setTimeout = (fn, ms) => {
  const timer = { fn, at: clock + (ms || 0) };
  timers.add(timer);
  return timer;
};
globalThis.clearTimeout = (timer) => {
  timers.delete(timer);
};
function advance(ms) {
  const end = clock + ms;
  for (;;) {
    const due = [...timers].filter((t) => t.at <= end).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    timers.delete(due);
    clock = Math.max(clock, due.at);
    due.fn();
  }
  clock = end;
}

// ── Fake WHEP clients and audio elements ──────────────────────────────────

function fakeAudio(name) {
  return { name, paused: true, srcObject: null, muted: false };
}

let clients = [];
function createClient() {
  const client = {
    started: null,
    stopped: false,
    moves: [],
    delays: [],
    resumed: 0,
    start(opts) {
      this.started = opts;
      this.audioEl = opts.audioEl;
    },
    stop() {
      this.stopped = true;
      this.started.onState({ peerConnection: 'closed', audioActive: false });
    },
    setAudioElement(el, autoplay) {
      this.moves.push({ el, autoplay });
      this.audioEl = el;
      this.started.onState({ peerConnection: 'connected', audioActive: autoplay });
    },
    setAudioDelayMs(ms) {
      this.delays.push(ms);
    },
    resume() {
      this.resumed++;
    },
    // What the real client reports once its stream plays.
    live() {
      this.started.onState({ peerConnection: 'connected', audioActive: true });
    },
  };
  clients.push(client);
  return client;
}

function setup(audioDelayMs = 0) {
  clients = [];
  timers.clear();
  clock = 1000;
  const audioEl = fakeAudio('player');
  const standbyEl = fakeAudio('standby');
  const states = [];
  const source = createChannelSource({
    interpretationUrl: 'http://mtx/demo/3/th/whep',
    floorUrl: 'http://mtx/demo/3/floor/whep',
    audioEl,
    standbyEl,
    audioDelayMs,
    createClient,
    onState: (state, kind) => states.push({ state, kind }),
    now: () => clock,
  });
  return { source, audioEl, standbyEl, states };
}

const running = () => clients.filter((c) => !c.stopped);
const onPlayer = (audioEl) => running().filter((c) => c.audioEl === audioEl);
const kindOf = (client) => (client.started.whepUrl.includes('/floor/') ? 'floor' : 'interpretation');

console.log('\n── Decision ──');
assert(chooseSource({ current: 'floor', onAir: true, offAirForMs: 0 }) === 'interpretation', 'on air plays the interpretation');
assert(chooseSource({ current: null, onAir: false, offAirForMs: HANDOVER_GUARD_MS }) === 'floor', 'off air for the guard plays the floor');
assert(chooseSource({ current: 'interpretation', onAir: false, offAirForMs: HANDOVER_GUARD_MS - 1 }) === 'interpretation', 'a short drop keeps the interpretation');
assert(chooseSource({ current: null, onAir: false, offAirForMs: 0 }) === null, 'nothing plays before the guard runs out');
assert(HANDOVER_GUARD_MS === 3000 && SWITCH_TIMEOUT_MS === 5000, 'guard is 3 s and the switch timeout 5 s');

console.log('\n── Booth never live, floor live ──');
{
  const { source, audioEl, standbyEl } = setup();
  assert(standbyEl.muted === true, 'the standby element is always muted');
  advance(HANDOVER_GUARD_MS - 1);
  assert(clients.length === 0 && source.source() === null, 'nothing plays before 3 s');
  advance(1);
  assert(clients.length === 1 && kindOf(clients[0]) === 'floor', 'the floor starts after 3 s off air');
  assert(clients[0].audioEl === audioEl, 'with nothing playing the floor goes straight to the player');
  assert(source.source() === 'floor', 'the source is the floor');
  assert(timers.size === 0, 'no timer is left once the floor plays');
}

console.log('\n── Booth live before the guard runs out ──');
{
  const { source, audioEl } = setup();
  advance(1000);
  source.setOnAir(true);
  assert(clients.length === 1 && kindOf(clients[0]) === 'interpretation', 'the interpretation starts at once');
  assert(clients[0].audioEl === audioEl && timers.size === 0, 'it plays on the player and the guard is cancelled');
  source.setOnAir(true);
  advance(10000);
  assert(clients.length === 1, 'a repeated on air (mic mute, cough key) changes nothing');
}

console.log('\n── Booth goes live while the floor plays ──');
{
  const { source, audioEl, standbyEl, states } = setup();
  advance(HANDOVER_GUARD_MS);
  const floor = clients[0];
  floor.live();
  source.setOnAir(true);
  const booth = clients[1];
  assert(kindOf(booth) === 'interpretation' && booth.audioEl === standbyEl, 'the booth stream warms up on the muted standby');
  assert(!floor.stopped && onPlayer(audioEl).length === 1, 'the floor keeps playing until the booth has audio');
  booth.started.onState({ peerConnection: 'connected', audioActive: false });
  assert(!floor.stopped, 'a connected booth stream without audio does not take over');
  states.length = 0;
  booth.live();
  assert(floor.stopped, 'the floor stops once the booth has audio');
  assert(booth.moves.length === 1 && booth.moves[0].el === audioEl, 'the booth stream moves to the player without a new session');
  assert(onPlayer(audioEl).length === 1 && onPlayer(audioEl)[0] === booth, 'only one stream is ever on the player');
  assert(states.every((s) => s.kind === 'interpretation'), 'the player state now comes from the interpretation');
  assert(source.source() === 'interpretation' && timers.size === 0, 'no switch timer is left');
}

console.log('\n── Make-before-break timeout ──');
{
  const { source, audioEl } = setup();
  advance(HANDOVER_GUARD_MS);
  const floor = clients[0];
  source.setOnAir(true);
  const booth = clients[1];
  advance(SWITCH_TIMEOUT_MS - 1);
  assert(!floor.stopped, 'the floor is kept while the booth stream is not ready');
  advance(1);
  assert(floor.stopped && booth.audioEl === audioEl, 'after 5 s the floor stops anyway and the booth takes the player');
}

console.log('\n── Handover under 3 s ──');
{
  const { source } = setup();
  source.setOnAir(true);
  clients[0].live();
  source.setOnAir(false);
  advance(HANDOVER_GUARD_MS - 100);
  source.setOnAir(true);
  advance(10000);
  assert(clients.length === 1 && !clients[0].stopped, 'a handover drop shorter than 3 s never switches');
}

console.log('\n── Off air for 3 s, then the floor fails ──');
{
  const { source, audioEl, standbyEl } = setup();
  source.setOnAir(true);
  const booth = clients[0];
  booth.live();
  source.setOnAir(false);
  source.setOnAir(false);
  advance(HANDOVER_GUARD_MS - 1);
  assert(clients.length === 1, 'repeated off-air updates do not restart or shorten the guard');
  advance(1);
  const floor = clients[1];
  assert(kindOf(floor) === 'floor' && floor.audioEl === standbyEl, 'the floor warms up on the standby');
  floor.started.onState({ peerConnection: 'new', audioActive: false, retrying: true });
  advance(SWITCH_TIMEOUT_MS);
  assert(booth.stopped && floor.audioEl === audioEl, 'a floor that never plays still replaces the silent booth after 5 s');
  assert(source.source() === 'floor', 'the floor keeps retrying on its own');
}

console.log('\n── Organizer Stop, then unlock ──');
{
  const { source, audioEl } = setup();
  source.setOnAir(true);
  clients[0].live();
  source.setOnAir(false); // broadcast_unlocked false makes on_air false
  advance(HANDOVER_GUARD_MS);
  clients[1].live();
  assert(source.source() === 'floor' && clients[0].stopped, 'a locked booth plays the floor');
  source.setOnAir(true);
  clients[2].live();
  assert(source.source() === 'interpretation' && clients[1].stopped, 'unlocking goes back to the interpretation');
  assert(onPlayer(audioEl).length === 1, 'one stream on the player');
}

console.log('\n── Flapping ──');
{
  const { source, audioEl } = setup();
  advance(HANDOVER_GUARD_MS);
  clients[0].live();
  for (let i = 0; i < 10; i++) {
    source.setOnAir(i % 2 === 0);
    advance(500);
  }
  assert(clients.length <= 2, '10 toggles in 5 s start at most one extra stream, got ' + clients.length);
  advance(HANDOVER_GUARD_MS + SWITCH_TIMEOUT_MS);
  assert(source.source() === 'floor', 'the last state (off air) wins');
  assert(onPlayer(audioEl).length === 1 && running().length === 1, 'exactly one stream runs afterwards');
  source.setOnAir(true);
  advance(SWITCH_TIMEOUT_MS);
  assert(source.source() === 'interpretation' && running().length === 1, 'on air as the last state wins too');
}

console.log('\n── Back off air while the booth stream warms up ──');
{
  const { source } = setup();
  advance(HANDOVER_GUARD_MS);
  clients[0].live();
  source.setOnAir(true);
  source.setOnAir(false);
  advance(HANDOVER_GUARD_MS);
  assert(clients[1].stopped && !clients[0].stopped, 'the pending booth stream is dropped and the floor stays');
  assert(source.source() === 'floor' && timers.size === 0, 'nothing is left pending');
}

console.log('\n── Pause, delay and resume ──');
{
  const { source, audioEl } = setup(750);
  advance(HANDOVER_GUARD_MS);
  const floor = clients[0];
  assert(floor.started.audioDelayMs === 750, 'the floor uses the room delay');
  floor.live();
  audioEl.srcObject = { id: 'floor' };
  audioEl.paused = true;
  source.setAudioDelayMs(0);
  source.setOnAir(true);
  const booth = clients[1];
  assert(booth.started.audioDelayMs === 0, 'a delay change applies to the next stream');
  source.setAudioDelayMs(250);
  assert(floor.delays.at(-1) === 250 && booth.delays.at(-1) === 250, 'a delay change reaches both streams');
  booth.live();
  advance(250);
  assert(booth.moves[0].autoplay === false, 'a listener pause carries across the switch');
  source.resume();
  assert(booth.resumed === 1 && floor.resumed === 0, 'tap to listen resumes the stream on the player');
}
{
  const { source, audioEl } = setup();
  advance(HANDOVER_GUARD_MS);
  source.setOnAir(true);
  clients[1].live();
  assert(clients[1].moves[0].autoplay === true, 'a player that never loaded a stream counts as playing');
  assert(audioEl.paused === true, 'the controller never touches the player itself');
}

console.log('\n── Listener delay: the old stream plays until the new one fills its delay ──');
{
  const { source, audioEl } = setup(3000);
  advance(HANDOVER_GUARD_MS);
  const floor = clients[0];
  floor.live();
  source.setOnAir(true);
  const booth = clients[1];
  advance(1000);
  booth.live();
  advance(1000);
  booth.live();
  advance(1999);
  assert(!floor.stopped && booth.moves.length === 0, 'the floor keeps playing while the booth fills its 3 s delay');
  advance(1);
  assert(floor.stopped && booth.moves.length === 1 && booth.moves[0].el === audioEl, 'after 3 s of booth audio the booth takes the player');
  assert(timers.size === 0, 'repeated audio reports leave no extra timer');
}
{
  const { source } = setup(3000);
  advance(HANDOVER_GUARD_MS);
  const floor = clients[0];
  source.setOnAir(true);
  advance(SWITCH_TIMEOUT_MS + 3000 - 1);
  assert(!floor.stopped, 'the switch timeout grows by the listener delay');
  advance(1);
  assert(floor.stopped && source.source() === 'interpretation', 'and still ends the switch');
}
{
  const { source } = setup(3000);
  advance(HANDOVER_GUARD_MS);
  source.setOnAir(true);
  clients[1].live();
  source.stop();
  assert(timers.size === 0 && running().length === 0, 'stop while the delay fills leaves nothing behind');
}

console.log('\n── Teardown ──');
{
  const { source } = setup();
  advance(1000);
  source.stop();
  advance(10000);
  assert(clients.length === 0 && timers.size === 0, 'stop during the guard cancels it');
  source.setOnAir(true);
  source.setAudioDelayMs(100);
  source.resume();
  assert(clients.length === 0, 'a stopped source ignores later updates');
}
{
  const { source } = setup();
  advance(HANDOVER_GUARD_MS);
  source.setOnAir(true);
  source.stop();
  assert(running().length === 0 && timers.size === 0, 'stop during a switch stops both streams and the timeout');
  assert(source.source() === null, 'nothing plays after stop');
}

console.log('\n' + passed + ' passed, ' + failed + ' failed');
process.exit(failed > 0 ? 1 : 0);
