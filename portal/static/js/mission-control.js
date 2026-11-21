/**
 * Mission Control — live booth matrix for one event
 *
 * Connects to each booth's WebSocket as a silent observer (no booth:join).
 * Coordinators, event owners and Admins can:
 *   - See every booth of the rooms they run, grouped by room
 *   - See who is in each booth, who is live and who is speaking
 *   - Choose who is live, without joining the booth (#1338)
 *   - Listen in through a per-booth volume slider (WHEP)
 *   - Turn a booth's broadcast on or off
 *   - Start and stop the floor captions of each room
 *   - Open a booth themselves
 *
 * The page passes its data in data-config on #mission-control (no inline script).
 */

import { createBackoff, finalCloseMessage } from './booth-reconnect.js';
import { confirmChange } from './confirm-dialog.js';

const root = document.getElementById('mission-control');
const config = JSON.parse(root?.dataset.config || '{}');
const roomsContainer = document.getElementById('rooms-container');

// What a coordinator can do about a close that retrying won't fix.
const MC_CLOSE_MESSAGES = new Map([
  [4001, 'Your session has expired. Reload the page to sign in again.'],
  [4003, 'You no longer have access to this booth.'],
]);

const HANDOFF_LABELS = {
  offered: 'Handover offered',
  requested: 'Handover requested',
};

/**
 * @typedef {object} BoothEntry
 * @property {object} state
 * @property {WebSocket|null} ws
 * @property {object|null} whep
 * @property {HTMLAudioElement|null} audioElement
 * @property {number} currentVolume
 * @property {string} pendingFocusId
 * @property {{next: () => number, reset: () => void}} backoff
 * @property {string} closedMessage  Why the socket stopped for good; empty while it may reconnect.
 * @property {string} switchingTo  Participant ID we asked to make live; empty when no request is pending.
 */

/** @type {Map<string, BoothEntry>} */
const boothMap = new Map();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function init() {
  const rooms = config.rooms || [];
  const booths = config.booths || [];
  roomsContainer.innerHTML = '';

  if (rooms.length === 0 && booths.length === 0) {
    const empty = el('div', 'mc-empty-state');
    empty.append(
      el('p', 'mc-empty-title', 'No rooms or booths yet'),
      el('p', '', "Once the event's owners add rooms and booths, they appear here."),
    );
    roomsContainer.appendChild(empty);
    return;
  }

  // Rooms in the order the server sent them; a booth whose room is unknown still gets a section.
  const roomOrder = rooms.map((room) => room.id);
  booths.forEach((b) => {
    if (!roomOrder.includes(b.room_id)) {
      roomOrder.push(b.room_id);
      rooms.push({ id: b.room_id, name: b.room_name || `Room ${b.room_id}`, floor_captions: false });
    }
  });

  rooms.forEach((room) => {
    const section = el('section', 'mc-room');
    section.setAttribute('aria-labelledby', `mc-room-title-${room.id}`);

    const head = el('div', 'mc-room-head');
    const title = el('h2', 'mc-room-title', room.name);
    title.id = `mc-room-title-${room.id}`;
    head.appendChild(title);
    head.appendChild(renderFloorCaptions(room));
    section.appendChild(head);

    const grid = el('div', 'mc-grid');
    grid.id = `booth-grid-${room.id}`;
    if (!booths.some((b) => b.room_id === room.id)) {
      grid.appendChild(el('p', 'mc-muted', 'No booths in this room yet.'));
    }
    section.appendChild(grid);
    roomsContainer.appendChild(section);
  });

  booths.forEach((b) => {
    boothMap.set(b.booth_id, {
      state: b,
      ws: null,
      whep: null,
      audioElement: null,
      currentVolume: 0,
      pendingFocusId: '',
      backoff: createBackoff(),
      closedMessage: '',
      switchingTo: '',
    });
    renderCard(b.booth_id);
    connectWs(b.booth_id);
  });
}

// ---------------------------------------------------------------------------
// Floor captions (one per room) — uses the coordinator floor-transcription API
// ---------------------------------------------------------------------------

const CAPTION_STATES = {
  stopped: ['Off', 'is-off'],
  starting: ['Starting…', 'is-busy'],
  joined: ['Joining audio…', 'is-busy'],
  receiving: ['On', 'is-on'],
  reconnecting: ['Reconnecting…', 'is-busy'],
  degraded: ['Needs attention', 'is-problem'],
};

function renderFloorCaptions(room) {
  const box = el('div', 'mc-captions');
  if (!room.floor_captions) {
    box.appendChild(el('span', 'mc-muted', 'Floor captions are off for this room.'));
    return box;
  }
  const label = el('span', 'mc-captions-label', 'Floor captions');
  const status = el('span', 'mc-pill', 'Checking…');
  box.append(label, status);

  if (room.floor_source === 'program_ingest') {
    status.textContent = 'From the program feed';
    status.title = 'Captions start automatically when the program feed sends audio.';
    return box;
  }

  const start = el('button', 'btn btn-sm btn-outline', 'Start');
  start.type = 'button';
  start.id = `mc-captions-start-${room.id}`;
  const stop = el('button', 'btn btn-sm btn-outline', 'Stop');
  stop.type = 'button';
  stop.id = `mc-captions-stop-${room.id}`;
  const reason = el('span', 'mc-captions-reason');
  box.append(start, stop, reason);

  async function refresh() {
    try {
      const res = await fetch(`/api/rooms/${room.id}/floor-transcription/status`);
      if (!res.ok) throw new Error(String(res.status));
      const data = await res.json();
      const [text, tone] = CAPTION_STATES[data.state] || [String(data.state || 'Unknown').replace(/_/g, ' '), 'is-busy'];
      status.textContent = text;
      status.className = `mc-pill ${tone}`;
      reason.textContent = data.reason || '';
      start.hidden = data.enabled;
      stop.hidden = !data.enabled;
    } catch (err) {
      status.textContent = 'Unavailable';
      status.className = 'mc-pill is-problem';
      console.debug('Floor caption status failed', err);
    }
  }

  async function send(action) {
    start.disabled = true;
    stop.disabled = true;
    try {
      const res = await fetch(`/api/rooms/${room.id}/floor-transcription/${action}`, {
        method: 'POST',
        headers: { 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || '' },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        showToast(body.detail || `Couldn't ${action} the floor captions.`, 'error');
      }
    } catch (err) {
      showToast(`Couldn't ${action} the floor captions.`, 'error');
      console.debug('Floor caption request failed', err);
    } finally {
      start.disabled = false;
      stop.disabled = false;
      refresh();
    }
  }

  start.addEventListener('click', () => send('start'));
  stop.addEventListener('click', () => send('stop'));
  refresh();
  setInterval(refresh, 15000);
  return box;
}

// ---------------------------------------------------------------------------
// WebSocket — silent observer mode (no booth:join participant registration)
// ---------------------------------------------------------------------------

function connectWs(boothId) {
  const entry = boothMap.get(boothId);
  if (!entry) return;

  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${window.location.host}/ws/booth/${boothId}`;
  const ws = new WebSocket(wsUrl);
  entry.ws = ws;

  ws.onopen = () => {
    entry.backoff.reset();
    renderCard(boothId);
  };

  ws.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    if (data.type === 'booth:state') {
      entry.state = { ...data.state, room_name: data.state.room_name ?? entry.state.room_name };
      entry.switchingTo = '';
      renderCard(boothId);
    } else if (data.type === 'booth:error') {
      showToast(data.message || 'The booth refused that change.', 'error');
      if (entry.switchingTo) {
        entry.switchingTo = '';
        renderCard(boothId);
      }
    }
    // Silently ignore booth:joined — we do not join as a participant.
  };

  ws.onclose = (e) => {
    entry.ws = null;
    entry.switchingTo = '';
    if (finalCloseMessage(e.code)) {
      // Retrying a refused session would only be refused again, every few seconds.
      entry.closedMessage = MC_CLOSE_MESSAGES.get(e.code) || finalCloseMessage(e.code);
      renderCard(boothId);
      return;
    }
    renderCard(boothId);
    // Jittered back-off, so every card does not reconnect at once after a portal restart.
    setTimeout(() => connectWs(boothId), entry.backoff.next());
  };

  ws.onerror = () => ws.close();
}

// ---------------------------------------------------------------------------
// Choose who is live — booth:set-active as an observer (#1338)
// ---------------------------------------------------------------------------

async function makeLive(boothId, targetId) {
  const entry = boothMap.get(boothId);
  if (!entry || entry.switchingTo) return;
  // What the coordinator saw when they clicked. If someone else changes who is
  // live while the dialog is open, the server refuses instead of overriding them.
  const seen = entry.state;
  const expectedActiveId = seen.active_interpreter_id || null;
  const participants = seen.participants || [];
  const target = participants.find((p) => p.participant_id === targetId);
  const current = participants.find((p) => p.participant_id === expectedActiveId);
  if (!target) return;

  if (current && (current.mic_active || current.ingest_connected || seen.ingest_status === 'connected')) {
    const ok = await confirmChange(
      `${current.display_name} is on air in ${seen.language}. Make ${target.display_name} live instead? `
      + `${current.display_name} goes off air straight away.`,
    );
    if (!ok) return;
  }

  if (!entry.ws || entry.ws.readyState !== WebSocket.OPEN) {
    showToast("Can't reach this booth right now. Try again in a moment.", 'error');
    return;
  }
  entry.switchingTo = targetId;
  entry.ws.send(JSON.stringify({
    type: 'booth:set-active',
    target_id: targetId,
    expected_active_id: expectedActiveId,
  }));
  renderCard(boothId);
}

// ---------------------------------------------------------------------------
// Go Live / Stop — broadcast-unlock toggle without joining as participant
// ---------------------------------------------------------------------------

function setBroadcastLive(boothId, goLive) {
  const entry = boothMap.get(boothId);
  if (!entry || !entry.ws || entry.ws.readyState !== WebSocket.OPEN) {
    showToast("Can't reach this booth right now. Try again in a moment.", 'error');
    return;
  }
  entry.ws.send(JSON.stringify({
    type: 'booth:set-broadcast-unlocked',
    unlocked: goLive,
  }));
}

// ---------------------------------------------------------------------------
// Audio monitor — lazy WHEP start/stop
// ---------------------------------------------------------------------------

function handleVolumeChange(boothId, volume) {
  const entry = boothMap.get(boothId);
  if (!entry) return;
  const vol = parseInt(volume, 10);

  if (vol > 0 && !entry.whep) {
    const whepUrl = `${config.whipBase}/${entry.state.mediamtx_path}/whep`;
    const whep = window.createWhepClient();
    whep.start({ whepUrl, audioEl: entry.audioElement });
    entry.whep = whep;
  } else if (vol === 0 && entry.whep) {
    entry.whep.stop();
    entry.whep = null;
  }

  if (entry.audioElement) {
    entry.audioElement.volume = vol / 100;
  }
}

// ---------------------------------------------------------------------------
// Card rendering
// ---------------------------------------------------------------------------

function renderCard(boothId) {
  const entry = boothMap.get(boothId);
  if (!entry) return;
  const s = entry.state;

  let card = document.getElementById(`mc-card-${boothId}`);
  if (!card) {
    card = el('article', 'card mc-booth');
    card.id = `mc-card-${boothId}`;

    const audio = document.createElement('audio');
    audio.autoplay = true;
    audio.hidden = true;
    entry.audioElement = audio;
    card.appendChild(audio);

    const roomGrid = document.getElementById(`booth-grid-${s.room_id}`);
    (roomGrid || roomsContainer).appendChild(card);
  }

  // Everyone who can go live, as decided by the server (portal/roles.py).
  const interpreters = (s.participants || []).filter((p) => p.can_go_live);
  const isWsOpen = entry.ws && entry.ws.readyState === WebSocket.OPEN;
  const isLive = s.broadcast_unlocked;
  const onAir = s.ingest_status === 'connected';

  // Keep keyboard focus across re-renders (#873).
  const active = document.activeElement;
  let focusedId = '';
  if (card.contains(active)) {
    focusedId = active.id;
  } else if (active === document.body) {
    focusedId = entry.pendingFocusId;
  }
  entry.pendingFocusId = '';

  card.innerHTML = '';
  card.classList.toggle('is-on-air', onAir);

  // Header: language and on-air state
  const head = el('div', 'mc-booth-head');
  const title = el('h3', 'mc-booth-title', s.language);
  title.appendChild(el('span', 'mc-booth-code', s.language_code));
  head.appendChild(title);
  head.appendChild(el('span', `mc-pill ${onAir ? 'is-on' : 'is-off'}`, onAir ? '● On air' : 'Not on air'));
  card.appendChild(head);

  // Interpreters
  const people = el('div', 'mc-booth-section');
  people.appendChild(el('p', 'mc-booth-label', 'Interpreters'));
  if (interpreters.length) {
    const list = el('ul', 'mc-people');
    interpreters.forEach((p) => {
      const isActive = p.participant_id === s.active_interpreter_id;
      const row = el('li', `mc-person${isActive ? ' is-active' : ''}`);
      const name = el('span', 'mc-person-name', p.display_name);
      if (isActive) name.appendChild(el('span', 'mc-person-tag', 'live'));
      row.appendChild(name);
      const side = el('span', 'mc-person-side');
      if (p.connected === false) {
        // Their connection dropped; the server keeps their place while they reconnect.
        side.appendChild(el('span', 'mc-pill is-off', 'Reconnecting…'));
      } else {
        side.appendChild(el('span', `mc-pill ${p.mic_active ? 'is-on' : 'is-off'}`, p.mic_active ? 'Speaking' : 'Muted'));
        if (!isActive) {
          const switching = entry.switchingTo === p.participant_id;
          const make = el('button', 'btn btn-sm btn-outline mc-make-live', switching ? 'Switching…' : 'Make live');
          make.type = 'button';
          make.id = `mc-make-live-${boothId}-${p.participant_id}`;
          make.setAttribute('aria-label', `Make ${p.display_name} live in ${s.language}`);
          make.disabled = !isWsOpen || Boolean(entry.switchingTo);
          make.addEventListener('click', () => makeLive(boothId, p.participant_id));
          side.appendChild(make);
        }
      }
      row.appendChild(side);
      list.appendChild(row);
    });
    people.appendChild(list);
  } else {
    people.appendChild(el('p', 'mc-muted', 'Nobody is in this booth.'));
  }
  const handoff = HANDOFF_LABELS[s.handoff_state];
  if (handoff) {
    const initiator = (s.participants || []).find((p) => p.participant_id === s.handoff_initiator_id);
    const pill = el('span', 'mc-pill is-busy', initiator ? `${handoff} by ${initiator.display_name}` : handoff);
    pill.title = 'The interpreters are passing the microphone. Make live overrides it.';
    people.appendChild(pill);
  }
  card.appendChild(people);

  // Listen in
  const listen = el('div', 'mc-booth-section');
  const listenHead = el('div', 'mc-listen-head');
  const volLabel = el('label', 'mc-booth-label', 'Listen in');
  volLabel.htmlFor = `mc-vol-${boothId}`;
  const volValue = el('span', 'mc-muted', `${entry.currentVolume > 0 ? entry.currentVolume : 0}%`);
  volValue.id = `mc-vol-lbl-${boothId}`;
  listenHead.append(volLabel, volValue);
  const volInput = document.createElement('input');
  volInput.type = 'range';
  volInput.id = `mc-vol-${boothId}`;
  volInput.className = 'mc-volume';
  volInput.min = '0';
  volInput.max = '100';
  volInput.value = String(entry.currentVolume > 0 ? entry.currentVolume : 0);
  listen.append(listenHead, volInput);
  card.appendChild(listen);

  // Actions: broadcast toggle and open the booth
  const actions = el('div', 'mc-booth-actions');
  const btn = el('button', `btn ${!isWsOpen ? 'btn-outline' : isLive ? 'btn-danger' : 'btn-success'}`);
  btn.type = 'button';
  btn.id = `mc-live-${boothId}`;
  btn.textContent = entry.closedMessage ? 'Unavailable' : !isWsOpen ? 'Connecting…' : isLive ? 'Stop broadcast' : 'Go live';
  btn.disabled = !isWsOpen;
  const open = el('a', 'btn btn-outline', 'Open booth');
  open.href = `/interpreter/${encodeURIComponent(config.eventSlug)}/${s.room_id}/${encodeURIComponent(s.language_code)}`;
  open.id = `mc-open-${boothId}`;
  actions.append(btn, open);
  if (entry.closedMessage) {
    const why = el('p', 'mc-booth-closed', entry.closedMessage);
    why.setAttribute('role', 'status');
    actions.appendChild(why);
  }
  card.appendChild(actions);

  card.appendChild(entry.audioElement);

  volInput.addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    volValue.textContent = `${val}%`;
    entry.currentVolume = val;
  });
  volInput.addEventListener('change', (e) => handleVolumeChange(boothId, e.target.value));
  btn.addEventListener('click', () => setBroadcastLive(boothId, !isLive));

  let focusTarget = focusedId ? document.getElementById(focusedId) : null;
  if (!focusTarget && focusedId.startsWith(`mc-make-live-${boothId}-`)) {
    // That interpreter is live now, so their Make live button is gone.
    focusTarget = document.getElementById(`mc-live-${boothId}`);
  }
  if (focusTarget && !focusTarget.disabled) {
    focusTarget.focus({ preventScroll: true });
  } else if (focusTarget) {
    entry.pendingFocusId = focusedId;
  }
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function showToast(msg, type = 'info') {
  if (typeof window.showToast === 'function') {
    window.showToast(msg, type);
    return;
  }
  const t = el('div', `mc-toast${type === 'error' ? ' is-error' : ''}`, msg);
  t.setAttribute('role', 'status');
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

document.addEventListener('focusin', (e) => {
  boothMap.forEach((entry, boothId) => {
    const card = document.getElementById(`mc-card-${boothId}`);
    if (!card || !card.contains(e.target)) {
      entry.pendingFocusId = '';
    }
  });
});

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
