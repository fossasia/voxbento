const card = document.getElementById('program-ingest');
const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || '';
const status = document.getElementById('program-status');
const secret = document.getElementById('program-secret');
const offset = document.getElementById('program-offset');
const stateBadge = document.getElementById('program-state-badge');
const warning = document.getElementById('program-warning');
const relayRisk = document.getElementById('program-relay-risk');
const relayRadio = document.getElementById('program-mode-relay');
const rtmpsRadio = document.getElementById('program-mode-rtmps');
const destinationForm = document.getElementById('program-relay-destination');
const destinationStatus = document.getElementById('program-destination-status');
const relayKey = document.getElementById('program-relay-key');
const relayServer = document.getElementById('program-relay-server');
const fullUrlRow = document.getElementById('program-full-url-row');
const fullUrl = document.getElementById('program-full-url');
let busy = false;
let modeChosen = false;
let latest = null;
let currentPassphrase = '';
let currentBackupPassphrase = '';

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

const modeLabels = {
  whip: 'OBS streaming to YouTube itself',
  rtmps: 'Streaming tool sends VoxBento an extra stream',
  rtmps_relay: 'VoxBento forwarding to YouTube',
  srt: 'Secure Reliable Transport (SRT)',
};

// Both RTMPS modes take the OBS-style server and stream key.
const RTMPS_MODES = ['rtmps', 'rtmps_relay'];

// Per-mode wording; RTMPS modes call the credential a "stream key", as OBS and YouTube do.
const streamKeyWords = {rotate: 'New stream key', revoke: 'Revoke stream key', expires: 'Stream key expires'};
const credentialWords = {
  whip: {rotate: 'Rotate token', revoke: 'Revoke token', expires: 'Token expires'},
  rtmps: streamKeyWords,
  rtmps_relay: streamKeyWords,
  srt: {rotate: 'New passphrase', revoke: 'Revoke passphrase', expires: 'Passphrase expires'},
};

const confirmations = {
  enable: 'This switches the room to this source and stops VoxBento hearing the room any other way.',
  rotate: 'This creates a new credential. The encoder disconnects until you paste the new one.',
  revoke: 'This disconnects the encoder now and the current credential stops working.',
  disable: 'This disconnects the encoder and switches the room back to the online meeting.',
  promote_primary: 'This switches the active feed back to the primary contribution stream.',
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

function savePassphrases(pass, backupPass) {
  if (pass !== undefined && pass !== null) {
    currentPassphrase = pass;
  }
  if (backupPass !== undefined && backupPass !== null) {
    currentBackupPassphrase = backupPass;
  }
}

function clearSavedPassphrases() {
  currentPassphrase = '';
  currentBackupPassphrase = '';
}

const toolTitles = {
  obs: 'Connect OBS Studio',
  vmix: 'Connect vMix',
  wirecast: 'Connect Wirecast',
  hardware: 'Connect hardware encoder',
};

function currentTool() {
  const activeTab = card.querySelector('.program-tool-tab.is-active');
  return activeTab?.dataset?.tool || 'obs';
}

function updateConnectTitle() {
  const titleEl = document.getElementById('program-connect-title');
  if (!titleEl) return;
  const mode = selectedMode();
  if (mode === 'srt') {
    const tool = currentTool();
    titleEl.textContent = toolTitles[tool] || 'Connect encoder';
  } else if (mode === 'rtmps') {
    titleEl.textContent = 'Connect streaming tool';
  } else {
    titleEl.textContent = 'Connect OBS';
  }
}

function selectedMode() {
  return card.querySelector('input[name="program-mode"]:checked')?.value || 'whip';
}

function applyMode(mode) {
  card.querySelectorAll('[data-mode-panel]').forEach(panel => {
    panel.hidden = !panel.dataset.modePanel.split(' ').includes(mode);
  });
  const relay = mode === 'rtmps_relay';
  const streamKey = RTMPS_MODES.includes(mode);
  relayRisk.hidden = !relay;
  document.getElementById('program-connect-step').textContent = relay ? '2' : '1';
  document.getElementById('program-monitor-step').textContent = relay ? '3' : '2';
  document.getElementById('program-monitor-title').textContent = relay ? 'Go live and check' : 'Monitor the feed';
  updateConnectTitle();
  const words = credentialWords[mode];
  card.querySelector('[data-ingest-action="rotate"]').textContent = words.rotate;
  card.querySelector('[data-ingest-action="revoke"]').textContent = words.revoke;
  document.getElementById('program-expires-label').textContent = words.expires;
  const enable = card.querySelector('[data-ingest-action="enable"]');
  const help = document.getElementById('program-enable-help');
  const isSrt = mode === 'srt';
  if (latest?.enabled && latest.mode !== mode) {
    enable.textContent = `Switch to ${modeLabels[mode]}`;
    help.textContent = 'Creates a new credential and stops VoxBento hearing the room any other way.';
  } else if (latest?.enabled && latest.credential_present) {
    enable.textContent = isSrt ? 'Create a new passphrase' : (streamKey ? 'Create a new stream key' : 'Create a new token');
    help.textContent = 'Only needed if you lost it. The encoder disconnects until you paste the new one.';
  } else {
    enable.textContent = isSrt ? 'Enable ingest and create passphrase' : (streamKey ? 'Create stream key' : 'Enable ingest and create token');
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
    const modeActive = data.enabled && data.mode === selectedMode();
    setStep('connect', modeActive && ingestUp ? 'done' : 'current');
    setStep('monitor', modeActive && data.health.processing.state === 'processing' ? 'done' : modeActive && ingestUp ? 'current' : 'todo');
  }
}

function renderHealth(stage, value) {
  const chip = document.getElementById(`program-health-${stage}`);
  chip.textContent = healthLabels[value.state] || value.state;
  chip.dataset.state = value.state;
  document.getElementById(`program-health-${stage}-reason`).textContent = value.reason || '';
}

function renderStatus(data) {
  const stateLabel = data.state === 'waiting' && data.mode === 'rtmps'
    ? 'Waiting for your streaming tool'
    : stateLabels[data.state] || data.state;
  // In relay mode YouTube delivery is what the organizer watches; captions have their own row.
  const youtubeLive = data.enabled && data.mode === 'rtmps_relay' && data.health.relay.state === 'live';
  stateBadge.textContent = youtubeLive ? 'Live on YouTube' : stateLabel;
  stateBadge.dataset.state = youtubeLive ? 'processing' : data.state;
  status.textContent = data.reason ? `${stateLabel}. ${data.reason}` : stateLabel;
  document.getElementById('program-codecs').textContent = data.codecs.join(', ') || 'None detected';
  document.getElementById('program-connected').textContent = formatTime(data.last_connected_at) || 'Never';
  document.getElementById('program-disconnected').textContent = formatTime(data.last_disconnected_at) || 'Never';
  document.getElementById('program-expires').textContent = formatTime(data.expires_at) || 'Not created';
  const activeFeedEl = document.getElementById('program-active-feed');
  if (activeFeedEl) {
    activeFeedEl.textContent = data.active_feed === 'backup' ? 'Backup (active)' : 'Primary';
  }
  const promoteBtn = document.getElementById('program-promote-primary');
  if (promoteBtn) {
    promoteBtn.hidden = !(selectedMode() === 'srt' && data.enabled && data.active_feed === 'backup');
  }
  const backupToggle = document.getElementById('program-backup-toggle');
  if (backupToggle) {
    backupToggle.checked = Boolean(data.backup_enabled);
    backupToggle.disabled = !data.enabled || selectedMode() !== 'srt';
  }
  const backupStatusEl = document.getElementById('program-backup-status');
  if (backupStatusEl && selectedMode() === 'srt') {
    if (!data.backup_enabled) {
      backupStatusEl.textContent = 'Disabled';
    } else if (data.active_feed === 'backup') {
      backupStatusEl.textContent = 'Active (on-air)';
    } else if (data.backup_connected) {
      backupStatusEl.textContent = 'Connected (standing by)';
    } else {
      backupStatusEl.textContent = 'Disconnected';
    }
  }
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
  const rtmpsSelected = RTMPS_MODES.includes(selectedMode());
  const warnings = [...data.warnings];
  if (!data.available) warnings.push('Ask the server operator to enable authenticated program ingest.');
  if (relaySelected && data.relay_available && data.relay_service_down) {
    warnings.push('The YouTube relay service on the server is not responding. Ask the server operator to check the program-relay container.');
  }
  if (rtmpsSelected && data.rtmps_available && !data.rtmps_server) {
    warnings.push('The server has no public RTMPS address yet. Ask the server operator to set PROGRAM_RTMPS_PUBLIC_BASE.');
  }
  if (Array.isArray(data.alerts)) {
    for (const a of data.alerts) {
      warnings.push(`[${a.target.toUpperCase()}] ${a.message} (Action: ${a.action})`);
    }
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

function updateSrtToolFields(data) {
  if (!data) return;
  const host = data.srt_host || (window.location.hostname || 'localhost');
  const port = data.srt_port || 8890;
  const path = data.srt_path || '';
  const backupPath = data.srt_backup_path || '';
  const pass = currentPassphrase;
  const backupPass = currentBackupPassphrase;
  const hasCredential = Boolean(data.credential_present || pass);

  // OBS URLs
  let obsUrl = '';
  let backupObsUrl = '';
  if (pass) {
    obsUrl = `srt://${host}:${port}?streamid=publish:${path}::${pass}&passphrase=${pass}&pkt_size=1316`;
  } else if (hasCredential) {
    obsUrl = `srt://${host}:${port}?streamid=publish:${path}::<passphrase>&passphrase=<passphrase>&pkt_size=1316`;
  } else {
    obsUrl = data.srt_server || `srt://${host}:${port}?streamid=publish:${path}&pkt_size=1316`;
  }

  if (backupPass) {
    backupObsUrl = `srt://${host}:${port}?streamid=publish:${backupPath}::${backupPass}&passphrase=${backupPass}&pkt_size=1316`;
  } else if (hasCredential) {
    backupObsUrl = `srt://${host}:${port}?streamid=publish:${backupPath}::<passphrase>&passphrase=<passphrase>&pkt_size=1316`;
  } else {
    backupObsUrl = data.srt_backup_server || `srt://${host}:${port}?streamid=publish:${backupPath}&pkt_size=1316`;
  }

  const srtServerInput = document.getElementById('program-srt-server');
  if (srtServerInput) srtServerInput.value = obsUrl;
  const srtBackupInput = document.getElementById('program-srt-backup');
  if (srtBackupInput) srtBackupInput.value = backupObsUrl;

  // Stream IDs
  const streamId = pass ? `publish:${path}::${pass}` : (hasCredential ? `publish:${path}::<passphrase>` : `publish:${path}`);
  const backupStreamId = backupPass ? `publish:${backupPath}::${backupPass}` : (hasCredential ? `publish:${backupPath}::<passphrase>` : `publish:${backupPath}`);

  // vMix fields
  const vmixHost = document.getElementById('program-srt-vmix-host');
  if (vmixHost) vmixHost.value = host;
  const vmixPort = document.getElementById('program-srt-vmix-port');
  if (vmixPort) vmixPort.value = port;
  const vmixStreamId = document.getElementById('program-srt-vmix-streamid');
  if (vmixStreamId) vmixStreamId.value = streamId;
  const vmixBackupStreamId = document.getElementById('program-srt-vmix-backup-streamid');
  if (vmixBackupStreamId) vmixBackupStreamId.value = backupStreamId;

  // Wirecast fields
  const wcAddress = document.getElementById('program-srt-wirecast-address');
  if (wcAddress) wcAddress.value = host;
  const wcPort = document.getElementById('program-srt-wirecast-port');
  if (wcPort) wcPort.value = port;
  const wcStreamId = document.getElementById('program-srt-wirecast-streamid');
  if (wcStreamId) wcStreamId.value = streamId;
  const wcBackupStreamId = document.getElementById('program-srt-wirecast-backup-streamid');
  if (wcBackupStreamId) wcBackupStreamId.value = backupStreamId;

  // Hardware / Other fields
  const hwHost = document.getElementById('program-srt-hw-host');
  if (hwHost) hwHost.value = host;
  const hwPort = document.getElementById('program-srt-hw-port');
  if (hwPort) hwPort.value = port;
  const hwStreamId = document.getElementById('program-srt-hw-streamid');
  if (hwStreamId) hwStreamId.value = streamId;
  const hwUrl = document.getElementById('program-srt-hw-url');
  if (hwUrl) hwUrl.value = obsUrl;
  const hwBackupStreamId = document.getElementById('program-srt-hw-backup-streamid');
  if (hwBackupStreamId) hwBackupStreamId.value = backupStreamId;
  const hwBackupUrl = document.getElementById('program-srt-hw-backup-url');
  if (hwBackupUrl) hwBackupUrl.value = backupObsUrl;

  // Manage passphrase inputs across vMix, Wirecast, and Hardware
  const passInputs = [
    { inputId: 'program-srt-vmix-passphrase', pass: pass },
    { inputId: 'program-srt-vmix-backup-passphrase', pass: backupPass },
    { inputId: 'program-srt-wirecast-passphrase', pass: pass },
    { inputId: 'program-srt-wirecast-backup-passphrase', pass: backupPass },
    { inputId: 'program-srt-hw-passphrase', pass: pass },
    { inputId: 'program-srt-hw-backup-passphrase', pass: backupPass },
  ];

  passInputs.forEach(({ inputId, pass: p }) => {
    const inputEl = document.getElementById(inputId);
    const toggleBtn = card.querySelector(`[data-toggle-target="${inputId}"]`);
    const copyBtn = card.querySelector(`[data-copy-target="${inputId}"]`);
    if (!inputEl) return;

    if (p) {
      inputEl.value = p;
      inputEl.placeholder = '';
      if (toggleBtn) {
        toggleBtn.disabled = false;
        toggleBtn.title = '';
      }
      if (copyBtn) {
        copyBtn.disabled = false;
      }
    } else {
      inputEl.value = '';
      inputEl.placeholder = hasCredential ? 'Hidden (click "Create a new passphrase" to view)' : 'Click "Create a new passphrase"';
      if (toggleBtn) {
        toggleBtn.disabled = true;
        toggleBtn.textContent = 'Show';
        toggleBtn.title = 'Passphrase is encrypted on the server. Click "Create a new passphrase" to generate a viewable one.';
      }
      if (copyBtn) {
        copyBtn.disabled = true;
      }
    }
  });

  // Toggle backup sections visibility based on data.backup_enabled
  const backupSections = card.querySelectorAll('[data-backup-section]');
  backupSections.forEach(section => {
    section.hidden = !data.backup_enabled;
  });
}

function initToolTabs() {
  const tabs = card.querySelectorAll('.program-tool-tab');
  const panels = card.querySelectorAll('.program-tool-panel');
  if (!tabs.length) return;

  function selectTool(toolName) {
    tabs.forEach(tab => {
      const active = tab.dataset.tool === toolName;
      tab.classList.toggle('is-active', active);
      tab.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    panels.forEach(panel => {
      panel.hidden = panel.dataset.toolPanel !== toolName;
    });
    try { localStorage.setItem('voxbento_srt_tool', toolName); } catch {}
    updateConnectTitle();
  }

  tabs.forEach(tab => {
    tab.addEventListener('click', () => selectTool(tab.dataset.tool));
  });

  const saved = (function() {
    try { return localStorage.getItem('voxbento_srt_tool'); } catch { return null; }
  })() || 'obs';
  selectTool(saved);

  card.querySelectorAll('[data-copy-target]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const targetId = btn.dataset.copyTarget;
      const targetEl = document.getElementById(targetId);
      if (targetEl && targetEl.value) {
        await copyField(targetEl, btn, 'Copied');
      } else if (targetEl && !targetEl.value) {
        status.textContent = 'No value available to copy. Click "Create a new passphrase" below.';
      }
    });
  });

  card.querySelectorAll('[data-toggle-target]').forEach(btn => {
    btn.addEventListener('click', () => {
      const targetId = btn.dataset.toggleTarget;
      const targetEl = document.getElementById(targetId);
      if (targetEl && targetEl.value) {
        const isPassword = targetEl.type === 'password';
        targetEl.type = isPassword ? 'text' : 'password';
        btn.textContent = isPassword ? 'Hide' : 'Show';
      }
    });
  });
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
    rtmpsRadio.disabled = !data.rtmps_available;
    document.getElementById('program-rtmps-unavailable').hidden = data.rtmps_available;
    const srtRadio = document.getElementById('program-mode-srt');
    if (srtRadio) {
      srtRadio.disabled = !data.srt_available;
      const srtUnavailable = document.getElementById('program-srt-unavailable');
      if (srtUnavailable) srtUnavailable.hidden = data.srt_available;
    }
    applyMode(selectedMode());
    document.getElementById('program-endpoint').value = data.endpoint;
    document.getElementById('program-rtmps-server').value = data.rtmps_server;
    updateSrtToolFields(data);
    renderStatus(data);
    card.querySelectorAll('[data-ingest-action]').forEach(button => {
      const action = button.dataset.ingestAction;
      const relayBlocked = action === 'enable' && selectedMode() === 'rtmps_relay' && !data.relay_available;
      const rtmpsBlocked = action === 'enable' && selectedMode() === 'rtmps' && !data.rtmps_available;
      const srtBlocked = action === 'enable' && selectedMode() === 'srt' && !data.srt_available;
      button.disabled = (['enable', 'rotate'].includes(action) && !data.available) || relayBlocked || rtmpsBlocked || srtBlocked ||
        (['rotate', 'revoke', 'disable'].includes(action) && !data.enabled) ||
        (action === 'promote_primary' && (!data.enabled || data.active_feed !== 'backup'));
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
  const isSrt = Boolean(data.passphrase);
  const streamKey = Boolean(data.stream_key);
  if (isSrt) {
    savePassphrases(data.passphrase, data.backup_passphrase || '');
    updateSrtToolFields(latest || {});
    secret.value = data.passphrase;
    secret.dataset.kind = 'passphrase';
    document.getElementById('program-secret-label').textContent = 'SRT Passphrase';
    document.getElementById('program-secret-help').textContent =
      'Copy the passphrase below or copy the complete URL above for OBS. It is shown only once and expires in 30 days.';
  } else {
    secret.value = streamKey ? data.stream_key : data.secret;
    secret.dataset.kind = streamKey ? 'stream_key' : 'token';
    document.getElementById('program-secret-label').textContent = streamKey ? 'Stream key' : 'Bearer token';
    document.getElementById('program-secret-help').textContent = streamKey
      ? 'Paste this as the Stream Key in OBS or your streaming tool. Copy it now: it is shown only once and expires in 30 days.'
      : 'Paste this into OBS as the Bearer Token. It expires in 30 days.';
  }
  const backupSecretInput = document.getElementById('program-backup-secret');
  const backupSecretRow = document.getElementById('program-backup-secret-row');
  if (data.backup_passphrase && backupSecretInput && backupSecretRow) {
    backupSecretInput.value = data.backup_passphrase;
    backupSecretRow.hidden = false;
  } else if (backupSecretRow) {
    backupSecretRow.hidden = true;
    if (backupSecretInput) backupSecretInput.value = '';
  }
  document.getElementById('program-secret-panel').hidden = false;
  updateFullUrl();
}

card.querySelectorAll('input[name="program-mode"]').forEach(radio => {
  radio.addEventListener('change', () => {
    modeChosen = true;
    try { localStorage.setItem('voxbento_program_mode_' + card.dataset.url, radio.value); } catch {}
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
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfToken()
        },
        body: JSON.stringify({action, sync_offset_ms: Number(offset.value), mode}),
      });
      const data = await readError(response);
      if (['revoke', 'disable'].includes(action)) {
        clearSavedPassphrases();
        updateSrtToolFields(latest || {});
      }
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
    method: 'PUT', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken()},
    body: JSON.stringify({server_url: relayServer.value.trim() || null, stream_key: relayKey.value.trim()}),
  }), 'YouTube destination update failed');
  if (saved) relayKey.value = '';
});

document.getElementById('program-relay-clear').addEventListener('click', async () => {
  if (destinationPending || busy) return;
  if (!window.confirm('Remove the saved YouTube stream key? Forwarding stops and YouTube stops receiving this room.')) return;
  await changeDestination(() => fetch(card.dataset.relayUrl, {method: 'DELETE', headers: {'X-CSRF-Token': csrfToken()}}), 'YouTube destination removal failed');
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
const revealBackupBtn = document.getElementById('program-reveal-backup');
const backupSecretInput = document.getElementById('program-backup-secret');
if (revealBackupBtn && backupSecretInput) {
  revealBackupBtn.addEventListener('click', () => {
    backupSecretInput.type = backupSecretInput.type === 'password' ? 'text' : 'password';
    revealBackupBtn.textContent = backupSecretInput.type === 'password' ? 'Show' : 'Hide';
  });
}
const copyBackupBtn = document.getElementById('program-copy-backup');
if (copyBackupBtn && backupSecretInput) {
  copyBackupBtn.addEventListener('click', async () => {
    await copyField(backupSecretInput, copyBackupBtn, 'Copied');
  });
}
const dismissBtn = document.getElementById('program-dismiss');
if (dismissBtn) {
  dismissBtn.addEventListener('click', () => {
    secret.value = '';
    fullUrl.value = '';
    if (backupSecretInput) backupSecretInput.value = '';
    const backupSecretRow = document.getElementById('program-backup-secret-row');
    if (backupSecretRow) backupSecretRow.hidden = true;
    document.getElementById('program-secret-panel').hidden = true;
  });
}
document.getElementById('program-copy-endpoint').addEventListener('click', async () => {
  await copyField(document.getElementById('program-endpoint'), document.getElementById('program-copy-endpoint'), 'URL copied');
});
document.getElementById('program-copy-rtmps').addEventListener('click', async () => {
  await copyField(document.getElementById('program-rtmps-server'), document.getElementById('program-copy-rtmps'), 'Server copied');
});
const copySrtBtn = document.getElementById('program-copy-srt');
if (copySrtBtn) {
  copySrtBtn.addEventListener('click', async () => {
    await copyField(document.getElementById('program-srt-server'), copySrtBtn, 'URL copied');
  });
}
const copySrtBackupBtn = document.getElementById('program-copy-srt-backup');
if (copySrtBackupBtn) {
  copySrtBackupBtn.addEventListener('click', async () => {
    await copyField(document.getElementById('program-srt-backup'), copySrtBackupBtn, 'URL copied');
  });
}
const backupToggle = document.getElementById('program-backup-toggle');
if (backupToggle) {
  backupToggle.addEventListener('change', async () => {
    busy = true;
    try {
      const response = await fetch(card.dataset.url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-CSRF-Token': csrfToken()
        },
        body: JSON.stringify({
          action: 'toggle_backup',
          backup_enabled: backupToggle.checked,
          mode: selectedMode()
        }),
      });
      const data = await readError(response);
      const backupSecretInput = document.getElementById('program-backup-secret');
      const backupSecretRow = document.getElementById('program-backup-secret-row');
      if (data?.backup_passphrase) {
        savePassphrases(undefined, data.backup_passphrase);
        if (backupSecretInput && backupSecretRow) {
          backupSecretInput.value = data.backup_passphrase;
          backupSecretRow.hidden = false;
          document.getElementById('program-secret-panel').hidden = false;
        }
      } else if (!backupToggle.checked) {
        savePassphrases(undefined, '');
        if (backupSecretRow) backupSecretRow.hidden = true;
        if (backupSecretInput) backupSecretInput.value = '';
      }
      updateSrtToolFields(latest || {});
    } catch (error) {
      console.error('Failed to toggle backup feed', error);
      status.textContent = error.message;
      backupToggle.checked = !backupToggle.checked;
    } finally {
      busy = false;
      await refresh();
    }
  });
}
initToolTabs();
const savedProgramMode = (function() {
  try { return localStorage.getItem('voxbento_program_mode_' + card.dataset.url); } catch { return null; }
})();
if (savedProgramMode) {
  const radio = card.querySelector(`input[name="program-mode"][value="${savedProgramMode}"]`);
  if (radio) {
    radio.checked = true;
    modeChosen = true;
  }
}
applyMode(selectedMode());
window.addEventListener('pagehide', () => { secret.value = ''; fullUrl.value = ''; if (backupSecretInput) backupSecretInput.value = ''; relayKey.value = ''; });
refresh();
const refreshTimer = setInterval(refresh, 5000);
window.addEventListener('pagehide', () => clearInterval(refreshTimer));
