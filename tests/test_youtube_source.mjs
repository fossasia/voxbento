import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// The "From a YouTube stream" guide (#1087, plan 007) against a small fake DOM that can move nodes.
const source = readFileSync(new URL('../portal/static/js/youtube-source.js', import.meta.url), 'utf8')
  .replace(/^export .*$/m, '');

class FakeNode {
  constructor(name, { matches = [], ...props } = {}) {
    this.name = name;
    this.matches = new Set(matches);
    this.children = [];
    this.parentNode = null;
    this.hidden = false;
    this.dataset = {};
    this.handlers = {};
    Object.assign(this, props);
  }

  detach() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    siblings.splice(siblings.indexOf(this), 1);
    this.parentNode = null;
  }

  appendChild(child) {
    child.detach();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  insertBefore(child, reference) {
    child.detach();
    child.parentNode = this;
    const index = reference ? this.children.indexOf(reference) : this.children.length;
    this.children.splice(index, 0, child);
    return child;
  }

  get nextSibling() {
    const siblings = this.parentNode.children;
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  *descendants() {
    for (const child of this.children) {
      yield child;
      yield* child.descendants();
    }
  }

  querySelectorAll(selector) { return [...this.descendants()].filter((node) => node.matches.has(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, handler) { (this.handlers[type] ||= []).push(handler); }
  dispatchEvent(event) { (this.handlers[event.type] || []).forEach((handler) => handler(event)); }
}

function radio(name, value, extra = {}) {
  return new FakeNode(`${name}:${value}`, {
    matches: [`input[name="${name}"]`, `input[name="${name}"][value="${value}"]`],
    value, checked: false, disabled: false, ...extra,
  });
}

// Like a browser radio group: checking one unchecks the others.
function exclusive(group) {
  group.forEach((input) => {
    let checked = input.checked;
    Object.defineProperty(input, 'checked', {
      get: () => checked,
      set: (value) => {
        if (value) group.forEach((other) => { if (other !== input) other.checked = false; });
        checked = value;
      },
    });
  });
  return group;
}

function page({ choice = 'youtube', initialRoute = '', relayDisabled = false } = {}) {
  const root = new FakeNode('root');
  const choices = exclusive(['youtube', 'meeting', 'encoder', 'desk'].map((value) => radio('audio_choice', value, { checked: value === choice })));
  choices.forEach((input) => root.appendChild(input));

  const guide = root.appendChild(new FakeNode('youtube-source'));
  guide.dataset.initialRoute = initialRoute;
  const connectSlot = guide.appendChild(new FakeNode('connect-slot', { matches: ['[data-youtube-account-slot]'] }));
  const routes = exclusive(['multi', 'forward', 'tab'].map((value) => guide.appendChild(radio('youtube-route', value))));
  const notes = Object.fromEntries(['multi', 'forward'].map((value) => [value,
    guide.appendChild(new FakeNode(`note-${value}`, { matches: [`[data-route-unavailable="${value}"]`], hidden: true }))]));
  const panels = Object.fromEntries(['', 'multi forward', 'tab'].map((value) => {
    const panel = guide.appendChild(new FakeNode(`panel-${value}`, { matches: ['[data-route-panel]'] }));
    panel.dataset.routePanel = value;
    return [value, panel];
  }));
  const ingestSlot = panels['multi forward'].appendChild(new FakeNode('ingest-slot', { matches: ['[data-youtube-ingest-slot]'] }));
  const tabBlocked = guide.appendChild(new FakeNode('tab-blocked', { matches: ['[data-tab-blocked]'], hidden: true }));

  // The encoder panel: the streaming-software card, with the YouTube block inside it.
  const encoderPanel = root.appendChild(new FakeNode('encoder-panel'));
  const ingest = encoderPanel.appendChild(new FakeNode('program-ingest'));
  const picker = ingest.appendChild(new FakeNode('picker', { matches: ['.program-mode-picker'] }));
  const modes = Object.fromEntries(['whip', 'rtmps', 'rtmps_relay'].map((value) => [value,
    picker.appendChild(radio('program-mode', value, { checked: value === 'whip', disabled: value === 'rtmps_relay' && relayDisabled }))]));
  exclusive(Object.values(modes));
  const setup = ingest.appendChild(new FakeNode('setup'));
  const connect = setup.appendChild(new FakeNode('youtube-connect'));
  setup.appendChild(new FakeNode('destination-form'));

  const byId = { 'youtube-source': guide, 'program-ingest': ingest, 'youtube-connect': connect };
  const context = vm.createContext({
    document: {
      getElementById: (id) => byId[id] || null,
      querySelectorAll: (selector) => root.querySelectorAll(selector),
      createComment: (text) => new FakeNode(`<!--${text}-->`),
    },
    Event: class { constructor(type) { this.type = type; } },
  });
  vm.runInContext(source, context);
  // What program-ingest.js dispatches after each status check.
  const status = (detail) => ingest.dispatchEvent({ type: 'program-ingest:status', detail });
  return { choices, routes, notes, panels, connectSlot, ingestSlot, tabBlocked, encoderPanel, ingest, picker, modes, setup, connect, status };
}

const choose = (input) => { input.checked = true; input.dispatchEvent({ type: 'change' }); };

// Opening the guide moves the existing controls in; the card's own mode picker is hidden there.
{
  const p = page({ initialRoute: 'forward' });
  assert.equal(p.connect.parentNode, p.connectSlot);
  assert.equal(p.ingest.parentNode, p.ingestSlot);
  assert.equal(p.picker.hidden, true);
  assert.equal(p.routes[1].checked, true);
  assert.equal(p.panels['multi forward'].hidden, false);
  assert.equal(p.panels.tab.hidden, true);
  assert.equal(p.panels[''].hidden, true);
  // The card shows the rendered mode before its first status check, without a change event.
  assert.equal(p.modes.rtmps_relay.checked, true);
}

// Until the owner picks a route, the route follows the room's saved source from each status check.
{
  const p = page();
  p.status({ enabled: true, mode: 'rtmps_relay' });
  assert.equal(p.routes[1].checked, true);
  assert.equal(p.panels['multi forward'].hidden, false);
  // Another tab switched the room to an RTMPS copy.
  p.status({ enabled: true, mode: 'rtmps' });
  assert.equal(p.routes[0].checked, true);
  // Once a route is picked here, it stays.
  choose(p.routes[2]);
  p.status({ enabled: true, mode: 'rtmps_relay' });
  assert.equal(p.routes[2].checked, true);
}

// The tab route on a room still taking an RTMPS stream shows the card to stop it, with the reason.
{
  const p = page({ initialRoute: 'multi' });
  p.status({ enabled: true, mode: 'rtmps' });
  choose(p.routes[2]);
  assert.equal(p.panels.tab.hidden, false);
  assert.equal(p.panels['multi forward'].hidden, false);
  assert.equal(p.tabBlocked.hidden, false);
  // "Switch to the online meeting" in the card: the sender can start, the card goes away.
  p.status({ enabled: false, mode: 'rtmps' });
  assert.equal(p.panels['multi forward'].hidden, true);
  assert.equal(p.tabBlocked.hidden, true);
  // Other routes never show the note.
  p.status({ enabled: true, mode: 'rtmps' });
  choose(p.routes[0]);
  assert.equal(p.tabBlocked.hidden, true);
}

// A route picks the matching mode in the card, the same as clicking it there; the tab route changes none.
{
  const p = page();
  assert.equal(p.panels[''].hidden, false);
  let changes = 0;
  p.modes.rtmps.addEventListener('change', () => { changes += 1; });
  p.routes[0].checked = true;
  p.routes[0].dispatchEvent({ type: 'change' });
  assert.equal(p.modes.rtmps.checked, true);
  assert.equal(changes, 1);
  p.routes[2].checked = true;
  p.routes[2].dispatchEvent({ type: 'change' });
  assert.equal(p.panels.tab.hidden, false);
  assert.equal(p.panels['multi forward'].hidden, true);
  assert.equal(p.modes.rtmps.checked, true);
  assert.equal(changes, 1);
}

// Choosing another way of hearing the room puts both blocks back exactly where they were.
{
  const p = page({ initialRoute: 'multi' });
  p.choices[0].checked = false;
  choose(p.choices[2]);
  assert.equal(p.ingest.parentNode, p.encoderPanel);
  assert.equal(p.connect.parentNode, p.setup);
  assert.equal(p.setup.children.indexOf(p.connect), 1); // after its marker, before the destination form
  assert.equal(p.setup.children[2].name, 'destination-form');
  assert.equal(p.picker.hidden, false);
  // And back again.
  p.choices[2].checked = false;
  choose(p.choices[0]);
  assert.equal(p.ingest.parentNode, p.ingestSlot);
}

// Another choice on load leaves the controls where they are.
{
  const p = page({ choice: 'encoder' });
  assert.equal(p.ingest.parentNode, p.encoderPanel);
  assert.equal(p.picker.hidden, false);
}

// A route the server cannot offer is shown with the operator hint and cannot be picked.
{
  const p = page({ relayDisabled: true });
  assert.equal(p.routes[1].disabled, true);
  assert.equal(p.notes.forward.hidden, false);
  assert.equal(p.routes[0].disabled, false);
  assert.equal(p.notes.multi.hidden, true);
}

console.log('youtube-source.js: ok');
