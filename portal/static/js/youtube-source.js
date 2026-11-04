const card = document.getElementById('youtube-source');
if (card) init(card);

function init(root) {
  const ingestUrl = root.dataset.ingestUrl;
  const routeRadios = root.querySelectorAll('input[name="youtube-route"]');
  const routePanels = root.querySelectorAll('[data-youtube-route]');
  const skipLink = root.querySelector('[data-youtube-skip-broadcast]');

  const forwardingBtn = document.getElementById('youtube-setup-forwarding');
  const forwardingStatus = document.getElementById('youtube-forwarding-status');
  const multiBtn = document.getElementById('youtube-setup-multi');
  const multiCredentials = document.getElementById('youtube-multi-credentials');

  const healthIngest = document.getElementById('youtube-health-ingest');
  const healthRelay = document.getElementById('youtube-health-relay');
  const healthCaptions = document.getElementById('youtube-health-captions');
  const healthProcessing = document.getElementById('youtube-health-processing');

  let pollTimer = null;

  routeRadios.forEach(radio => radio.addEventListener('change', onRouteChange));
  if (skipLink) skipLink.addEventListener('click', onSkip);
  if (forwardingBtn) forwardingBtn.addEventListener('click', onSetupForwarding);
  if (multiBtn) multiBtn.addEventListener('click', onSetupMulti);

  root.querySelectorAll('[data-copy-target]').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = document.getElementById(btn.dataset.copyTarget);
      if (!input) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(input.value).catch(() => {
          input.select();
          document.execCommand('copy');
        });
      } else {
        input.select();
        document.execCommand('copy');
      }
    });
  });

  restoreRouteState();

  function onSkip(event) {
    event.preventDefault();
    const target = document.getElementById('youtube-step-route');
    if (target) target.scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  function onRouteChange() {
    const selected = root.querySelector('input[name="youtube-route"]:checked');
    if (!selected) return;
    routePanels.forEach(panel => {
      panel.hidden = panel.dataset.youtubeRoute !== selected.value;
    });
  }

  function restoreRouteState() {
    fetchStatus().then(data => {
      if (!data) return;
      if (data.enabled) startPolling();
      const mode = data.mode;
      let route = null;
      if (mode === 'rtmps_relay') route = 'encoder';
      else if (mode === 'whip' && data.credential_present) route = 'multi';
      if (!route) return;
      const radio = root.querySelector(`input[name="youtube-route"][value="${route}"]`);
      if (radio && !root.querySelector('input[name="youtube-route"]:checked')) {
        radio.checked = true;
        onRouteChange();
      }
      if (route === 'multi' && data.credential_present && multiCredentials) {
        multiCredentials.hidden = false;
        const serverInput = document.getElementById('youtube-multi-server');
        const keyInput = document.getElementById('youtube-multi-key');
        if (serverInput && data.rtmps_server) serverInput.value = data.rtmps_server;
        if (keyInput && data.stream_key) keyInput.value = data.stream_key;
      }
    });
  }

  async function fetchStatus() {
    try {
      const response = await fetch(ingestUrl, {cache: 'no-store'});
      if (!response.ok) return null;
      return await response.json();
    } catch {
      return null;
    }
  }

  async function onSetupForwarding() {
    const message = 'While this is on, a VoxBento outage also interrupts your YouTube stream.';
    if (!window.confirm(message)) return;
    forwardingBtn.disabled = true;
    forwardingStatus.textContent = 'Setting up forwarding…';
    try {
      const response = await fetch(ingestUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'enable', mode: 'rtmps_relay'}),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        forwardingStatus.textContent = data.detail || 'Could not set up forwarding.';
        forwardingBtn.disabled = false;
        return;
      }
      forwardingStatus.textContent = 'Forwarding enabled. Save the YouTube stream key to start relaying.';
      startPolling();
    } catch (error) {
      console.error('Forwarding setup failed', error);
      forwardingStatus.textContent = 'Network error. Check your connection and try again.';
      forwardingBtn.disabled = false;
    }
  }

  async function onSetupMulti() {
    multiBtn.disabled = true;
    try {
      const response = await fetch(ingestUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'enable', mode: 'whip'}),
      });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        multiBtn.textContent = data.detail || 'Could not enable ingest.';
        multiBtn.disabled = false;
        return;
      }
      const status = await fetchStatus();
      if (multiCredentials && status) {
        multiCredentials.hidden = false;
        const serverInput = document.getElementById('youtube-multi-server');
        const keyInput = document.getElementById('youtube-multi-key');
        if (serverInput) serverInput.value = status.rtmps_server || '';
        if (keyInput && status.stream_key) keyInput.value = status.stream_key;
      }
      startPolling();
    } catch (error) {
      console.error('Multi-destination setup failed', error);
      multiBtn.disabled = false;
    }
  }

  function startPolling() {
    if (pollTimer) return;
    poll();
    pollTimer = window.setInterval(poll, 5000);
  }

  async function poll() {
    try {
      const response = await fetch(ingestUrl, {cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      updateHealth(data);
    } catch (error) {
      console.warn('Status poll failed', error);
    }
  }

  function updateHealth(data) {
    const health = data.health || {};
    setChip(healthIngest, health.ingest);
    setChip(healthProcessing, health.processing);
    if (health.relay !== undefined) {
      const relayRow = root.querySelector('[data-youtube-health="relay"]');
      if (relayRow) relayRow.hidden = false;
      setChip(healthRelay, health.relay);
    }
    if (data.youtube && data.youtube.captions !== undefined) {
      const captionsRow = root.querySelector('[data-youtube-health="captions"]');
      if (captionsRow) captionsRow.hidden = false;
      setChip(healthCaptions, data.youtube.captions);
    }
  }

  function setChip(element, value) {
    if (!element) return;
    if (value === undefined || value === null) return;
    if (typeof value === 'object') {
      element.dataset.state = value.state || 'unknown';
      element.textContent = value.label || value.state || '—';
    } else {
      const state = value === true ? 'ok' : value === false ? 'error' : String(value);
      element.dataset.state = state;
      element.textContent = state === 'ok' ? 'Live' : state === 'error' ? 'Down' : state;
    }
  }
}
