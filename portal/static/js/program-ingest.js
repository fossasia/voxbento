const card = document.getElementById('program-ingest');
const status = document.getElementById('program-status');
const secret = document.getElementById('program-secret');
const offset = document.getElementById('program-offset');
let busy = false;

async function refresh() {
  if (busy || document.hidden) return;
  try {
    const response = await fetch(card.dataset.url, {cache: 'no-store'});
    if (!response.ok) throw new Error('Cannot load program ingest status');
    const data = await response.json();
    document.getElementById('program-endpoint').value = data.endpoint;
    status.textContent = `${data.state}: ${data.reason} Codecs: ${data.codecs.join(', ') || 'none'}. Last connected: ${data.last_connected_at || 'never'}. Disconnected: ${data.last_disconnected_at || 'never'}. Token expires: ${data.expires_at || 'none'}.`;
    document.getElementById('program-warnings').textContent = data.warnings.join(' ') +
      (data.available ? '' : ' Server operator must enable authenticated program ingest first.');
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
});
document.getElementById('program-copy').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(secret.value);
  } catch (error) {
    console.error('Cannot copy program token', error);
    status.textContent = 'Clipboard unavailable. Reveal and copy the token manually.';
  }
});
window.addEventListener('pagehide', () => { secret.value = ''; });
refresh();
const refreshTimer = setInterval(refresh, 5000);
window.addEventListener('pagehide', () => clearInterval(refreshTimer));
