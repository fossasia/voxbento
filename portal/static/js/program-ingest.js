const card = document.getElementById('program-ingest');
const status = document.getElementById('program-status');
const secret = document.getElementById('program-secret');
const offset = document.getElementById('program-offset');
const stateBadge = document.getElementById('program-state-badge');
const warning = document.getElementById('program-warning');
const relayRisk = document.getElementById('program-relay-risk');
const relayRadio = document.getElementById('program-mode-relay');
const destinationForm = document.getElementById('program-relay-destination');
const destinationStatus = document.getElementById('program-destination-status');
const relayKey = document.getElementById('program-relay-key');
const relayServer = document.getElementById('program-relay-server');
const fullUrlRow = document.getElementById('program-full-url-row');
const fullUrl = document.getElementById('program-full-url');
let busy = false;
let modeChosen = false;
let latest = null;

const stateLabels = {
  disabled: 'Disabled',
  waiting: 'Waiting for OBS',
  receiving: 'Receiving stream',
  processing: 'Processing audio',
  degraded: 'Needs attention',
  disconnected: 'Disconnected',
};

const healthLabels = {
  disabled: 'Off',
  waiting: 'Waiting',
  connected: 'Connected',
  disconnected: 'Disconnected',
  degraded: 'Needs attention',
  idle: 'Idle',
  starting: 'Starting',
  live: 'Live',
  retrying: 'Retrying',
  stalled: 'Stalled',
  capacity: 'At capacity',
  unconfigured: 'Not configured',
  failed: 'Failed',
  processing: 'Processing',
};

const modeLabels = {whip: 'OBS streaming to YouTube itself', rtmps_relay: 'VoxBento forwarding to YouTube'};

// Per-mode wording; the relay calls its credential a "stream key", as OBS and YouTube do.
const credentialWords = {
  whip: {rotate: 'Rotate token', revoke: 'Revoke token', expires: 'Token expires'},
  rtmps_relay: {rotate: 'New stream key', revoke: 'Revoke stream key', expires: 'Stream key expires'},
};

const confirmations = {
  enable: 'This switches the room to this source and stops VoxBento hearing the room any other way.',
  rotate: 'This creates a new credential. The encoder disconnects until you paste the new one.',
  revoke: 'This disconnects the encoder now and the current credential stops working.',
  disable: 'This disconnects the encoder and switches the room back to the online meeting.',
};

function formatTime(value) {
  if (!value) return null;
  // Timestamps are UTC; SQLite returns them without an offset, which Date would read as local.
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function setStep(name, status) {
  card.querySelectorAll(`[data-step="${name}"]`).forEach(step => { step.dataset.status = status; });
}

function selectedMode() {
  return card.querySelector('input[name="program-mode"]:checked')?.value || 'whip';
}

function applyMode(mode) {
  card.querySelectorAll('[data-mode-panel]').forEach(panel => {
    panel.hidden = panel.dataset.modePanel !== mode;
  });
  const relay = mode === 'rtmps_relay';
  relayRisk.hidden = !relay;
  document.getElementById('program-connect-step').textContent = relay ? '2' : '1';
  document.getElementById('program-monitor-step').textContent = relay ? '3' : '2';
  document.getElementById('program-monitor-title').textContent = relay ? 'Go live and check' : 'Monitor the feed';
  const words = credentialWords[mode];
  card.querySelector('[data-ingest-action="rotate"]').textContent = words.rotate;
  card.querySelector('[data-ingest-action="revoke"]').textContent = words.revoke;
  document.getElementById('program-expires-label').textContent = words.expires;
  const enable = card.querySelector('[data-ingest-action="enable"]');
  const help = document.getElementById('program-enable-help');
  if (latest?.enabled && latest.mode !== mode) {
    enable.textContent = `Switch to ${modeLabels[mode]}`;
    help.textContent = 'Creates a new credential and stops VoxBento hearing the room any other way.';
  } else if (latest?.enabled && latest.credential_present) {
    enable.textContent = relay ? 'Create a new stream key' : 'Create a new token';
    help.textContent = 'Only needed if you lost it. The encoder disconnects until you paste the new one.';
  } else {
    enable.textContent = relay ? 'Create stream key' : 'Enable ingest and create token';
    help.textContent = 'Room-specific and shown only once. Valid for 30 days.';
  }
}

function renderSteps(data) {
  const ingestUp = data.health.ingest.state === 'connected';
  if (selectedMode() === 'rtmps_relay') {
    const relayMode = data.enabled && data.mode === 'rtmps_relay';
    const saved = data.destination.configured;
    setStep('destination', saved ? 'done' : 'current');
    setStep('connect', relayMode && ingestUp ? 'done' : saved ? 'current' : 'todo');
    setStep('monitor', relayMode && data.health.relay.state === 'live' ? 'done' : relayMode && ingestUp ? 'current' : 'todo');
  } else {
    const whipMode = data.enabled && data.mode === 'whip';
    setStep('connect', whipMode && ingestUp ? 'done' : 'current');
    setStep('monitor', whipMode && data.health.processing.state === 'processing' ? 'done' : whipMode && ingestUp ? 'current' : 'todo');
  }
}

function renderHealth(stage, value) {
  const chip = document.getElementById(`program-health-${stage}`);
  chip.textContent = healthLabels[value.state] || value.state;
  chip.dataset.state = value.state;
  document.getElementById(`program-health-${stage}-reason`).textContent = value.reason || '';
}

function renderStatus(data) {
  const stateLabel = stateLabels[data.state] || data.state;
  // In relay mode YouTube delivery is what the organizer watches; captions have their own row.
  const youtubeLive = data.enabled && data.mode === 'rtmps_relay' && data.health.relay.state === 'live';
  stateBadge.textContent = youtubeLive ? 'Live on YouTube' : stateLabel;
  stateBadge.dataset.state = youtubeLive ? 'processing' : data.state;
  status.textContent = data.reason ? `${stateLabel}. ${data.reason}` : stateLabel;
  document.getElementById('program-codecs').textContent = data.codecs.join(', ') || 'None detected';
  document.getElementById('program-connected').textContent = formatTime(data.last_connected_at) || 'Never';
  document.getElementById('program-disconnected').textContent = formatTime(data.last_disconnected_at) || 'Never';
  document.getElementById('program-expires').textContent = formatTime(data.expires_at) || 'Not created';
  renderHealth('ingest', data.health.ingest);
  renderHealth('relay', data.health.relay);
  renderHealth('processing', data.health.processing);
  renderSteps(data);

  const destination = data.destination;
  destinationStatus.textContent = destination.configured
    ? `Saved ${formatTime(destination.updated_at)} for ${destination.server_url}. The key can't be shown again; paste a new one to replace it.`
    : 'Not saved yet. YouTube receives nothing until you save a stream key.';
  relayKey.placeholder = destination.configured ? 'Saved — paste a new key to replace it' : 'xxxx-xxxx-xxxx-xxxx-xxxx';
  document.getElementById('program-relay-save').textContent = destination.configured ? 'Replace stream key' : 'Save stream key';
  document.getElementById('program-relay-clear').disabled = !destination.configured || !data.relay_available;

  const relaySelected = selectedMode() === 'rtmps_relay';
  const warnings = [...data.warnings];
  if (!data.available) warnings.push('Ask the server operator to enable authenticated program ingest.');
  if (relaySelected && data.relay_available && data.relay_service_down) {
    warnings.push('The YouTube relay service on the server is not responding. Ask the server operator to check the program-relay container.');
  }
  if (relaySelected && data.relay_available && !data.rtmps_server) {
    warnings.push('The server has no public RTMPS address yet. Ask the server operator to set PROGRAM_RTMPS_PUBLIC_BASE.');
  }
  warning.hidden = warnings.length === 0;
  document.getElementById('program-warning-title').textContent = data.available
    ? 'Program ingest needs attention'
    : 'Program ingest is not available yet';
  const list = document.getElementById('program-warnings');
  list.replaceChildren(...warnings.map(text => {
    const item = document.createElement('li');
    item.textContent = text;
    return item;
  }));
}

function updateFullUrl() {
  const show = secret.value !== '' && secret.dataset.kind === 'stream_key';
  fullUrlRow.hidden = !show;
  fullUrl.value = show ? `${document.getElementById('program-rtmps-server').value}/${secret.value}` : '';
}

async function copyField(input, button, successLabel) {
  const originalLabel = button.textContent;
  try {
    await navigator.clipboard.writeText(input.value);
    button.textContent = successLabel;
    window.setTimeout(() => { button.textContent = originalLabel; }, 1600);
  } catch (error) {
    console.error('Program ingest value could not be copied', error);
    status.textContent = 'Clipboard unavailable. Select and copy the value manually.';
  }
}

async function readError(response) {
  let data;
  try {
    data = await response.json();
  } catch (error) {
    // A proxy may return HTML on failure; preserve the HTTP status below.
    if (response.ok) throw error;
  }
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : `Update failed (HTTP ${response.status})`;
    throw new Error(detail);
  }
  return data;
}

async function refresh() {
  if (busy || document.hidden) return;
  try {
    const response = await fetch(card.dataset.url, {cache: 'no-store'});
    if (!response.ok) throw new Error('Cannot load program ingest status');
    const data = await response.json();
    latest = data;
    if (!modeChosen && data.enabled) {
      card.querySelector(`input[name="program-mode"][value="${data.mode}"]`).checked = true;
    }
    relayRadio.disabled = !data.relay_available;
    document.getElementById('program-relay-unavailable').hidden = data.relay_available;
    applyMode(selectedMode());
    document.getElementById('program-endpoint').value = data.endpoint;
    document.getElementById('program-rtmps-server').value = data.rtmps_server;
    renderStatus(data);
    card.querySelectorAll('[data-ingest-action]').forEach(button => {
      const action = button.dataset.ingestAction;
      const relayBlocked = action === 'enable' && selectedMode() === 'rtmps_relay' && !data.relay_available;
      button.disabled = (['enable', 'rotate'].includes(action) && !data.available) || relayBlocked ||
        (['rotate', 'revoke', 'disable'].includes(action) && !data.enabled);
    });
    destinationForm.querySelectorAll('input, button[type="submit"]').forEach(item => {
      item.disabled = !data.relay_available;
    });
    updateFullUrl();
  } catch (error) {
    console.error('Program ingest status unavailable', error);
    status.textContent = 'Status unavailable. Check your connection and try again.';
  }
}

function showSecret(data) {
  const relay = Boolean(data.stream_key);
  secret.value = relay ? data.stream_key : data.secret;
  secret.dataset.kind = relay ? 'stream_key' : 'token';
  document.getElementById('program-secret-label').textContent = relay ? 'Stream key' : 'Bearer token';
  document.getElementById('program-secret-help').textContent = relay
    ? 'Paste this into OBS as the Stream Key. Copy it now: it is shown only once and expires in 30 days.'
    : 'Paste this into OBS as the Bearer Token. It expires in 30 days.';
  document.getElementById('program-secret-panel').hidden = false;
  updateFullUrl();
}

card.querySelectorAll('input[name="program-mode"]').forEach(radio => {
  radio.addEventListener('change', () => {
    modeChosen = true;
    applyMode(selectedMode());
    if (latest) renderStatus(latest);
  });
});

card.querySelectorAll('[data-ingest-action]').forEach(button => {
  button.addEventListener('click', async () => {
    const action = button.dataset.ingestAction;
    const mode = selectedMode();
    // Creating the first credential for a room already in this mode disconnects nothing.
    const firstCredential = action === 'enable' && latest?.enabled && latest.mode === mode && !latest.credential_present;
    const relayNotice = action === 'enable' && mode === 'rtmps_relay'
      ? ' While VoxBento forwards to YouTube, a VoxBento outage interrupts YouTube.'
      : '';
    if (action !== 'sync' && !firstCredential && !window.confirm(`${confirmations[action]}${relayNotice} Continue?`)) return;
    if (!offset.reportValidity()) return;
    busy = true;
    let shouldRefresh = false;
    card.querySelectorAll('button').forEach(item => { item.disabled = true; });
    secret.value = '';
    secret.type = 'password';
    fullUrl.type = 'password';
    updateFullUrl();
    document.getElementById('program-reveal').textContent = 'Show';
    document.getElementById('program-secret-panel').hidden = true;
    try {
      const response = await fetch(card.dataset.url, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action, sync_offset_ms: Number(offset.value), mode}),
      });
      const data = await readError(response);
      if (data.secret) showSecret(data);
      if (action === 'enable') modeChosen = false;
      shouldRefresh = true;
    } catch (error) {
      console.error('Program ingest update failed', error);
      status.textContent = error.message;
    } finally {
      busy = false;
      card.querySelectorAll('button').forEach(item => { item.disabled = false; });
      if (shouldRefresh) await refresh();
    }
  });
});

const destinationButtons = [document.getElementById('program-relay-save'), document.getElementById('program-relay-clear')];
let destinationPending = false;

// One destination change at a time: a slow save must never land after a later removal.
async function changeDestination(request, failureLabel) {
  if (destinationPending || busy) return;
  destinationPending = true;
  busy = true;
  const previous = destinationButtons.map(button => button.disabled);
  destinationButtons.forEach(button => { button.disabled = true; });
  let changed = false;
  try {
    await readError(await request());
    changed = true;
  } catch (error) {
    console.error(failureLabel, error);
    destinationStatus.textContent = error.message;
  } finally {
    busy = false;
    if (changed) {
      await refresh();
    } else {
      destinationButtons.forEach((button, index) => { button.disabled = previous[index]; });
    }
    destinationPending = false;
  }
  return changed;
}

destinationForm.addEventListener('submit', async event => {
  event.preventDefault();
  if (!destinationForm.reportValidity()) return;
  const saved = await changeDestination(() => fetch(card.dataset.relayUrl, {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({server_url: relayServer.value.trim() || null, stream_key: relayKey.value.trim()}),
  }), 'YouTube destination update failed');
  if (saved) relayKey.value = '';
});

document.getElementById('program-relay-clear').addEventListener('click', async () => {
  if (destinationPending || busy) return;
  if (!window.confirm('Remove the saved YouTube stream key? Forwarding stops and YouTube stops receiving this room.')) return;
  await changeDestination(() => fetch(card.dataset.relayUrl, {method: 'DELETE'}), 'YouTube destination removal failed');
});

document.getElementById('program-reveal').addEventListener('click', () => {
  secret.type = secret.type === 'password' ? 'text' : 'password';
  fullUrl.type = secret.type;
  document.getElementById('program-reveal').textContent = secret.type === 'password' ? 'Show' : 'Hide';
});
document.getElementById('program-copy-full-url').addEventListener('click', async () => {
  await copyField(fullUrl, document.getElementById('program-copy-full-url'), 'Copied');
});
document.getElementById('program-copy').addEventListener('click', async () => {
  await copyField(secret, document.getElementById('program-copy'), 'Copied');
});
document.getElementById('program-copy-endpoint').addEventListener('click', async () => {
  await copyField(document.getElementById('program-endpoint'), document.getElementById('program-copy-endpoint'), 'URL copied');
});
document.getElementById('program-copy-rtmps').addEventListener('click', async () => {
  await copyField(document.getElementById('program-rtmps-server'), document.getElementById('program-copy-rtmps'), 'Server copied');
});
window.addEventListener('pagehide', () => { secret.value = ''; fullUrl.value = ''; relayKey.value = ''; });
refresh();
const refreshTimer = setInterval(refresh, 5000);
window.addEventListener('pagehide', () => clearInterval(refreshTimer));
