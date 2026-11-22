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
const captionsBox = document.getElementById('youtube-captions');
const captionsSelect = document.getElementById('youtube-captions-select');
const captionsStatus = document.getElementById('youtube-captions-status');
// The saved caption language as last reported by the server ('' = off).
let captionsLanguage = '';
let captionChoicesKey = '';
let connected = false;
let linkedId = null;
let pending = false;
// Bumped by every status read and every change: a response from an older read is dropped, so a status
// fetched before a caption change can never overwrite what that change set.
let statusGeneration = 0;
// Relay mode as of the last status check: null until the first one. The stream key is filled in only
// when this page sees relay mode switched on, never because a destination is merely absent: an owner
// who removed the key, or reloads a room without one, keeps forwarding stopped.
let relayWasActive = null;

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

// State-changing requests carry the admin CSRF token from the page's meta tag (double-submit cookie).
const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || '';

function request(url, options = {}) {
  const headers = {...options.headers};
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrfToken();
  return fetch(url, {cache: 'no-store', credentials: 'same-origin', ...options, headers});
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

function captionsStateText(captions) {
  if (!captions.language) return 'Off. Choose a language to post captions to this broadcast.';
  switch (captions.state) {
    case 'posting': {
      const at = captions.last_ok_at ? new Date(captions.last_ok_at * 1000).toLocaleTimeString() : '';
      return at ? `Posting captions. Last accepted by YouTube at ${at}.` : 'Posting captions.';
    }
    case 'waiting': return 'On. Waiting for speech in the room.';
    case 'starting': return 'Starting…';
    case 'failing': return `Not posting: ${captions.reason || 'YouTube refused the captions.'}`;
    default: return 'Off.';
  }
}

// Caption choices come from the server (the spoken language and the room's enabled translations).
function renderCaptions(captions) {
  captionsBox.hidden = !captions;
  if (!captions) return;
  const key = JSON.stringify(captions.choices || []);
  if (key !== captionChoicesKey) {
    captionChoicesKey = key;
    captionsSelect.replaceChildren(new Option('Off', ''), ...(captions.choices || []).map(choice => new Option(choice.label, choice.value)));
  }
  captionsLanguage = captions.language || '';
  if (!pending) captionsSelect.value = captionsLanguage;
  captionsStatus.textContent = captionsStateText(captions);
}

function renderRoomStatus(youtube) {
  linkedId = youtube && !youtube.error ? youtube.broadcast_id : null;
  unlink.hidden = !youtube;
  renderCaptions(youtube && !youtube.error ? youtube.captions : null);
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
  const relayActive = Boolean(data.enabled && data.mode === 'rtmps_relay');
  const switchedOn = relayWasActive === false && relayActive;
  relayWasActive = relayActive;
  if (!switchedOn || pending || !youtube || youtube.error || data.destination?.configured) return;
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
  const generation = ++statusGeneration;
  try {
    const data = await readJson(await request(box.dataset.roomStatusUrl));
    if (generation !== statusGeneration) return;
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
  statusGeneration += 1;
  // The caption selector is locked too, so a second choice is never silently dropped.
  const controls = [...box.querySelectorAll('button'), captionsSelect];
  controls.forEach(control => { control.disabled = true; });
  try {
    await run();
  } catch (error) {
    console.error('YouTube update failed', error);
    say(error.message, 'error');
    offerSignInAgain(error);
  } finally {
    controls.forEach(control => { control.disabled = false; });
    pending = false;
    // Show the caption language in effect after the change, whether it succeeded or not.
    captionsSelect.value = captionsLanguage;
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

captionsSelect.addEventListener('change', () => {
  const language = captionsSelect.value;
  // On an error nothing was saved, and change() puts the selector back on the language in effect.
  change(async () => {
    const captions = await readJson(await request(box.dataset.captionsUrl, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({language: language || null}),
    }));
    say(language ? 'Captions will be posted to this broadcast\'s CC track.' : 'Captions on YouTube are off.', 'success');
    renderCaptions(captions);
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
// YouTube status is cached on the server for 60 s per room, so polling faster gains nothing.
// A poll never runs during a change: the change reads the status itself when it needs to.
const statusTimer = setInterval(() => { if (!pending) loadRoomStatus(); }, 60000);
window.addEventListener('pagehide', () => clearInterval(statusTimer));
