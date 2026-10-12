const card = document.getElementById('program-sender');
const deviceSelect = document.getElementById('sender-device');
const startButton = document.getElementById('sender-start');
const stopButton = document.getElementById('sender-stop');
const statusLine = document.getElementById('sender-status');
const stateBadge = document.getElementById('sender-state-badge');
const meterFill = document.getElementById('sender-meter-fill');
const unavailable = document.getElementById('sender-unavailable');

const stateLabels = {
  idle: 'Not sending',
  connecting: 'Connecting',
  live: 'Live',
  reconnecting: 'Reconnecting',
  'no-audio': 'No audio detected',
  'permission-denied': 'Microphone blocked',
  error: 'Error',
  stopped: 'Stopped',
};

const MAX_RECONNECTS = 5;

let stream = null;
let pc = null;
let audioContext = null;
let analyser = null;
let meterTimer = null;
let resourceUrl = null;
let sessionSecret = null;
let userStopped = false;
let reconnects = 0;
let silentSince = null;

function setState(state, message) {
  stateBadge.textContent = stateLabels[state] || state;
  stateBadge.dataset.state = state;
  if (message) statusLine.textContent = message;
}

function supported() {
  return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.RTCPeerConnection);
}

async function listDevices() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs = devices.filter(device => device.kind === 'audioinput');
    const chosen = deviceSelect.value;
    deviceSelect.innerHTML = '';
    inputs.forEach((device, index) => {
      const option = document.createElement('option');
      option.value = device.deviceId;
      option.textContent = device.label || `Microphone ${index + 1}`;
      deviceSelect.appendChild(option);
    });
    if (chosen) deviceSelect.value = chosen;
  } catch (error) {
    console.error('Could not list audio inputs', error);
  }
}

function startMeter() {
  audioContext = new (window.AudioContext || window.webkitAudioContext)();
  const source = audioContext.createMediaStreamSource(stream);
  analyser = audioContext.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const buffer = new Uint8Array(analyser.fftSize);
  meterTimer = window.setInterval(() => {
    analyser.getByteTimeDomainData(buffer);
    let sum = 0;
    for (const sample of buffer) sum += (sample - 128) ** 2;
    const rms = Math.sqrt(sum / buffer.length) / 128;
    meterFill.style.width = `${Math.min(100, Math.round(rms * 180))}%`;
    detectSilence(rms);
  }, 120);
}

function detectSilence(rms) {
  if (stateBadge.dataset.state !== 'live' && stateBadge.dataset.state !== 'no-audio') return;
  const now = Date.now();
  if (rms > 0.02) {
    silentSince = null;
    if (stateBadge.dataset.state === 'no-audio') setState('live', 'Sending program audio.');
  } else {
    silentSince = silentSince || now;
    if (now - silentSince > 4000 && stateBadge.dataset.state === 'live') {
      setState('no-audio', 'No audio from the selected input. Check the sound-desk feed and levels.');
    }
  }
}

function stopMeter() {
  if (meterTimer) window.clearInterval(meterTimer);
  meterTimer = null;
  if (audioContext) audioContext.close().catch(() => {});
  audioContext = null;
  analyser = null;
  meterFill.style.width = '0%';
}

async function mintSession() {
  const response = await fetch(card.dataset.mintUrl, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({sync_offset_ms: 0}),
    cache: 'no-store',
  });
  let data;
  try {
    data = await response.json();
  } catch (error) {
    if (response.ok) throw error;
  }
  if (!response.ok) throw new Error(typeof data?.detail === 'string' ? data.detail : `Could not start session (HTTP ${response.status})`);
  sessionSecret = data.secret;
  return data;
}

async function publish(endpoint) {
  pc = new RTCPeerConnection({iceServers: []});
  stream.getAudioTracks().forEach(track => pc.addTrack(track, stream));
  pc.addEventListener('connectionstatechange', handleConnectionChange);
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  const response = await fetch(endpoint, {
    method: 'POST',
    headers: {'Content-Type': 'application/sdp', 'Authorization': `Bearer ${sessionSecret}`},
    body: offer.sdp,
  });
  if (!response.ok) throw new Error(`Floor rejected the stream (HTTP ${response.status})`);
  resourceUrl = response.headers.get('Location') || null;
  const answer = await response.text();
  await pc.setRemoteDescription({type: 'answer', sdp: answer});
}

function handleConnectionChange() {
  if (!pc) return;
  const state = pc.connectionState;
  if (state === 'connected') {
    reconnects = 0;
    if (stateBadge.dataset.state !== 'no-audio') setState('live', 'Sending program audio.');
  } else if ((state === 'failed' || state === 'disconnected') && !userStopped) {
    reconnect();
  }
}

async function reconnect() {
  if (userStopped || reconnects >= MAX_RECONNECTS) {
    if (!userStopped) setState('error', 'Lost the connection and could not reconnect. Start again when the network recovers.');
    return;
  }
  reconnects += 1;
  setState('reconnecting', `Connection dropped. Reconnecting (attempt ${reconnects})…`);
  teardownPeer();
  try {
    const session = await mintSession();
    await publish(session.endpoint);
  } catch (error) {
    window.setTimeout(reconnect, 2000);
  }
}

function teardownPeer() {
  if (pc) {
    pc.removeEventListener('connectionstatechange', handleConnectionChange);
    pc.close();
  }
  pc = null;
}

async function start() {
  if (!supported()) {
    setState('error', 'This browser cannot capture audio or publish WebRTC.');
    return;
  }
  userStopped = false;
  reconnects = 0;
  startButton.disabled = true;
  setState('connecting', 'Requesting the audio input…');
  try {
    const deviceId = deviceSelect.value;
    stream = await navigator.mediaDevices.getUserMedia({
      audio: deviceId ? {deviceId: {exact: deviceId}} : true,
    });
  } catch (error) {
    startButton.disabled = false;
    if (error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) {
      setState('permission-denied', 'Microphone access was blocked. Allow it in the browser and try again.');
    } else {
      setState('error', 'Could not open the selected audio input.');
    }
    return;
  }
  await listDevices();
  startMeter();
  try {
    setState('connecting', 'Connecting to the floor…');
    const session = await mintSession();
    await publish(session.endpoint);
    setState('live', 'Sending program audio.');
    startButton.hidden = true;
    stopButton.hidden = false;
  } catch (error) {
    console.error('Program sender could not start', error);
    setState('error', error.message);
    await stop(true);
  } finally {
    startButton.disabled = false;
  }
}

async function stop(keepState) {
  userStopped = true;
  teardownPeer();
  stopMeter();
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  if (resourceUrl) {
    fetch(resourceUrl, {method: 'DELETE'}).catch(() => {});
    resourceUrl = null;
  }
  // Release the server-side session so the floor frees immediately.
  fetch(card.dataset.mintUrl, {method: 'DELETE', cache: 'no-store'}).catch(() => {});
  sessionSecret = null;
  stopButton.hidden = true;
  startButton.hidden = false;
  if (!keepState) setState('stopped', 'Stopped sending. The floor is free for another source.');
}

async function checkAvailability() {
  try {
    const response = await fetch(card.dataset.statusUrl, {cache: 'no-store'});
    if (!response.ok) return;
    const data = await response.json();
    if (!data.available) {
      unavailable.hidden = false;
      startButton.disabled = true;
    }
  } catch (error) {
    console.error('Program sender availability check failed', error);
  }
}

if (!supported()) {
  setState('error', 'This browser cannot capture audio or publish WebRTC.');
  startButton.disabled = true;
} else {
  navigator.mediaDevices.addEventListener?.('devicechange', listDevices);
  listDevices();
  checkAvailability();
  startButton.addEventListener('click', start);
  stopButton.addEventListener('click', () => stop(false));
  window.addEventListener('pagehide', () => { sessionSecret = null; if (stream) stop(true); });
}
