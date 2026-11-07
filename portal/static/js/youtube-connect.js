// "Sign in with YouTube" on the room page (#1087): connect the event's channel and pick the
// room's broadcast. URLs come from data attributes on #youtube-connect (no inline script).
// The stream key never reaches the browser: the server fills in the relay destination itself.

const box = document.getElementById('youtube-connect');
const message = document.getElementById('youtube-message');
const channel = document.getElementById('youtube-channel');
const form = document.getElementById('youtube-broadcast-form');
const select = document.getElementById('youtube-broadcast-select');
const link = document.getElementById('youtube-broadcast-link');
const statusLine = document.getElementById('youtube-status-line');
const unlink = document.getElementById('youtube-unlink');
const signInAgain = document.getElementById('youtube-signin-again');
let connected = false;
let linkedId = null;
let pending = false;
let relayKeyFilled = false;

const lifeCycleLabels = {
  created: 'Created',
  ready: 'Ready',
  testStarting: 'Starting preview',
  testing: 'Preview',
  liveStarting: 'Starting',
  live: 'Live',
  complete: 'Ended',
  revoked: 'Removed',
  missing: 'Not found on the connected channel',
};

const streamLabels = {active: 'receiving', inactive: 'no data', ready: 'ready', created: 'not started', error: 'error'};

// Where Google sent the owner back: shown once, then dropped from the address bar.
const outcomes = {
  connected: ['YouTube is connected. Choose this room\'s broadcast below.', 'success'],
  cancelled: ['YouTube sign-in was cancelled. Nothing was changed.', 'info'],
  error: ['YouTube sign-in did not finish. Try again, and allow access on Google\'s screen.', 'error'],
  'no-channel': ['That Google account has no YouTube channel. Sign in with the account that owns the channel.', 'error'],
};

// Messages use the shared alert styles, so a refused link stands out from the status notes.
function say(text, tone = 'info') {
  message.textContent = text || '';
  message.className = `alert alert-${tone}`;
  message.hidden = !text;
}

async function readJson(response) {
  let data;
  try {
    data = await response.json();
  } catch (error) {
    // A proxy may return HTML on failure; preserve the HTTP status below.
    if (response.ok) throw error;
  }
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : `Request failed (HTTP ${response.status})`;
    const error = new Error(detail);
    // Set when Google needs a new sign-in; the room's broadcast links are kept meanwhile.
    error.lastError = data?.last_error;
    throw error;
  }
  return data;
}

function offerSignInAgain(error) {
  if (error.lastError) signInAgain.hidden = false;
}

function request(url, options = {}) {
  return fetch(url, {cache: 'no-store', credentials: 'same-origin', ...options});
}

function showState(state) {
  box.querySelectorAll('[data-youtube-state]').forEach(panel => {
    panel.hidden = panel.dataset.youtubeState !== state;
  });
}

function broadcastLabel(broadcast) {
  const status = lifeCycleLabels[broadcast.life_cycle_status] || broadcast.life_cycle_status;
  return `${broadcast.title || broadcast.id} (${status})`;
}

async function loadBroadcasts() {
  try {
    const data = await readJson(await request(box.dataset.broadcastsUrl));
    const options = data.broadcasts.map(broadcast => new Option(broadcastLabel(broadcast), broadcast.id));
    const placeholder = new Option(options.length ? 'Choose a broadcast' : 'No live or upcoming broadcasts', '');
    select.replaceChildren(placeholder, ...options);
    if (linkedId) select.value = linkedId;
  } catch (error) {
    console.error('YouTube broadcasts unavailable', error);
    select.replaceChildren(new Option('Broadcasts unavailable', ''));
    say(error.message, 'error');
    offerSignInAgain(error);
  }
}

function renderRoomStatus(youtube) {
  linkedId = youtube && !youtube.error ? youtube.broadcast_id : null;
  unlink.hidden = !youtube;
  if (!youtube) {
    statusLine.textContent = 'No broadcast chosen for this room yet.';
    return;
  }
  if (youtube.error) {
    statusLine.textContent = youtube.error;
    return;
  }
  const parts = [`${youtube.title || youtube.broadcast_id}: ${lifeCycleLabels[youtube.life_cycle_status] || youtube.life_cycle_status}`];
  if (youtube.stream_status) parts.push(`stream ${streamLabels[youtube.stream_status] || youtube.stream_status}`);
  if (youtube.health) parts.push(`health ${youtube.health}`);
  statusLine.textContent = parts.join(' · ');
  if (select.querySelector(`option[value="${CSS.escape(linkedId)}"]`)) select.value = linkedId;
}

// The server fills in the stream key only for a room already in relay mode. When relay mode is
// switched on after the broadcast was linked, link it once more so the key is filled in then.
async function fillRelayKey(data) {
  const youtube = data.youtube;
  if (relayKeyFilled || pending || !youtube || youtube.error || !data.enabled) return;
  if (data.mode !== 'rtmps_relay' || data.destination?.configured) return;
  relayKeyFilled = true;
  try {
    const result = await readJson(await request(box.dataset.broadcastUrl, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({broadcast: youtube.broadcast_id}),
    }));
    if (result.destination_configured) {
      say(`Relay mode is on: VoxBento filled in the stream key for "${result.title}".`, 'success');
    }
  } catch (error) {
    console.error('YouTube stream key could not be filled in', error);
    say(error.message, 'error');
    offerSignInAgain(error);
  }
}

async function loadRoomStatus() {
  if (!connected || document.hidden) return;
  try {
    const data = await readJson(await request(box.dataset.roomStatusUrl));
    renderRoomStatus(data.youtube);
    await fillRelayKey(data);
  } catch (error) {
    console.error('YouTube status unavailable', error);
  }
}

async function loadConnection() {
  try {
    const data = await readJson(await request(box.dataset.statusUrl));
    box.hidden = !data.available;
    if (!data.available) return;
    connected = data.connected;
    showState(connected ? 'connected' : 'disconnected');
    if (!connected) return;
    channel.textContent = data.channel_title || data.channel_id;
    // Signing in again replaces the token and keeps every room's broadcast; Disconnect would clear them.
    signInAgain.hidden = !data.last_error;
    if (data.last_error) say(data.last_error, 'error');
    await loadRoomStatus();
    await loadBroadcasts();
  } catch (error) {
    console.error('YouTube connection status unavailable', error);
  }
}

// One change at a time, so a slow request never lands after a later one.
async function change(run) {
  if (pending) return;
  pending = true;
  const buttons = [...box.querySelectorAll('button')];
  buttons.forEach(button => { button.disabled = true; });
  try {
    await run();
  } catch (error) {
    console.error('YouTube update failed', error);
    say(error.message, 'error');
    offerSignInAgain(error);
  } finally {
    buttons.forEach(button => { button.disabled = false; });
    pending = false;
  }
}

// The input changed last wins: a stale pasted link must never override a later list choice.
link.addEventListener('input', () => {
  if (link.value.trim()) select.value = '';
  say('');
});
select.addEventListener('change', () => {
  link.value = '';
  say('');
});

form.addEventListener('submit', event => {
  event.preventDefault();
  const choice = link.value.trim() || select.value;
  if (!choice) {
    say('Choose a broadcast or paste its link.', 'error');
    return;
  }
  change(async () => {
    const data = await readJson(await request(box.dataset.broadcastUrl, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({broadcast: choice}),
    }));
    link.value = '';
    say(data.destination_configured
      ? `This room now streams to "${data.title}". VoxBento filled in its stream key.`
      : `This room is linked to "${data.title}".`, 'success');
    await loadRoomStatus();
  });
});

unlink.addEventListener('click', () => {
  change(async () => {
    await readJson(await request(box.dataset.broadcastUrl, {method: 'DELETE'}));
    say('Broadcast unlinked. A saved stream key stays until you remove it.');
    select.value = '';
    await loadRoomStatus();
  });
});

document.getElementById('youtube-disconnect').addEventListener('click', () => {
  if (!window.confirm('Disconnect YouTube for every room of this event? VoxBento\'s access is revoked at Google. Saved stream keys stay until you remove them.')) return;
  change(async () => {
    await readJson(await request(box.dataset.statusUrl, {method: 'DELETE'}));
    say('YouTube is disconnected.');
    await loadConnection();
  });
});

const params = new URLSearchParams(window.location.search);
const outcome = params.get('youtube');
if (outcome) {
  const [text, tone] = outcomes[outcome] || [''];
  say(text, tone);
  // Back from Google: show the streaming-software panel this flow started from.
  const encoder = document.querySelector('input[name="audio_choice"][value="encoder"]');
  if (encoder && !encoder.checked) {
    encoder.checked = true;
    encoder.dispatchEvent(new Event('change'));
  }
  params.delete('youtube');
  const query = params.toString();
  history.replaceState(null, '', `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`);
}

loadConnection();
// YouTube status is cached on the server for 30 s per room, so polling faster gains nothing.
const statusTimer = setInterval(loadRoomStatus, 30000);
window.addEventListener('pagehide', () => clearInterval(statusTimer));
