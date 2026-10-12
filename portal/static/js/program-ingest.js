const card = document.getElementById('program-ingest');
const status = document.getElementById('program-status');
const secret = document.getElementById('program-secret');
const offset = document.getElementById('program-offset');
const stateBadge = document.getElementById('program-state-badge');
const warning = document.getElementById('program-warning');
let busy = false;

const stateLabels = {
  disabled: 'Disabled',
  waiting: 'Waiting for OBS',
  receiving: 'Receiving stream',
  processing: 'Processing audio',
  degraded: 'Needs attention',
  disconnected: 'Disconnected',
};

function renderStatus(data) {
  const stateLabel = stateLabels[data.state] || data.state;
  stateBadge.textContent = stateLabel;
  stateBadge.dataset.state = data.state;
  status.textContent = data.reason ? `${stateLabel}. ${data.reason}` : stateLabel;
  document.getElementById('program-codecs').textContent = data.codecs.join(', ') || 'None detected';
  document.getElementById('program-connected').textContent = data.last_connected_at || 'Never';
  document.getElementById('program-disconnected').textContent = data.last_disconnected_at || 'Never';
  document.getElementById('program-expires').textContent = data.expires_at || 'Not created';

  const warnings = [...data.warnings];
  if (!data.available) warnings.push('Ask the server operator to enable authenticated program ingest.');
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

async function refresh() {
  if (busy || document.hidden) return;
  try {
    const response = await fetch(card.dataset.url, {cache: 'no-store'});
    if (!response.ok) throw new Error('Cannot load program ingest status');
    const data = await response.json();
    document.getElementById('program-endpoint').value = data.endpoint;
    renderStatus(data);
    card.querySelectorAll('[data-ingest-action]').forEach(button => {
      const action = button.dataset.ingestAction;
      button.disabled = (['enable', 'rotate'].includes(action) && !data.available) ||
        (['rotate', 'revoke', 'disable'].includes(action) && !data.enabled);
    });
  } catch (error) {
    console.error('Program ingest status unavailable', error);
    status.textContent = 'Status unavailable. Check your connection and try again.';
  }
}

card.querySelectorAll('[data-ingest-action]').forEach(button => {
  button.addEventListener('click', async () => {
    const action = button.dataset.ingestAction;
    if (action !== 'sync' && !window.confirm('This changes the floor source or credential and disconnects the current floor stream. Continue?')) return;
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
        body: JSON.stringify({action, sync_offset_ms: Number(offset.value)}),
      });
      let data;
      try {
        data = await response.json();
      } catch (error) {
        // A proxy may return HTML on failure; preserve the HTTP status below.
        if (response.ok) throw error;
      }
      if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : `Update failed (HTTP ${response.status})`);
      if (data.secret) {
        secret.value = data.secret;
        document.getElementById('program-secret-panel').hidden = false;
      }
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
document.getElementById('program-reveal').addEventListener('click', () => {
  secret.type = secret.type === 'password' ? 'text' : 'password';
  document.getElementById('program-reveal').textContent = secret.type === 'password' ? 'Show' : 'Hide';
});
document.getElementById('program-copy').addEventListener('click', async () => {
  await copyField(secret, document.getElementById('program-copy'), 'Token copied');
});
document.getElementById('program-copy-endpoint').addEventListener('click', async () => {
  await copyField(document.getElementById('program-endpoint'), document.getElementById('program-copy-endpoint'), 'URL copied');
});
window.addEventListener('pagehide', () => { secret.value = ''; });
refresh();
const refreshTimer = setInterval(refresh, 5000);
window.addEventListener('pagehide', () => clearInterval(refreshTimer));
