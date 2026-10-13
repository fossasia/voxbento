const card = document.getElementById('meeting-source');
const badge = document.getElementById('meeting-state-badge');
const statusText = document.getElementById('meeting-status');
const urlInput = document.getElementById('meeting-url');
const warning = document.getElementById('meeting-warning');
const connectButton = card.querySelector("[data-meeting-action='connect']");
const disconnectButton = card.querySelector("[data-meeting-action='disconnect']");
let busy = false;
// Kept until the next action so the status poll does not hide why a request failed.
let lastError = '';
// The URL the server last accepted; editing the field away from it offers a switch.
let appliedUrl = urlInput.value.trim();
let lastStatus = null;

const stateLabels = {
  stopped: 'Stopped',
  starting: 'Starting',
  joined: 'Joined meeting',
  receiving: 'Receiving audio',
  processing: 'Processing',
  reconnecting: 'Reconnecting',
  degraded: 'Needs attention',
};
const lifecycle = ['starting', 'joined', 'receiving', 'processing'];

/**
 * Decide which lifecycle actions the owner may take for the current status.
 * @param {{enabled: boolean, state: string, floor_source: string}} data
 * @param {boolean} urlChanged whether the URL field differs from the applied URL
 * @returns {{connect: boolean, disconnect: boolean}}
 */
function allowedActions(data, urlChanged) {
  // Connect stays available once automatic recovery gives up, so the owner can retry,
  // and whenever the URL was edited, so a connected agent can move to another meeting.
  return {connect: !data.enabled || data.state === 'degraded' || urlChanged, disconnect: data.enabled};
}

function renderLifecycle(state) {
  const reached = lifecycle.indexOf(state);
  card.querySelectorAll('.meeting-lifecycle li').forEach((item, index) => {
    if (reached === -1 || index > reached) item.dataset.status = 'pending';
    else item.dataset.status = index === reached && state !== 'processing' ? 'current' : 'done';
  });
}

function render(data) {
  lastStatus = data;
  const label = stateLabels[data.state] || data.state;
  badge.textContent = label;
  badge.dataset.state = data.state;
  if (data.enabled) {
    statusText.textContent = data.reason ? `${label}. ${data.reason}` : label;
  } else {
    statusText.textContent = data.reason || 'Not connected. Confirm the meeting URL, then click Connect meeting.';
  }
  if (lastError) statusText.textContent = lastError;
  renderLifecycle(data.state);
  document.getElementById('meeting-owner').textContent = data.floor_source === 'program_ingest'
    ? 'Program ingest'
    : (data.enabled ? 'Meeting agent' : 'None');
  document.getElementById('meeting-restarts').textContent = `${data.restarts} of ${data.max_restarts} used`;

  const attention = data.state === 'degraded' || data.state === 'reconnecting';
  warning.hidden = !attention;
  document.getElementById('meeting-warning-title').textContent = data.state === 'degraded'
    ? 'Meeting source needs attention'
    : 'Meeting agent is reconnecting';
  document.getElementById('meeting-warning-text').textContent = attention ? data.reason : '';

  const urlChanged = data.enabled && urlInput.value.trim() !== appliedUrl;
  const allowed = allowedActions(data, urlChanged);
  connectButton.disabled = !allowed.connect;
  disconnectButton.disabled = !allowed.disconnect;
  if (data.floor_source === 'program_ingest') connectButton.textContent = 'Switch to meeting';
  else connectButton.textContent = urlChanged ? 'Switch meeting' : 'Connect meeting';
}

async function refresh() {
  if (busy || document.hidden) return;
  try {
    const response = await fetch(card.dataset.url, {cache: 'no-store'});
    if (!response.ok) throw new Error(`Cannot load meeting status (HTTP ${response.status})`);
    render(await response.json());
  } catch (error) {
    console.error('Meeting source status unavailable', error);
    statusText.textContent = 'Status unavailable. Check your connection and try again.';
  }
}

async function change(action, confirmSourceChange = false) {
  const response = await fetch(card.dataset.url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({action, meeting_url: urlInput.value, confirm_source_change: confirmSourceChange}),
  });
  let data;
  try {
    data = await response.json();
  } catch (error) {
    // A proxy may return HTML on failure; preserve the HTTP status below.
    if (response.ok) throw error;
  }
  if (response.status === 409 && data?.detail?.code === 'source_conflict') {
    if (window.confirm(data.detail.message)) return change(action, true);
    return null;
  }
  if (!response.ok) {
    throw new Error(typeof data?.detail === 'string' ? data.detail : `Update failed (HTTP ${response.status})`);
  }
  return data;
}

card.querySelectorAll('[data-meeting-action]').forEach(button => {
  button.addEventListener('click', async () => {
    const action = button.dataset.meetingAction;
    if (action === 'connect' && !urlInput.reportValidity()) return;
    if (action === 'disconnect' && !window.confirm('Disconnect the meeting agent? Captions, translation and speech for this room stop until you connect again.')) return;
    busy = true;
    lastError = '';
    connectButton.disabled = true;
    disconnectButton.disabled = true;
    statusText.textContent = action === 'connect' ? 'Connecting the meeting agent…' : 'Disconnecting the meeting agent…';
    try {
      const data = await change(action);
      if (data) {
        if (action === 'connect') appliedUrl = urlInput.value.trim();
        render(data);
      }
    } catch (error) {
      console.error('Meeting source update failed', error);
      lastError = error.message;
    } finally {
      busy = false;
      await refresh();
    }
  });
});

urlInput.addEventListener('input', () => {
  if (lastStatus && !busy) render(lastStatus);
});
refresh();
const refreshTimer = setInterval(refresh, 3000);
window.addEventListener('pagehide', () => clearInterval(refreshTimer));
