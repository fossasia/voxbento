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

const modeLabels = {whip: 'independent outputs (WHIP)', rtmps_relay: 'YouTube relay (RTMPS)'};

function selectedMode() {
  return card.querySelector('input[name="program-mode"]:checked')?.value || 'whip';
}

function applyMode(mode) {
  card.querySelectorAll('[data-mode-panel]').forEach(panel => {
    panel.hidden = panel.dataset.modePanel !== mode;
  });
  relayRisk.hidden = mode !== 'rtmps_relay';
  document.getElementById('program-monitor-step').textContent = mode === 'rtmps_relay' ? '3' : '2';
  const enable = card.querySelector('[data-ingest-action="enable"]');
  if (latest?.enabled && latest.mode !== mode) {
    enable.textContent = `Switch to ${modeLabels[mode]} and create credential`;
  } else {
    enable.textContent = mode === 'rtmps_relay' ? 'Enable relay ingest and create stream key' : 'Enable ingest and create token';
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
  stateBadge.textContent = stateLabel;
  stateBadge.dataset.state = data.state;
  status.textContent = data.reason ? `${stateLabel}. ${data.reason}` : stateLabel;
  document.getElementById('program-codecs').textContent = data.codecs.join(', ') || 'None detected';
  document.getElementById('program-connected').textContent = data.last_connected_at || 'Never';
  document.getElementById('program-disconnected').textContent = data.last_disconnected_at || 'Never';
  document.getElementById('program-expires').textContent = data.expires_at || 'Not created';
  renderHealth('ingest', data.health.ingest);
  renderHealth('relay', data.health.relay);
  renderHealth('processing', data.health.processing);

  const destination = data.destination;
  destinationStatus.textContent = destination.configured
    ? `Configured for ${destination.server_url}. Updated ${destination.updated_at}.`
    : 'Not configured. YouTube receives nothing until a destination is saved.';
  document.getElementById('program-relay-clear').disabled = !destination.configured;

  const warnings = [...data.warnings];
  if (!data.available) warnings.push('Ask the server operator to enable authenticated program ingest.');
  if (selectedMode() === 'rtmps_relay' && !data.relay_available) {
    warnings.push('YouTube relay mode is not enabled on this server.');
  }
  warning.hidden = warnings.length === 0;
  document.getElementById('program-warning-title').textContent = data.available
    ? 'Program ingest needs attention'
    : 'Program ingest is not available yet';
  document.getElementById('program-warnings').textContent = warnings.join(' ');
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
  } catch (error) {
    console.error('Program ingest status unavailable', error);
    status.textContent = 'Status unavailable. Check your connection and try again.';
  }
}

function showSecret(data) {
  const relay = Boolean(data.stream_key);
  secret.value = relay ? data.stream_key : data.secret;
  document.getElementById('program-secret-label').textContent = relay ? 'Stream key' : 'Bearer token';
  document.getElementById('program-secret-help').textContent = relay
    ? 'Paste this into OBS as the Stream Key. It expires in 30 days.'
    : 'Paste this into OBS as the Bearer Token. It expires in 30 days.';
  document.getElementById('program-secret-panel').hidden = false;
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
    const relayNotice = action === 'enable' && mode === 'rtmps_relay'
      ? ' In relay mode a VoxBento outage interrupts YouTube.'
      : '';
    if (action !== 'sync' && !window.confirm(`This changes the floor source or credential and disconnects the current floor stream.${relayNotice} Continue?`)) return;
    if (!offset.reportValidity()) return;
    busy = true;
    let shouldRefresh = false;
    card.querySelectorAll('button').forEach(item => { item.disabled = true; });
    secret.value = '';
    secret.type = 'password';
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
    body: JSON.stringify({
      server_url: document.getElementById('program-relay-server').value,
      stream_key: relayKey.value,
    }),
  }), 'YouTube destination update failed');
  if (saved) relayKey.value = '';
});

document.getElementById('program-relay-clear').addEventListener('click', async () => {
  if (destinationPending || busy) return;
  if (!window.confirm('Remove the YouTube destination? The relay stops and YouTube stops receiving this room.')) return;
  await changeDestination(() => fetch(card.dataset.relayUrl, {method: 'DELETE'}), 'YouTube destination removal failed');
});

document.getElementById('program-reveal').addEventListener('click', () => {
  secret.type = secret.type === 'password' ? 'text' : 'password';
  document.getElementById('program-reveal').textContent = secret.type === 'password' ? 'Show' : 'Hide';
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
window.addEventListener('pagehide', () => { secret.value = ''; relayKey.value = ''; });
refresh();
const refreshTimer = setInterval(refresh, 5000);
window.addEventListener('pagehide', () => clearInterval(refreshTimer));
