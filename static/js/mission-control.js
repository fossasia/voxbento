/**
 * Mission Control — Live Booth Matrix
 *
 * Connects to each booth's WebSocket as a silent observer (no booth:join).
 * Coordinators / admins can:
 *   - Monitor all configured booths for the event in real-time
 *   - See which interpreters are present and speaking
 *   - Listen in via a per-booth audio volume slider (WHEP)
 *   - Toggle Go Live / Stop per booth
 */

const config = window.MISSION_CONTROL_CONFIG;
const grid = document.getElementById('booth-grid');

/** @type {Map<string, {state:object, ws:WebSocket|null, whep:object|null, audioElement:HTMLAudioElement|null, currentVolume:number}>} */
const boothMap = new Map();

function init() {
  if (!config.initialBooths || config.initialBooths.length === 0) {
    const emptyDiv = document.createElement('div');
    emptyDiv.className = 'card';
    emptyDiv.style.cssText = 'padding:2rem;text-align:center;grid-column:1/-1;';
    const p = document.createElement('p');
    p.style.cssText = 'color:var(--color-muted);margin:0;';
    p.textContent = 'No booths are configured for this event yet.';
    emptyDiv.appendChild(p);
    grid.innerHTML = '';
    grid.appendChild(emptyDiv);
    return;
  }

  config.initialBooths.forEach(b => {
    boothMap.set(b.booth_id, {
      state: b,
      ws: null,
      whep: null,
      audioElement: null,
      currentVolume: 0,
    });
    renderCard(b.booth_id);
    connectWs(b.booth_id);
  });
}

// ---------------------------------------------------------------------------
// WebSocket — silent observer mode (no booth:join participant registration)
// ---------------------------------------------------------------------------

function connectWs(boothId) {
  const entry = boothMap.get(boothId);
  if (!entry) return;

  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${proto}//${window.location.host}/ws/booth/${boothId}`;
  const ws = new WebSocket(wsUrl);
  entry.ws = ws;

  ws.onopen = () => {
    renderCard(boothId);
  };

  ws.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch { return; }

    if (data.type === 'booth:state') {
      entry.state = data.state;
      renderCard(boothId);
    }
    // Silently ignore booth:joined — we do not join as a participant.
  };

  ws.onclose = () => {
    entry.ws = null;
    renderCard(boothId);
    setTimeout(() => connectWs(boothId), 3000);
  };

  ws.onerror = () => ws.close();
}

// ---------------------------------------------------------------------------
// Go Live / Stop — broadcast-unlock toggle without joining as participant
// ---------------------------------------------------------------------------

function setBroadcastLive(boothId, goLive) {
  const entry = boothMap.get(boothId);
  if (!entry || !entry.ws || entry.ws.readyState !== WebSocket.OPEN) {
    showToast(`Cannot reach booth — WebSocket not connected.`, 'error');
    return;
  }
  entry.ws.send(JSON.stringify({
    type: 'booth:set-broadcast-unlocked',
    unlocked: goLive,
  }));
}

// ---------------------------------------------------------------------------
// Audio monitor — lazy WHEP start/stop
// ---------------------------------------------------------------------------

function handleVolumeChange(boothId, volume) {
  const entry = boothMap.get(boothId);
  if (!entry) return;
  const vol = parseInt(volume, 10);

  if (vol > 0 && !entry.whep) {
    const whepUrl = `${config.whipBase}/${entry.state.mediamtx_path}/whep`;
    const whep = window.createWhepClient();
    whep.start({ whepUrl, audioEl: entry.audioElement });
    entry.whep = whep;
  } else if (vol === 0 && entry.whep) {
    entry.whep.stop();
    entry.whep = null;
  }

  if (entry.audioElement) {
    entry.audioElement.volume = vol / 100;
  }
}

// ---------------------------------------------------------------------------
// Card rendering
// ---------------------------------------------------------------------------

function renderCard(boothId) {
  const entry = boothMap.get(boothId);
  if (!entry) return;
  const s = entry.state;

  let card = document.getElementById(`mc-card-${boothId}`);
  if (!card) {
    card = document.createElement('div');
    card.id = `mc-card-${boothId}`;
    card.className = 'card';
    card.style.cssText = 'padding:1.25rem;display:flex;flex-direction:column;gap:1rem;';

    const audio = document.createElement('audio');
    audio.autoplay = true;
    audio.hidden = true;
    entry.audioElement = audio;
    card.appendChild(audio);

    grid.appendChild(card);
  }

  // Interpreters and rows
  const LIVE_ROLES = new Set(['interpreter', 'coordinator', 'event_admin', 'super_admin']);
  const interpreters = (s.participants || []).filter(p => LIVE_ROLES.has(p.role));

  // Broadcast control
  const isWsOpen = entry.ws && entry.ws.readyState === WebSocket.OPEN;
  const isLive = s.broadcast_unlocked;
  let btnClass = isLive ? 'btn-danger' : 'btn-success';
  let btnText = isLive ? 'Stop' : 'Go Live';
  let isDisabled = false;
  
  if (!isWsOpen) {
    btnClass = 'btn-outline';
    btnText = 'Connecting...';
    isDisabled = true;
  }

  card.innerHTML = '';

  const headerDiv = document.createElement('div');
  headerDiv.style.cssText = 'display:flex;justify-content:space-between;align-items:flex-start;';
  
  const titleDiv = document.createElement('div');
  const h3 = document.createElement('h3');
  h3.style.margin = '0';
  h3.textContent = s.language + ' ';
  const langCodeSpan = document.createElement('span');
  langCodeSpan.style.cssText = 'font-size:0.8rem;font-weight:normal;color:var(--color-muted);';
  langCodeSpan.textContent = `(${s.language_code})`;
  h3.appendChild(langCodeSpan);
  titleDiv.appendChild(h3);
  
  const roomDiv = document.createElement('div');
  roomDiv.style.cssText = 'font-size:0.8rem;color:var(--color-muted);margin-top:0.2rem;';
  roomDiv.textContent = s.room_name ? s.room_name : `Room ID: ${s.room_id ?? 'N/A'}`;
  titleDiv.appendChild(roomDiv);
  headerDiv.appendChild(titleDiv);
  
  const ingestSpan = document.createElement('span');
  if (s.ingest_status === 'connected') {
    ingestSpan.className = 'status-badge status-success';
    ingestSpan.textContent = '● Ingest Live';
  } else {
    ingestSpan.className = 'status-badge';
    ingestSpan.textContent = '○ No Ingest';
  }
  headerDiv.appendChild(ingestSpan);
  card.appendChild(headerDiv);

  const interpContainer = document.createElement('div');
  interpContainer.style.cssText = 'border-top:1px solid var(--color-border);padding-top:0.75rem;';
  const interpH4 = document.createElement('h4');
  interpH4.style.cssText = 'margin:0 0 0.5rem 0;font-size:0.8rem;text-transform:uppercase;color:var(--color-muted);';
  interpH4.textContent = 'Interpreters';
  interpContainer.appendChild(interpH4);

  if (interpreters.length) {
    interpreters.forEach(p => {
      const isActive = p.participant_id === s.active_interpreter_id;
      const isMuted = !p.mic_active;
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;align-items:center;justify-content:space-between;font-size:0.85rem;padding:0.25rem 0;';
      
      const nameSpan = document.createElement('span');
      if (isActive) {
        nameSpan.style.cssText = 'font-weight:bold;color:var(--color-primary);';
      }
      nameSpan.textContent = p.display_name;
      if (isActive) {
        const activeEm = document.createElement('em');
        activeEm.style.fontWeight = 'normal';
        activeEm.textContent = ' (active)';
        nameSpan.appendChild(activeEm);
      }
      row.appendChild(nameSpan);

      const statusSpan = document.createElement('span');
      statusSpan.className = `status-badge ${isMuted ? '' : 'status-success'}`;
      statusSpan.textContent = isMuted ? 'Muted' : '▶ Speaking';
      row.appendChild(statusSpan);
      interpContainer.appendChild(row);
    });
  } else {
    const noInterpDiv = document.createElement('div');
    noInterpDiv.style.cssText = 'font-size:0.85rem;color:var(--color-muted);';
    noInterpDiv.textContent = 'No interpreters present';
    interpContainer.appendChild(noInterpDiv);
  }
  card.appendChild(interpContainer);

  const volContainer = document.createElement('div');
  volContainer.style.cssText = 'border-top:1px solid var(--color-border);padding-top:0.75rem;display:flex;flex-direction:column;gap:0.5rem;';
  const volHeader = document.createElement('div');
  volHeader.style.cssText = 'display:flex;align-items:center;justify-content:space-between;';
  const volLabel = document.createElement('label');
  volLabel.style.cssText = 'font-size:0.8rem;font-weight:500;';
  volLabel.textContent = 'Monitor Audio';
  volHeader.appendChild(volLabel);
  const volSpan = document.createElement('span');
  volSpan.id = `mc-vol-lbl-${boothId}`;
  volSpan.style.cssText = 'font-size:0.8rem;color:var(--color-muted);';
  volSpan.textContent = entry.currentVolume > 0 ? `${entry.currentVolume}%` : '0%';
  volHeader.appendChild(volSpan);
  volContainer.appendChild(volHeader);
  
  const volInput = document.createElement('input');
  volInput.type = 'range';
  volInput.id = `mc-vol-${boothId}`;
  volInput.min = '0';
  volInput.max = '100';
  volInput.value = entry.currentVolume > 0 ? entry.currentVolume.toString() : '0';
  volInput.style.width = '100%';
  volContainer.appendChild(volInput);
  card.appendChild(volContainer);

  const btnContainer = document.createElement('div');
  btnContainer.style.cssText = 'margin-top:auto;padding-top:1rem;';
  const btn = document.createElement('button');
  btn.id = `mc-live-${boothId}`;
  btn.className = `btn ${btnClass}`;
  btn.style.cssText = 'width:100%;font-weight:600;';
  if (isDisabled) {
    btn.disabled = true;
  }
  btn.textContent = btnText;
  btnContainer.appendChild(btn);
  card.appendChild(btnContainer);

  card.appendChild(entry.audioElement);

  volInput.addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    volSpan.textContent = `${val}%`;
    entry.currentVolume = val;
  });

  volInput.addEventListener('change', (e) => {
    handleVolumeChange(boothId, e.target.value);
  });

  btn.addEventListener('click', () => {
    setBroadcastLive(boothId, !isLive);
  });
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function showToast(msg, type = 'info') {
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = `position:fixed;bottom:1.5rem;right:1.5rem;padding:0.75rem 1.25rem;border-radius:6px;font-size:0.9rem;z-index:9999;background:${type === 'error' ? '#c0392b' : '#2c3e50'};color:#fff;`;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

document.addEventListener('DOMContentLoaded', init);
