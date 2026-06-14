const config = window.MISSION_CONTROL_CONFIG;
const grid = document.getElementById('booth-grid');
const booths = new Map();

function init() {
  config.initialBooths.forEach(b => {
    booths.set(b.booth_id, {
      state: b,
      ws: null,
      whep: null,
      audioElement: null
    });
    renderCard(b.booth_id);
    connectWs(b.booth_id);
  });
}

function connectWs(boothId) {
  const boothData = booths.get(boothId);
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  const wsUrl = `${proto}//${host}/ws/booth/${boothId}`;
  
  const ws = new WebSocket(wsUrl);
  boothData.ws = ws;

  ws.onopen = () => {
    ws.send(JSON.stringify({
      type: 'booth:join',
      display_name: 'Mission Control',
      role: 'room_coordinator',
      language: boothData.state.language,
      channel_id: boothData.state.channel_id
    }));
  };

  ws.onmessage = (e) => {
    const data = JSON.parse(e.data);
    if (data.type === 'booth:state') {
      boothData.state = data.state;
      renderCard(boothId);
    } else if (data.type === 'booth:error') {
      console.error('WebSocket Error for booth ' + boothId + ':', data.message);
      alert('Error: ' + data.message);
    }
  };

  ws.onclose = () => {
    setTimeout(() => connectWs(boothId), 3000);
  };
}

function handleVolumeChange(boothId, volume) {
  const boothData = booths.get(boothId);
  const vol = parseInt(volume, 10);
  const audioEl = boothData.audioElement;
  
  if (vol > 0 && !boothData.whep) {
    // Start WHEP
    const whepUrl = `${config.whipBase}/${boothData.state.mediamtx_path}/whep`;
    const whep = window.createWhepClient();
    whep.start({ whepUrl: whepUrl, audioEl: audioEl });
    boothData.whep = whep;
  } else if (vol === 0 && boothData.whep) {
    // Stop WHEP
    boothData.whep.stop();
    boothData.whep = null;
  }
  
  if (audioEl) {
    audioEl.volume = vol / 100;
  }
}

function toggleBroadcastLock(boothId) {
  const boothData = booths.get(boothId);
  const newState = !boothData.state.broadcast_unlocked;
  if (boothData.ws && boothData.ws.readyState === WebSocket.OPEN) {
    boothData.ws.send(JSON.stringify({
      type: 'booth:set-broadcast-unlocked',
      unlocked: newState
    }));
  }
}

function renderCard(boothId) {
  const boothData = booths.get(boothId);
  const s = boothData.state;
  
  let card = document.getElementById(`card-${boothId}`);
  if (!card) {
    card = document.createElement('div');
    card.id = `card-${boothId}`;
    card.className = 'card';
    card.style.padding = '1.25rem';
    card.style.display = 'flex';
    card.style.flexDirection = 'column';
    card.style.gap = '1rem';
    
    // Create Audio Element
    const audio = document.createElement('audio');
    audio.autoplay = true;
    audio.hidden = true;
    boothData.audioElement = audio;
    card.appendChild(audio);
    
    grid.appendChild(card);
  }
  
  // Interpreters info
  const interpreters = s.participants.filter(p => ['interpreter', 'room_coordinator', 'event_owner', 'super_admin'].includes(p.role));
  const activeInterpreter = interpreters.find(p => p.participant_id === s.active_interpreter_id);
  
  const interpretersHtml = interpreters.map(p => {
    const isActive = p.participant_id === s.active_interpreter_id;
    const isMuted = !p.mic_active;
    return `
      <div style="display: flex; align-items: center; justify-content: space-between; font-size: 0.85rem; padding: 0.25rem 0;">
        <span style="${isActive ? 'font-weight: bold; color: var(--color-primary);' : ''}">${p.display_name} ${isActive ? '(Active)' : ''}</span>
        <span class="status-badge ${isMuted ? '' : 'status-success'}">${isMuted ? 'Muted' : 'Speaking'}</span>
      </div>
    `;
  }).join('') || '<div style="font-size:0.85rem; color:var(--color-muted)">No interpreters present</div>';

  const isUnlocked = s.broadcast_unlocked;
  const toggleBtnClass = isUnlocked ? 'btn-danger' : 'btn-success';
  const toggleBtnText = isUnlocked ? 'Lock Broadcast' : 'Unlock Broadcast (Go Live)';

  card.innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: flex-start;">
      <div>
        <h3 style="margin: 0;">${s.language} <span style="font-size: 0.8rem; font-weight: normal; color: var(--color-muted);">(${s.language_code})</span></h3>
        <div style="font-size: 0.8rem; color: var(--color-muted); margin-top: 0.2rem;">Room ID: ${s.room_id || 'N/A'}</div>
      </div>
      <div class="status-badge ${s.ingest_status === 'connected' ? 'status-success' : ''}">Ingest: ${s.ingest_status}</div>
    </div>
    
    <div style="border-top: 1px solid var(--color-border); padding-top: 0.75rem;">
      <h4 style="margin: 0 0 0.5rem 0; font-size: 0.8rem; text-transform: uppercase; color: var(--color-muted);">Interpreters</h4>
      ${interpretersHtml}
    </div>
    
    <div style="border-top: 1px solid var(--color-border); padding-top: 0.75rem; display: flex; flex-direction: column; gap: 0.5rem;">
      <div style="display: flex; align-items: center; justify-content: space-between;">
        <label style="font-size: 0.8rem; font-weight: 500;">Monitor Audio</label>
        <span id="vol-lbl-${boothId}" style="font-size: 0.8rem; color: var(--color-muted);">0%</span>
      </div>
      <input type="range" id="vol-${boothId}" min="0" max="100" value="0" style="width: 100%;">
    </div>
    
    <div style="margin-top: auto; padding-top: 1rem;">
      <button id="lock-${boothId}" class="btn ${toggleBtnClass}" style="width: 100%;">
        ${toggleBtnText}
      </button>
    </div>
  `;
  
  // Re-attach audio so it doesn't get destroyed
  card.appendChild(boothData.audioElement);
  
  // Bind events
  const volInput = card.querySelector(`#vol-${boothId}`);
  const volLbl = card.querySelector(`#vol-lbl-${boothId}`);
  // Restore current volume value on re-render to avoid jumping back to 0
  if (boothData.currentVolume) {
    volInput.value = boothData.currentVolume;
    volLbl.textContent = `${boothData.currentVolume}%`;
  }
  
  volInput.addEventListener('input', (e) => {
    const val = e.target.value;
    volLbl.textContent = `${val}%`;
    boothData.currentVolume = val;
  });
  
  volInput.addEventListener('change', (e) => {
    handleVolumeChange(boothId, e.target.value);
  });
  
  card.querySelector(`#lock-${boothId}`).addEventListener('click', () => {
    toggleBroadcastLock(boothId);
  });
}

document.addEventListener('DOMContentLoaded', init);
