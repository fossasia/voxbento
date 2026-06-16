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
    grid.innerHTML = '<div class="card" style="padding:2rem;text-align:center;grid-column:1/-1;"><p style="color:var(--color-muted);margin:0;">No booths are configured for this event yet.</p></div>';
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

  // Interpreter rows — show all roles that can go live (interpreter, coordinator,
  // event_admin, super_admin). Users with elevated roles may join a booth and act
  // as the active interpreter, so they must appear here too.
  const LIVE_ROLES = new Set(['interpreter', 'coordinator', 'event_admin', 'super_admin']);
  const interpreters = (s.participants || []).filter(p => LIVE_ROLES.has(p.role));

  const interpretersHtml = interpreters.length
    ? interpreters.map(p => {
        const isActive = p.participant_id === s.active_interpreter_id;
        const isMuted = !p.mic_active;
        return `
          <div style="display:flex;align-items:center;justify-content:space-between;font-size:0.85rem;padding:0.25rem 0;">
            <span style="${isActive ? 'font-weight:bold;color:var(--color-primary);' : ''}">${escHtml(p.display_name)}${isActive ? ' <em style="font-weight:normal">(active)</em>' : ''}</span>
            <span class="status-badge ${isMuted ? '' : 'status-success'}">${isMuted ? 'Muted' : '&#9654; Speaking'}</span>
          </div>`;
      }).join('')
    : '<div style="font-size:0.85rem;color:var(--color-muted);">No interpreters present</div>';

  // Broadcast control
  const isLive = s.broadcast_unlocked;
  const btnClass = isLive ? 'btn-danger' : 'btn-success';
  const btnText = isLive ? 'Stop' : 'Go Live';

  // Ingest status
  const ingestBadge = s.ingest_status === 'connected'
    ? '<span class="status-badge status-success">&#9679; Ingest Live</span>'
    : '<span class="status-badge">&#9675; No Ingest</span>';

  // Room label (populated from DB even for empty booths)
  const roomLabel = s.room_name
    ? `<div style="font-size:0.8rem;color:var(--color-muted);margin-top:0.2rem;">${escHtml(s.room_name)}</div>`
    : `<div style="font-size:0.8rem;color:var(--color-muted);margin-top:0.2rem;">Room ID: ${s.room_id ?? 'N/A'}</div>`;

  card.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;">
      <div>
        <h3 style="margin:0;">${escHtml(s.language)} <span style="font-size:0.8rem;font-weight:normal;color:var(--color-muted);">(${escHtml(s.language_code)})</span></h3>
        ${roomLabel}
      </div>
      ${ingestBadge}
    </div>

    <div style="border-top:1px solid var(--color-border);padding-top:0.75rem;">
      <h4 style="margin:0 0 0.5rem 0;font-size:0.8rem;text-transform:uppercase;color:var(--color-muted);">Interpreters</h4>
      ${interpretersHtml}
    </div>

    <div style="border-top:1px solid var(--color-border);padding-top:0.75rem;display:flex;flex-direction:column;gap:0.5rem;">
      <div style="display:flex;align-items:center;justify-content:space-between;">
        <label style="font-size:0.8rem;font-weight:500;">Monitor Audio</label>
        <span id="mc-vol-lbl-${boothId}" style="font-size:0.8rem;color:var(--color-muted);">0%</span>
      </div>
      <input type="range" id="mc-vol-${boothId}" min="0" max="100" value="0" style="width:100%;">
    </div>

    <div style="margin-top:auto;padding-top:1rem;">
      <button id="mc-live-${boothId}" class="btn ${btnClass}" style="width:100%;font-weight:600;">
        ${btnText}
      </button>
    </div>
  `;

  // Re-attach audio element (innerHTML replacement removes it from DOM)
  card.appendChild(entry.audioElement);

  // Restore volume slider after re-render
  const volInput = card.querySelector(`#mc-vol-${boothId}`);
  const volLbl = card.querySelector(`#mc-vol-lbl-${boothId}`);
  if (entry.currentVolume > 0) {
    volInput.value = entry.currentVolume;
    volLbl.textContent = `${entry.currentVolume}%`;
  }

  volInput.addEventListener('input', (e) => {
    const val = parseInt(e.target.value, 10);
    volLbl.textContent = `${val}%`;
    entry.currentVolume = val;
  });

  volInput.addEventListener('change', (e) => {
    handleVolumeChange(boothId, e.target.value);
  });

  card.querySelector(`#mc-live-${boothId}`).addEventListener('click', () => {
    setBroadcastLive(boothId, !isLive);
  });
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function escHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(msg, type = 'info') {
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = `position:fixed;bottom:1.5rem;right:1.5rem;padding:0.75rem 1.25rem;border-radius:6px;font-size:0.9rem;z-index:9999;background:${type === 'error' ? '#c0392b' : '#2c3e50'};color:#fff;`;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

document.addEventListener('DOMContentLoaded', init);
