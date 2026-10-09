/**
 * Program Stream Ingest card on the room page: live status polling, one-time
 * secret reveal/copy, rotate and revoke. The secret only ever exists in this
 * page's memory after a rotate call; it is never fetched again.
 */

const POLL_ACTIVE_MS = 3000;
const POLL_IDLE_MS = 10000;
const STATE_LABELS = {
  disabled: 'Disabled',
  waiting: 'Waiting for stream',
  receiving: 'Receiving',
  processing: 'Processing',
  degraded: 'Degraded',
  disconnected: 'Disconnected',
};

let pollTimer = null;

function byId(id) {
  return document.getElementById(id);
}

function formatTimestamp(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

function setText(id, text) {
  const el = byId(id);
  if (el) el.textContent = text;
}

function renderWarnings(warnings) {
  const list = byId('ingest-warnings');
  if (!list) return;
  list.replaceChildren(
    ...warnings.map((warning) => {
      const item = document.createElement('li');
      item.className = 'alert alert-warning';
      item.textContent = warning;
      return item;
    }),
  );
}

function renderStatus(data) {
  const pill = byId('ingest-state-pill');
  if (pill) {
    pill.className = `status-pill ingest-state ingest-state--${data.state}`;
  }
  setText('ingest-state-text', STATE_LABELS[data.state] || data.state);
  setText('ingest-state-detail', data.detail || '');
  setText('ingest-last-connected', formatTimestamp(data.last_connected_at));
  setText('ingest-last-disconnected', formatTimestamp(data.last_disconnected_at));
  setText('ingest-audio-codecs', (data.audio_codecs || []).join(', ') || '—');
  setText('ingest-video-codecs', (data.video_codecs || []).join(', ') || '—');
  setText('ingest-worker', data.worker_running ? 'Running' : 'Stopped');
  renderWarnings(data.warnings || []);
}

function schedulePoll(card, delayMs) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => pollStatus(card), delayMs);
}

async function pollStatus(card) {
  try {
    const response = await fetch(card.dataset.statusUrl, { headers: { Accept: 'application/json' } });
    if (!response.ok) {
      throw new Error(`status request failed with HTTP ${response.status}`);
    }
    const data = await response.json();
    renderStatus(data);
    const busy = data.state === 'receiving' || data.state === 'processing' || data.state === 'degraded';
    schedulePoll(card, busy ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  } catch (error) {
    console.error('Program ingest status poll failed', error);
    setText('ingest-state-detail', 'Could not refresh the ingest status. Retrying…');
    schedulePoll(card, POLL_IDLE_MS);
  }
}

async function copyField(inputId, button) {
  const input = byId(inputId);
  if (!input || !input.value) return;
  const original = button.textContent;
  try {
    await navigator.clipboard.writeText(input.value);
    button.textContent = 'Copied!';
  } catch (error) {
    console.error(`Copying #${inputId} failed; select it and copy manually`, error);
    input.type = 'text';
    input.select();
    button.textContent = 'Select & copy';
  }
  setTimeout(() => { button.textContent = original; }, 1500);
}

function revealSecret(secret) {
  const reveal = byId('ingest-secret-reveal');
  const input = byId('ingest-secret');
  if (!reveal || !input) return;
  input.value = secret;
  input.type = 'password';
  setText('ingest-secret-toggle', 'Show');
  reveal.hidden = false;
}

function clearSecret() {
  const input = byId('ingest-secret');
  if (input) input.value = '';
  const reveal = byId('ingest-secret-reveal');
  if (reveal) reveal.hidden = true;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || `request failed with HTTP ${response.status}`);
  }
  return data;
}

async function rotateSecret(card, button) {
  const expiry = byId('ingest-expiry');
  const days = expiry && expiry.value ? Number.parseInt(expiry.value, 10) : null;
  const replacing = button.textContent.trim().startsWith('Rotate');
  if (replacing && !window.confirm('Rotate the secret? The encoder will be disconnected until you paste the new secret.')) {
    return;
  }
  button.disabled = true;
  try {
    const data = await postJson(card.dataset.credentialUrl, { expires_in_days: days });
    revealSecret(data.secret);
    const expires = data.expires_at ? `, expires ${formatTimestamp(data.expires_at)}` : '';
    setText('ingest-credential-summary', `Active secret ending in …${data.hint}${expires}. Copy it below — it will not be shown again.`);
    button.textContent = 'Rotate secret';
    const revoke = byId('ingest-revoke-btn');
    if (revoke) revoke.disabled = false;
    pollStatus(card);
  } catch (error) {
    console.error('Rotating the program ingest secret failed', error);
    window.alert(`Could not generate a secret: ${error.message}`);
  } finally {
    button.disabled = false;
  }
}

async function revokeSecret(card, button) {
  if (!window.confirm('Revoke the secret? The encoder is disconnected immediately and cannot reconnect.')) {
    return;
  }
  button.disabled = true;
  try {
    await postJson(card.dataset.revokeUrl, {});
    clearSecret();
    setText('ingest-credential-summary', 'No secret yet.');
    setText('ingest-rotate-btn', 'Generate secret');
    pollStatus(card);
  } catch (error) {
    console.error('Revoking the program ingest secret failed', error);
    window.alert(`Could not revoke the secret: ${error.message}`);
    button.disabled = false;
  }
}

function init() {
  const card = byId('program-ingest-card');
  if (!card) return;

  card.querySelectorAll('[data-ingest-copy]').forEach((button) => {
    button.addEventListener('click', () => copyField(button.dataset.ingestCopy, button));
  });

  const toggle = byId('ingest-secret-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const input = byId('ingest-secret');
      if (!input) return;
      const hidden = input.type === 'password';
      input.type = hidden ? 'text' : 'password';
      toggle.textContent = hidden ? 'Hide' : 'Show';
    });
  }

  const rotate = byId('ingest-rotate-btn');
  if (rotate) rotate.addEventListener('click', () => rotateSecret(card, rotate));
  const revoke = byId('ingest-revoke-btn');
  if (revoke) revoke.addEventListener('click', () => revokeSecret(card, revoke));

  ['ingest-last-connected', 'ingest-last-disconnected'].forEach((id) => {
    const el = byId(id);
    if (el) el.textContent = formatTimestamp(el.dataset.iso);
  });

  if (card.dataset.enabled === 'true') {
    pollStatus(card);
  }
}

document.addEventListener('DOMContentLoaded', init);
