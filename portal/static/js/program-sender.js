const card = document.getElementById('program-sender');
const deviceSelect = document.getElementById('sender-device');
const startButton = document.getElementById('sender-start');
const muteButton = document.getElementById('sender-mute');
const stopButton = document.getElementById('sender-stop');
const testButton = document.getElementById('sender-test');
const recordButton = document.getElementById('sender-record');
const playback = document.getElementById('sender-playback');
const statusLine = document.getElementById('sender-status');
const levelLine = document.getElementById('sender-level');
const stateBadge = document.getElementById('sender-state-badge');
const meterFill = document.getElementById('sender-meter-fill');
const unavailable = document.getElementById('sender-unavailable');

const stateLabels = {
  idle: 'Not sending',
  testing: 'Sound check',
  connecting: 'Connecting',
  live: 'Live',
  muted: 'Muted',
  reconnecting: 'Reconnecting',
  'no-audio': 'No audio detected',
  'permission-denied': 'Microphone blocked',
  error: 'Error',
  stopped: 'Stopped',
};

const MAX_RETRY_DELAY_MS = 30000;
const DISCONNECT_GRACE_MS = 4000;
const RECORD_MS = 5000;

let stream = null;
// The meter reads a clone so it keeps showing the input while the sent track is muted.
let meterStream = null;
let pc = null;
let audioContext = null;
let meterTimer = null;
let resourceUrl = null;
let sessionSecret = null;
let sessionActive = false;
let sending = false;
let testing = false;
let muted = false;
let connecting = false;
let attempt = 0;
let retryTimer = null;
let disconnectTimer = null;
let silentSince = null;
let recentPeak = 0;
let wakeLock = null;
let recorder = null;
// Changed only by an explicit pick, so recovering from an unplugged USB input waits for that input.
let preferredDevice = savedDevice();
// Bumped by stop(); a connect() that resumes under an older generation is abandoned.
let generation = 0;

function setState(state, message) {
  stateBadge.textContent = stateLabels[state] || state;
  stateBadge.dataset.state = state;
  if (message) statusLine.textContent = message;
}

function supported() {
  return Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.RTCPeerConnection);
}

function savedDevice() {
  try {
    return window.localStorage.getItem(card.dataset.storageKey) || '';
  } catch (error) {
    return '';
  }
}

function saveDevice(deviceId) {
  try {
    window.localStorage.setItem(card.dataset.storageKey, deviceId);
  } catch (error) {
    // Storage can be blocked (private window); the default input still works.
    console.warn('Could not remember the audio input', error);
  }
}

async function listDevices() {
  try {
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs = devices.filter(device => device.kind === 'audioinput');
    const chosen = preferredDevice || deviceSelect.value;
    deviceSelect.innerHTML = '';
    inputs.forEach((device, index) => {
      const option = document.createElement('option');
      option.value = device.deviceId;
      option.textContent = device.label || `Microphone ${index + 1}`;
      deviceSelect.appendChild(option);
    });
    if (chosen && inputs.some(device => device.deviceId === chosen)) deviceSelect.value = chosen;
  } catch (error) {
    console.error('Could not list audio inputs', error);
  }
}

function releaseInput() {
  stopMeter();
  if (stream) stream.getTracks().forEach(track => track.stop());
  if (meterStream) meterStream.getTracks().forEach(track => track.stop());
  stream = null;
  meterStream = null;
}

/** @throws {DOMException} when the input cannot be opened or permission is denied */
async function openInput() {
  const deviceId = deviceSelect.value;
  // Program audio must bypass voice DSP, which pumps levels and hurts music and STT.
  const audioConstraints = {echoCancellation: false, noiseSuppression: false, autoGainControl: false};
  if (deviceId) audioConstraints.deviceId = {exact: deviceId};
  const next = await navigator.mediaDevices.getUserMedia({audio: audioConstraints});
  releaseInput();
  stream = next;
  const track = stream.getAudioTracks()[0];
  track.enabled = !muted;
  track.addEventListener('ended', handleInputLost);
  meterStream = new MediaStream([track.clone()]);
  startMeter();
  await listDevices();
  if (deviceSelect.value) saveDevice(deviceSelect.value);
  const sender = pc?.getSenders().find(item => item.track?.kind === 'audio' || item.track === null);
  if (sender) await sender.replaceTrack(track);
}

async function handleInputLost() {
  if (!sending && !testing) return;
  setState('reconnecting', 'The audio input disappeared. Waiting for it to come back…');
  await listDevices();
  // Wait for the chosen input rather than silently sending whichever input is left.
  if (preferredDevice && deviceSelect.value !== preferredDevice) {
    window.setTimeout(handleInputLost, 2000);
    return;
  }
  try {
    await openInput();
    setState(sending ? (muted ? 'muted' : 'live') : 'testing', 'Audio input restored.');
  } catch (error) {
    console.warn('Audio input still unavailable; retrying', error);
    window.setTimeout(handleInputLost, 2000);
  }
}

function startMeter() {
  audioContext = new (window.AudioContext || window.webkitAudioContext)();
  const source = audioContext.createMediaStreamSource(meterStream);
  const analyser = audioContext.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const buffer = new Uint8Array(analyser.fftSize);
  meterTimer = window.setInterval(() => {
    analyser.getByteTimeDomainData(buffer);
    let sum = 0;
    let peak = 0;
    for (const sample of buffer) {
      const value = (sample - 128) / 128;
      sum += value ** 2;
      peak = Math.max(peak, Math.abs(value));
    }
    const rms = Math.sqrt(sum / buffer.length);
    meterFill.style.width = `${Math.min(100, Math.round(rms * 180))}%`;
    // Decay slowly so the level hint describes the last second or so, not one frame.
    recentPeak = Math.max(peak, recentPeak * 0.92);
    describeLevel(rms);
    detectSilence(rms);
  }, 120);
}

function describeLevel(rms) {
  let level = 'good';
  let text = 'Good level.';
  if (recentPeak > 0.98) {
    level = 'clipping';
    text = 'Too loud — the input is clipping. Turn the sound-desk send down.';
  } else if (rms < 0.01 && recentPeak < 0.05) {
    level = 'silent';
    text = 'No signal. Check the cable and the selected input.';
  } else if (recentPeak < 0.2) {
    level = 'quiet';
    text = 'Quiet. Turn the sound-desk send up if speech is hard to hear.';
  }
  if (muted) text = `Muted — listeners hear silence. Input: ${text.charAt(0).toLowerCase()}${text.slice(1)}`;
  levelLine.dataset.level = level;
  levelLine.textContent = text;
}

function detectSilence(rms) {
  const state = stateBadge.dataset.state;
  if (state !== 'live' && state !== 'no-audio') return;
  const now = Date.now();
  if (rms > 0.02) {
    silentSince = null;
    if (state === 'no-audio') setState('live', 'Sending program audio.');
  } else {
    silentSince = silentSince || now;
    if (now - silentSince > 4000 && state === 'live') {
      setState('no-audio', 'No audio from the selected input. Check the sound-desk feed and levels.');
    }
  }
}

function stopMeter() {
  if (meterTimer) window.clearInterval(meterTimer);
  meterTimer = null;
  if (audioContext) audioContext.close().catch(error => console.warn('Could not close the meter', error));
  audioContext = null;
  recentPeak = 0;
  meterFill.style.width = '0%';
  levelLine.dataset.level = 'none';
  levelLine.textContent = 'Start a sound check to see the input level.';
}

function releaseHeaders() {
  // Bind the release to this session's secret so the server ignores a stale DELETE.
  return sessionSecret ? {'X-Program-Session': sessionSecret} : {};
}

class SenderError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

class StaleConnect extends Error {}

/** @throws {SenderError} when the server refuses a session */
async function mintSession() {
  // No sync_offset_ms: the server keeps the room's saved YouTube sync delay.
  const response = await fetch(card.dataset.mintUrl, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: '{}',
    cache: 'no-store',
  });
  let data;
  try {
    data = await response.json();
  } catch (error) {
    if (response.ok) throw error;
  }
  if (!response.ok) {
    const detail = typeof data?.detail === 'string' ? data.detail : `Could not start session (HTTP ${response.status})`;
    throw new SenderError(detail, response.status);
  }
  // The caller adopts data.secret only if its attempt is still current.
  return data;
}

/**
 * Builds the peer locally and only publishes it to `pc` once negotiation finishes for
 * the current generation, so a stop() mid-negotiation cannot leave a peer or WHIP source behind.
 * @throws {SenderError} when MediaMTX rejects the offer
 * @throws {StaleConnect} when stop() ran during negotiation
 */
async function publish(endpoint, secret, gen) {
  const peer = new RTCPeerConnection({iceServers: []});
  let location = null;
  try {
    stream.getAudioTracks().forEach(track => peer.addTrack(track, stream));
    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    if (gen !== generation) throw new StaleConnect();
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type': 'application/sdp', 'Authorization': `Bearer ${secret}`},
      body: offer.sdp,
    });
    if (response.ok) location = response.headers.get('Location') || null;
    if (gen !== generation) throw new StaleConnect();
    if (!response.ok) throw new SenderError(`Floor rejected the stream (HTTP ${response.status})`, response.status);
    const answer = await response.text();
    if (gen !== generation) throw new StaleConnect();
    await peer.setRemoteDescription({type: 'answer', sdp: answer});
    if (gen !== generation) throw new StaleConnect();
  } catch (error) {
    peer.close();
    if (location) fetch(location, {method: 'DELETE', keepalive: true}).catch(cleanupError => console.warn('WHIP release failed', cleanupError));
    throw error;
  }
  pc = peer;
  resourceUrl = location;
  pc.addEventListener('connectionstatechange', handleConnectionChange);
  handleConnectionChange();
}

function liveState() {
  if (muted) setState('muted', 'Muted. Listeners hear silence until you unmute; the floor stays yours.');
  else setState('live', 'Sending program audio. You can switch tabs — keep this one open.');
}

function handleConnectionChange() {
  if (!pc) return;
  const state = pc.connectionState;
  window.clearTimeout(disconnectTimer);
  if (state === 'connected') {
    // ICE recovered on its own; a rebuild queued during the drop would tear down a healthy peer.
    window.clearTimeout(retryTimer);
    retryTimer = null;
    attempt = 0;
    if (stateBadge.dataset.state !== 'no-audio') liveState();
  } else if (state === 'failed') {
    retryAfterPeerLoss();
  } else if (state === 'disconnected') {
    // ICE often recovers from a brief blip by itself; only rebuild if it does not.
    disconnectTimer = window.setTimeout(retryAfterPeerLoss, DISCONNECT_GRACE_MS);
  }
}

function retryDelay() {
  return Math.min(MAX_RETRY_DELAY_MS, 1000 * 2 ** Math.min(attempt, 5));
}

// First retry is immediate; a peer that keeps failing before it connects backs off like a failed connect.
function retryAfterPeerLoss() {
  const delay = attempt === 0 ? 0 : retryDelay();
  attempt += 1;
  scheduleReconnect(delay);
}

function teardownPeer() {
  if (pc) {
    pc.removeEventListener('connectionstatechange', handleConnectionChange);
    pc.close();
  }
  pc = null;
}

async function releaseSession() {
  teardownPeer();
  // Snapshot and clear before awaiting, so a late resume never wipes a newer session's state.
  const resource = resourceUrl;
  const secret = sessionActive ? sessionSecret : null;
  resourceUrl = null;
  sessionActive = false;
  sessionSecret = null;
  if (resource) {
    try {
      await fetch(resource, {method: 'DELETE'});
    } catch (error) {
      console.warn('Stale WHIP resource already gone', error);
    }
  }
  if (secret) await discardSession(secret);
}

async function discardSession(secret) {
  try {
    await fetch(card.dataset.mintUrl, {method: 'DELETE', cache: 'no-store', keepalive: true, headers: {'X-Program-Session': secret}});
  } catch (error) {
    console.warn('Could not release the previous sender session', error);
  }
}

function scheduleReconnect(delayMs) {
  if (!sending || retryTimer) return;
  retryTimer = window.setTimeout(() => {
    retryTimer = null;
    connect();
  }, delayMs);
}

async function connect() {
  if (!sending || connecting) return;
  const gen = generation;
  connecting = true;
  if (attempt > 0) setState('reconnecting', `Connection dropped. Reconnecting (attempt ${attempt})…`);
  else setState('connecting', 'Connecting to the floor…');
  try {
    // Drop the stale WHIP source and release the old session so the retry claims the floor cleanly.
    await releaseSession();
    if (gen !== generation) return;
    const session = await mintSession();
    if (gen !== generation) {
      // Stopped while minting: give back the session nobody will use.
      await discardSession(session.secret);
      return;
    }
    sessionSecret = session.secret;
    sessionActive = true;
    await publish(session.endpoint, session.secret, gen);
  } catch (error) {
    // stop() already released this attempt's session and reset the UI.
    if (gen !== generation) return;
    console.error('Program sender could not connect', error);
    connecting = false;
    if (error instanceof SenderError && error.status >= 400 && error.status < 500 && error.status !== 429) {
      // Refused, not dropped (sign-in expired, another source owns the floor, bad request). Retrying would not help.
      const hint = [401, 403, 404].includes(error.status)
        ? 'Your session is no longer allowed to send. Sign in again and restart.'
        : error.message;
      await stop(true);
      setState('error', hint);
      return;
    }
    attempt += 1;
    const delay = retryDelay();
    setState('reconnecting', `Could not reach the floor. Retrying in ${Math.round(delay / 1000)} s (attempt ${attempt})…`);
    scheduleReconnect(delay);
    return;
  }
  if (gen === generation) connecting = false;
}

async function keepAwake() {
  if (!sending || !('wakeLock' in navigator) || document.visibilityState !== 'visible') return;
  try {
    wakeLock = await navigator.wakeLock.request('screen');
  } catch (error) {
    console.warn('Screen wake lock unavailable; set the computer not to sleep', error);
  }
}

async function start() {
  if (!supported()) {
    setState('error', 'This browser cannot capture audio or publish WebRTC.');
    return;
  }
  startButton.disabled = true;
  setState('connecting', 'Requesting the audio input…');
  try {
    if (!stream) await openInput();
  } catch (error) {
    startButton.disabled = false;
    reportInputError(error);
    return;
  }
  sending = true;
  // After sending is set, so ending the sound check keeps the input open.
  stopTest(false);
  attempt = 0;
  startButton.hidden = true;
  startButton.disabled = false;
  muteButton.hidden = false;
  stopButton.hidden = false;
  recordButton.disabled = true;
  testButton.disabled = true;
  keepAwake();
  await connect();
}

async function stop(keepState) {
  generation += 1;
  sending = false;
  connecting = false;
  window.clearTimeout(retryTimer);
  window.clearTimeout(disconnectTimer);
  retryTimer = null;
  teardownPeer();
  releaseInput();
  // keepalive lets these cleanup requests complete even when the page is unloading.
  if (resourceUrl) {
    fetch(resourceUrl, {method: 'DELETE', keepalive: true}).catch(error => console.warn('WHIP release failed', error));
    resourceUrl = null;
  }
  // Release only a session we actually opened, bound to its secret so a stray or
  // delayed DELETE can never disconnect an encoder or a newer session.
  if (sessionActive) {
    fetch(card.dataset.mintUrl, {method: 'DELETE', cache: 'no-store', keepalive: true, headers: releaseHeaders()})
      .catch(error => console.warn('Sender session release failed', error));
    sessionActive = false;
  }
  sessionSecret = null;
  if (wakeLock) wakeLock.release().catch(error => console.warn('Wake lock release failed', error));
  wakeLock = null;
  setMuted(false);
  muteButton.hidden = true;
  stopButton.hidden = true;
  startButton.hidden = false;
  recordButton.disabled = false;
  testButton.disabled = false;
  if (!keepState) setState('stopped', 'Stopped sending. The floor is free for another source.');
}

function setMuted(value) {
  muted = value;
  stream?.getAudioTracks().forEach(track => { track.enabled = !muted; });
  muteButton.textContent = muted ? 'Unmute' : 'Mute';
  muteButton.setAttribute('aria-pressed', String(muted));
  muteButton.classList.toggle('btn-primary', muted);
  muteButton.classList.toggle('btn-outline', !muted);
}

function toggleMute() {
  setMuted(!muted);
  silentSince = null;
  if (sending && pc?.connectionState === 'connected') liveState();
}

function reportInputError(error) {
  if (error && (error.name === 'NotAllowedError' || error.name === 'SecurityError')) {
    setState('permission-denied', 'Microphone access was blocked. Allow it in the browser and try again.');
  } else {
    console.error('Could not open the audio input', error);
    setState('error', 'Could not open the selected audio input.');
  }
}

async function startTest() {
  try {
    await openInput();
  } catch (error) {
    reportInputError(error);
    return;
  }
  testing = true;
  testButton.textContent = 'Stop test';
  setState('testing', 'Sound check running. Nothing is sent to listeners.');
}

function stopTest(resetState) {
  if (!testing) return;
  testing = false;
  testButton.textContent = 'Test input';
  if (!sending) releaseInput();
  if (resetState) setState('idle', 'Sound check finished. Start sending when you are ready.');
}

async function recordCheck() {
  if (!window.MediaRecorder) {
    setState('error', 'This browser cannot record a sound check.');
    return;
  }
  const wasTesting = testing;
  if (!stream) {
    try {
      await openInput();
    } catch (error) {
      reportInputError(error);
      return;
    }
  }
  recordButton.disabled = true;
  startButton.disabled = true;
  playback.hidden = true;
  const chunks = [];
  recorder = new MediaRecorder(meterStream);
  recorder.addEventListener('dataavailable', event => chunks.push(event.data));
  recorder.addEventListener('stop', () => {
    if (playback.src) URL.revokeObjectURL(playback.src);
    playback.src = URL.createObjectURL(new Blob(chunks, {type: recorder.mimeType}));
    playback.hidden = false;
    playback.play().catch(error => console.warn('Autoplay blocked; use the player controls', error));
    recorder = null;
    recordButton.disabled = false;
    recordButton.textContent = 'Record 5 s and play back';
    startButton.disabled = false;
    if (!wasTesting && !sending) releaseInput();
    setState(testing ? 'testing' : 'idle', 'Playing back the recording. Use headphones to avoid feedback.');
  });
  recorder.start();
  setState('testing', 'Recording 5 seconds — speak or play program audio now…');
  recordButton.textContent = 'Recording…';
  window.setTimeout(() => recorder?.stop(), RECORD_MS);
}

async function changeDevice() {
  preferredDevice = deviceSelect.value;
  saveDevice(deviceSelect.value);
  if (!stream) return;
  try {
    // Swaps the live track in place; listeners hear the new input without a reconnect.
    await openInput();
  } catch (error) {
    reportInputError(error);
  }
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
  testButton.disabled = true;
  recordButton.disabled = true;
} else {
  navigator.mediaDevices.addEventListener?.('devicechange', listDevices);
  listDevices();
  checkAvailability();
  startButton.addEventListener('click', start);
  stopButton.addEventListener('click', () => stop(false));
  muteButton.addEventListener('click', toggleMute);
  testButton.addEventListener('click', () => (testing ? stopTest(true) : startTest()));
  recordButton.addEventListener('click', recordCheck);
  deviceSelect.addEventListener('change', changeDevice);
  window.addEventListener('online', () => {
    if (sending && pc?.connectionState !== 'connected') {
      window.clearTimeout(retryTimer);
      retryTimer = null;
      scheduleReconnect(0);
    }
  });
  // The OS drops the wake lock when the tab is hidden; take it back on return.
  document.addEventListener('visibilitychange', keepAwake);
  window.addEventListener('beforeunload', event => {
    if (sending) event.preventDefault();
  });
  // Let stop() send the secret-bound release before the secret is scrubbed.
  window.addEventListener('pagehide', () => { if (sending || sessionActive) stop(true); else sessionSecret = null; });
}
