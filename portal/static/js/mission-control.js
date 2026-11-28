/**
 * Mission Control — live booth matrix for one event
 *
 * Connects to each booth's WebSocket as a silent observer (no booth:join).
 * Coordinators, event owners and Admins can:
 *   - See every booth of the rooms they run, grouped by room
 *   - See who is in each booth, who is live and who is speaking
 *   - Listen in through a per-booth volume slider (WHEP)
 *   - Turn a booth's broadcast on or off
 *   - Start and stop the floor captions of each room
 *   - Choose the booth each room relays from, live (#1382)
 *   - Browse room reports and generate notes for a session
 *   - Open a booth themselves
 *
 * The page passes its data in data-config on #mission-control (no inline script).
 */

const root = document.getElementById('mission-control');
const config = JSON.parse(root?.dataset.config || '{}');
const roomsContainer = document.getElementById('rooms-container');

/** @type {Map<string, {state:object, ws:WebSocket|null, whep:object|null, audioElement:HTMLAudioElement|null, currentVolume:number, pendingFocusId:string}>} */
const boothMap = new Map();

/** Per room: the relay selector and the room's relay booth ID (null for none). */
const roomRelays = new Map();

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
    const controls = el('div', 'mc-room-controls');
    controls.appendChild(renderRelayControl(room));
    controls.appendChild(renderFloorCaptions(room));
    head.appendChild(controls);
    section.appendChild(head);
    if (room.reports) section.appendChild(renderReports(room.reports));

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
    });
    renderCard(b.booth_id);
    connectWs(b.booth_id);
  });
  roomRelays.forEach((_, roomId) => renderRelayOptions(roomId));
}

// ---------------------------------------------------------------------------
// Relay booth (one per room) — interpreters who don't understand the speaker
// listen to it instead of the floor. Open booths switch at once (#1382).
// ---------------------------------------------------------------------------

function renderRelayControl(room) {
  const box = el('div', 'mc-relay');
  const label = el('label', 'mc-relay-label', 'Relay from');
  const select = document.createElement('select');
  select.id = `mc-relay-${room.id}`;
  label.htmlFor = select.id;
  box.append(label, select);
  roomRelays.set(room.id, { select, relayBoothId: room.relay_booth_id || null, saving: false });
  select.addEventListener('change', () => chooseRelay(room.id, select.value || null));
  return box;
}

/** The room's booths, with an on-air mark, and the current relay selected. */
function renderRelayOptions(roomId) {
  const relay = roomRelays.get(roomId);
  if (!relay) return;
  const { select } = relay;
  const roomBooths = [...boothMap.values()].map((entry) => entry.state).filter((s) => s.room_id === roomId);
  const options = [{ value: '', text: 'No relay' }].concat(
    roomBooths.map((s) => ({
      value: s.booth_id,
      text: `${s.language}${s.ingest_status === 'connected' ? ' (on air)' : ' (off air)'}`,
    })),
  );
  // The room page can pick a booth of another room; show it rather than pretending there is no relay.
  if (relay.relayBoothId && !roomBooths.some((s) => s.booth_id === relay.relayBoothId)) {
    const other = boothMap.get(relay.relayBoothId);
    options.push({ value: relay.relayBoothId, text: `${other ? other.state.language : relay.relayBoothId} (another room)` });
  }
  // Rebuild only when something changed, so an open dropdown is not reset by every booth update.
  const current = [...select.options].map((o) => `${o.value}|${o.textContent}`).join('\n');
  const wanted = options.map((o) => `${o.value}|${o.text}`).join('\n');
  if (current !== wanted) {
    select.innerHTML = '';
    options.forEach((o) => select.appendChild(new Option(o.text, o.value)));
  }
  select.value = relay.relayBoothId || '';
  select.disabled = relay.saving || roomBooths.length < 2;
  select.title = roomBooths.length < 2 ? 'A relay needs at least two booths in the room.' : '';
}

async function chooseRelay(roomId, boothId) {
  const relay = roomRelays.get(roomId);
  if (!relay) return;
  const entry = boothId ? boothMap.get(boothId) : null;
  relay.saving = true;
  renderRelayOptions(roomId);
  try {
    const res = await fetch(`/api/rooms/${roomId}/relay`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || '',
      },
      body: JSON.stringify({ language_code: entry ? entry.state.language_code : null }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      showToast(body.detail || "Couldn't change the relay.", 'error');
    } else {
      relay.relayBoothId = body.relay_booth_id || null;
      showToast(entry ? `Booths now relay from ${entry.state.language}.` : 'Relay turned off.');
    }
  } catch (err) {
    showToast("Couldn't change the relay.", 'error');
    console.debug('Relay request failed', err);
  } finally {
    relay.saving = false;
    renderRelayOptions(roomId);
  }
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

function renderReports(reports) {
  const panel = document.createElement('iframe');
  panel.title = 'Meeting reports';
  panel.loading = 'lazy';
  panel.className = 'room-reports-frame';
  panel.src = reports.url + '?panel=1';
  return panel;
}

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
    renderCard(boothId);
  };

  ws.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    if (data.type === 'booth:state') {
      entry.state = { ...data.state, room_name: data.state.room_name ?? entry.state.room_name };
      renderCard(boothId);
      renderRelayOptions(entry.state.room_id);
    } else if (data.type === 'booth:relay') {
      // Someone (here or elsewhere) changed this booth's room relay.
      const relay = roomRelays.get(entry.state.room_id);
      if (relay && !relay.saving) {
        relay.relayBoothId = data.relay_booth_id || null;
        renderRelayOptions(entry.state.room_id);
      }
    }
    // Silently ignore booth:joined — we do not join as a participant.
  };

  ws.onclose = () => {
    entry.ws = null;
    renderCard(boothId);
    setTimeout(() => connectWs(boothId), 3000);
  };

  ws.onerror = () => ws.close();
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
      if (p.connected === false) {
        // Their connection dropped; the server keeps their place while they reconnect.
        row.appendChild(el('span', 'mc-pill is-off', 'Reconnecting…'));
      } else {
        row.appendChild(el('span', `mc-pill ${p.mic_active ? 'is-on' : 'is-off'}`, p.mic_active ? 'Speaking' : 'Muted'));
      }
      list.appendChild(row);
    });
    people.appendChild(list);
  } else {
    people.appendChild(el('p', 'mc-muted', 'Nobody is in this booth.'));
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
  btn.textContent = !isWsOpen ? 'Connecting…' : isLive ? 'Stop broadcast' : 'Go live';
  btn.disabled = !isWsOpen;
  const open = el('a', 'btn btn-outline', 'Open booth');
  open.href = `/interpreter/${encodeURIComponent(config.eventSlug)}/${s.room_id}/${encodeURIComponent(s.language_code)}`;
  open.id = `mc-open-${boothId}`;
  actions.append(btn, open);
  card.appendChild(actions);

  card.appendChild(entry.audioElement);

  volInput.addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    volValue.textContent = `${val}%`;
    entry.currentVolume = val;
  });
  volInput.addEventListener('change', (e) => handleVolumeChange(boothId, e.target.value));
  btn.addEventListener('click', () => setBroadcastLive(boothId, !isLive));

  const focusTarget = focusedId ? document.getElementById(focusedId) : null;
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
