const portal = document.getElementById('interpreter-portal')

if (!portal) {
  throw new Error('Interpreter portal root element is missing.')
}

const state = {
  wsConnected: false,
  ws: null,
  jwt: null,
  joined: false,
  boothId: portal.dataset.boothId,
  token: portal.dataset.boothToken || '',
  language: portal.dataset.defaultLanguage || 'English',
  channelId: portal.dataset.defaultChannel || `${portal.dataset.boothId}-audio`,
  participantId: null,
  participants: [],
  activeInterpreterId: null,
  chatMessages: [],
  micStream: null,
  peerConnection: null,
  micMuted: false,
  ingestConnected: false,
  ingestReachable: portal.dataset.aiortcAvailable === 'true',
  defaultJitsiRoom: portal.dataset.defaultJitsi || '',
  jitsiDomain: portal.dataset.jitsiDomain || '',
  whipBase: portal.dataset.whipBase || '',
  hlsBase: portal.dataset.hlsBase || '',
  useLegacyIngest: portal.dataset.useLegacyIngest === 'true',
  usedLegacyFallback: false,
  micDeviceId: localStorage.getItem('mic-device-id') || '',
  preflight: {
    micPermission: 'pending',
    audioDevice: 'pending',
    serverReachable: 'pending',
  },
}

const elements = {
  connectionStatus: document.getElementById('connection-status'),
  monitorStatus: document.getElementById('monitor-status'),
  activeIndicator: document.getElementById('active-indicator'),
  ingestStatus: document.getElementById('ingest-status'),
  activeName: document.getElementById('active-name'),
  micState: document.getElementById('mic-state'),
  ingestReachable: document.getElementById('ingest-reachable'),
  errorBanner: document.getElementById('error-banner'),
  participantList: document.getElementById('participant-list'),
  chatLog: document.getElementById('chat-log'),
  jitsiFrame: document.getElementById('jitsi-frame'),
  jitsiUrl: document.getElementById('jitsi-url'),
  joinJitsi: document.getElementById('join-jitsi'),
  joinBooth: document.getElementById('join-booth'),
  displayName: document.getElementById('display-name'),
  role: document.getElementById('participant-role'),
  language: document.getElementById('booth-language'),
  channel: document.getElementById('booth-channel'),
  chatForm: document.getElementById('chat-form'),
  chatInput: document.getElementById('chat-input'),
  toggleMic: document.getElementById('toggle-mic'),
  goLive: document.getElementById('go-live'),
  passRelay: document.getElementById('pass-relay'),
  micDeviceSelect: document.getElementById('mic-device-select'),
  micMeter: document.getElementById('mic-meter'),
  meterRow: document.getElementById('meter-row'),
  micTestBtn: document.getElementById('mic-test-btn'),
  hlsUrlRow: document.getElementById('hls-url-row'),
  hlsUrlDisplay: document.getElementById('hls-url-display'),
  copyHlsUrl: document.getElementById('copy-hls-url'),
  micChevron: document.getElementById('mic-chevron'),
  ctrlMicPopup: document.getElementById('ctrl-mic-popup'),
  muteLabel: document.getElementById('mute-label'),
  liveLabel: document.getElementById('live-label'),
  ctrlCompound: document.querySelector('.ctrl-compound'),
  leaveBooth: document.getElementById('leave-booth-btn'),
  preflightRetry: document.getElementById('preflight-retry'),
  checkMicPermission: document.getElementById('check-mic-permission'),
  checkAudioDevice: document.getElementById('check-audio-device'),
  checkServer: document.getElementById('check-server'),
  hlsValidationStatus: document.getElementById('hls-validation-status'),
}

// ── Audio context state ───────────────────────────────────────────────────────
let micTestStream = null
let micAnimFrame = null
let micAudioCtx = null
let micAnalyser = null

// ── HLS polling state ─────────────────────────────────────────────────────────
let hlsPollTimer = null
let hlsPollCount = 0
const HLS_POLL_INTERVAL_MS = 1000
const HLS_POLL_MAX_ATTEMPTS = 10

boot().catch((error) => {
  showError(`Failed to boot interpreter portal: ${error.message}`)
})

async function boot() {
  elements.jitsiUrl.value = state.defaultJitsiRoom
  await fetchBoothState()
  await fetchIngestReachability()
  await populateMicDevices()
  await acquireJwt()
  await connectWebSocket()
  bindEventHandlers()
  render()
  runPreflightChecks().catch((error) => {
    showError(`Preflight checks failed: ${error.message}`)
  })
}

// ── JWT and WebSocket lifecycle ───────────────────────────────────────────────

async function acquireJwt() {
  try {
    const response = await fetch('/api/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: state.token }),
    })
    if (!response.ok) return
    const data = await response.json()
    state.jwt = data.access_token || null
  } catch {
    // Server may not require auth; proceed without JWT
  }
}

function buildWsUrl() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = new URL(`${proto}//${window.location.host}/ws/booth/${encodeURIComponent(state.boothId)}`)
  if (state.jwt) {
    url.searchParams.set('token', state.jwt)
  }
  return url.toString()
}

function connectWebSocket() {
  return new Promise((resolve) => {
    const ws = new WebSocket(buildWsUrl())
    state.ws = ws

    const openTimer = setTimeout(() => resolve(), 4000)

    ws.addEventListener('open', () => {
      clearTimeout(openTimer)
      state.wsConnected = true
      setBadge(elements.connectionStatus, 'Connected', 'success')
      resolve()
    })

    ws.addEventListener('close', (event) => {
      state.wsConnected = false
      state.ws = null
      setBadge(elements.connectionStatus, 'Disconnected', 'warning')
      if (event.code !== 1000 && event.code !== 1001) {
        showError('Connection lost. Refresh the page to reconnect.')
      }
    })

    ws.addEventListener('error', () => {
      clearTimeout(openTimer)
      showError('WebSocket connection failed. Is the server running?')
      resolve()
    })

    ws.addEventListener('message', (event) => {
      try {
        handleServerMessage(JSON.parse(event.data))
      } catch {
        // Ignore malformed server messages
      }
    })
  })
}

function handleServerMessage(data) {
  const type = data.type
  if (type === 'booth:joined') {
    state.participantId = data.participant_id
    state.joined = true
    applyBoothState(data.state)
    render()
    showError('')
  } else if (type === 'booth:state') {
    applyBoothState(data.state)
    render()
  } else if (type === 'booth:chat') {
    state.chatMessages.push(data.message)
    renderChat()
  } else if (type === 'booth:error') {
    showError(data.message || 'An unknown booth error occurred.')
  }
}

/**
 * Send a JSON message over the WebSocket.
 * @returns {boolean} true if the message was sent successfully
 */
function wsSend(obj) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
    showError('Not connected to server.')
    return false
  }
  state.ws.send(JSON.stringify(obj))
  return true
}

function authHeaders() {
  if (!state.jwt) return {}
  return { Authorization: `Bearer ${state.jwt}` }
}

// ── Event binding ─────────────────────────────────────────────────────────────

function bindEventHandlers() {
  elements.joinBooth.addEventListener('click', () => {
    joinBooth()
  })

  elements.joinJitsi.addEventListener('click', () => {
    joinMonitoringFeed()
  })

  elements.jitsiFrame.addEventListener('load', () => {
    setBadge(elements.monitorStatus, 'Monitoring', 'success')
  })

  elements.chatForm.addEventListener('submit', (event) => {
    event.preventDefault()
    sendChatMessage()
  })

  elements.toggleMic.addEventListener('click', async () => {
    await toggleMicMute()
  })

  elements.goLive.addEventListener('click', async () => {
    if (state.ingestConnected) {
      await stopLiveIngest()
      return
    }
    await startLiveIngest()
  })

  elements.passRelay.addEventListener('click', () => {
    passRelayToNextInterpreter()
  })

  elements.micDeviceSelect.addEventListener('change', () => {
    state.micDeviceId = elements.micDeviceSelect.value
    localStorage.setItem('mic-device-id', state.micDeviceId)
    if (micTestStream) {
      stopMicTest().then(startMicTest).catch(() => {})
    }
  })

  elements.micTestBtn.addEventListener('click', async () => {
    if (micTestStream) {
      stopMicTest()
    } else {
      await startMicTest()
    }
  })

  elements.copyHlsUrl.addEventListener('click', () => {
    const url = elements.hlsUrlDisplay.textContent
    if (!url) return
    navigator.clipboard.writeText(url).then(() => {
      elements.copyHlsUrl.textContent = 'Copied!'
      setTimeout(() => {
        elements.copyHlsUrl.textContent = 'Copy'
      }, 2000)
    }).catch(() => {})
  })

  elements.micChevron.addEventListener('click', (event) => {
    event.stopPropagation()
    const isHidden = elements.ctrlMicPopup.classList.contains('hidden')
    elements.ctrlMicPopup.classList.toggle('hidden', !isHidden)
    elements.micChevron.setAttribute('aria-expanded', String(isHidden))
  })

  document.addEventListener('click', (event) => {
    if (!elements.ctrlMicPopup.classList.contains('hidden') &&
        !elements.ctrlMicPopup.contains(event.target) &&
        event.target !== elements.micChevron) {
      elements.ctrlMicPopup.classList.add('hidden')
      elements.micChevron.setAttribute('aria-expanded', 'false')
    }
  })

  document.addEventListener('keydown', (event) => {
    if (event.code !== 'Space') return
    const tag = document.activeElement?.tagName?.toLowerCase()
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return
    event.preventDefault()
    toggleMicMute().catch(() => {})
  })

  elements.preflightRetry.addEventListener('click', () => {
    runPreflightChecks().catch((error) => {
      showError(`Preflight checks failed: ${error.message}`)
    })
  })

  elements.leaveBooth.addEventListener('click', async () => {
    if (state.ingestConnected) {
      await stopLiveIngest()
    }
    if (state.joined && state.participantId) {
      wsSend({ type: 'booth:leave' })
      state.joined = false
      state.participantId = null
    }
    if (state.ws) {
      state.ws.close(1000)
      state.ws = null
    }
    setBadge(elements.connectionStatus, 'Left', 'warning')
    renderMicControls()
  })

  navigator.mediaDevices.addEventListener('devicechange', () => {
    populateMicDevices().catch(() => {})
  })

  elements.participantList.addEventListener('click', (event) => {
    const target = event.target
    if (!(target instanceof HTMLElement)) return
    if (!target.classList.contains('set-active-btn')) return
    const participantId = target.dataset.participantId
    if (!participantId) return
    wsSend({
      type: 'booth:set-active',
      target_id: participantId,
    })
  })

  window.addEventListener('beforeunload', () => {
    if (state.joined && state.participantId) {
      wsSend({ type: 'booth:leave' })
    }
  })
}

// ── Preflight checks ──────────────────────────────────────────────────────────

async function runPreflightChecks() {
  setPreflightStatus('micPermission', 'pending', 'Checking…')
  setPreflightStatus('audioDevice', 'pending', 'Checking…')
  setPreflightStatus('serverReachable', 'pending', 'Checking…')

  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    stream.getTracks().forEach((t) => t.stop())
    setPreflightStatus('micPermission', 'pass', 'Permission granted')
  } catch (error) {
    const msg =
      error.name === 'NotAllowedError'
        ? 'Denied — allow microphone access in browser settings'
        : `Error: ${error.message}`
    setPreflightStatus('micPermission', 'fail', msg)
  }

  try {
    const devices = await navigator.mediaDevices.enumerateDevices()
    const audioInputs = devices.filter((d) => d.kind === 'audioinput')
    if (audioInputs.length > 0) {
      setPreflightStatus('audioDevice', 'pass', `${audioInputs.length} device${audioInputs.length > 1 ? 's' : ''} found`)
    } else {
      setPreflightStatus('audioDevice', 'fail', 'No microphone detected — connect a USB mic or headset')
    }
  } catch {
    setPreflightStatus('audioDevice', 'warn', 'Could not enumerate devices')
  }

  if (!state.hlsBase) {
    setPreflightStatus('serverReachable', 'warn', 'HLS base not configured — server check skipped')
  } else {
    try {
      const controller = new AbortController()
      const timeoutId = window.setTimeout(() => controller.abort(), 4000)
      await fetch(`${state.hlsBase}/`, { method: 'HEAD', mode: 'no-cors', signal: controller.signal })
      window.clearTimeout(timeoutId)
      setPreflightStatus('serverReachable', 'pass', 'MediaMTX is reachable')
      state.ingestReachable = true
    } catch (error) {
      const msg =
        error.name === 'AbortError'
          ? 'Timed out — start MediaMTX: docker compose up mediamtx'
          : 'Unreachable — start MediaMTX: docker compose up mediamtx'
      setPreflightStatus('serverReachable', 'fail', msg)
      state.ingestReachable = false
    }
  }

  renderMicControls()
}

function setPreflightStatus(check, status, message = '') {
  state.preflight[check] = status
  const idMap = {
    micPermission: 'check-mic-permission',
    audioDevice: 'check-audio-device',
    serverReachable: 'check-server',
  }
  const iconMap = { pass: '✅', fail: '❌', warn: '⚠️', pending: '⏳' }
  const el = document.getElementById(idMap[check])
  if (!el) return
  el.className = `preflight-item preflight--${status}`
  const iconEl = el.querySelector('.preflight-icon')
  if (iconEl) iconEl.textContent = iconMap[status] ?? '⏳'
  const msgEl = el.querySelector('.preflight-msg')
  if (msgEl) msgEl.textContent = message
}

// ── HLS validation ────────────────────────────────────────────────────────────

async function validateHlsOutput() {
  if (!state.hlsBase || !state.channelId) return false
  const url = `${state.hlsBase}/${encodeURIComponent(state.channelId)}/index.m3u8`
  try {
    const response = await fetch(url, { cache: 'no-store' })
    if (!response.ok) return false
    const contentType = response.headers.get('content-type') || ''
    const normalized = contentType.toLowerCase()
    const isHls =
      normalized.startsWith('application/vnd.apple.mpegurl') ||
      normalized.startsWith('application/x-mpegurl') ||
      normalized.startsWith('audio/mpegurl')
    if (!isHls) return false
    const text = await response.text()
    return text.includes('#EXTM3U')
  } catch {
    return false
  }
}

function startHlsPolling() {
  stopHlsPolling()
  hlsPollCount = 0
  setHlsValidationStatus('polling')

  async function poll() {
    hlsPollCount += 1
    setHlsValidationStatus('polling')
    const valid = await validateHlsOutput()
    if (valid) {
      setHlsValidationStatus('streaming')
      hlsPollTimer = null
      return
    }
    if (hlsPollCount >= HLS_POLL_MAX_ATTEMPTS) {
      setHlsValidationStatus('unavailable')
      hlsPollTimer = null
      return
    }
    hlsPollTimer = window.setTimeout(poll, HLS_POLL_INTERVAL_MS)
  }

  hlsPollTimer = window.setTimeout(poll, HLS_POLL_INTERVAL_MS)
}

function stopHlsPolling() {
  if (hlsPollTimer !== null) {
    window.clearTimeout(hlsPollTimer)
    hlsPollTimer = null
  }
}

function setHlsValidationStatus(status) {
  const statusMap = {
    idle:        { text: 'Not started',   tone: '' },
    polling:     { text: `Validating\u2026 ${hlsPollCount}/${HLS_POLL_MAX_ATTEMPTS}`, tone: 'warning' },
    streaming:   { text: 'Streaming',     tone: 'success' },
    unavailable: { text: 'Not available', tone: 'danger' },
  }
  const { text, tone } = statusMap[status] ?? { text: status, tone: '' }
  setBadge(elements.hlsValidationStatus, text, tone)
}

// ── REST helpers ──────────────────────────────────────────────────────────────

async function fetchBoothState() {
  const url = new URL(`/api/booth/${encodeURIComponent(state.boothId)}/state`, window.location.origin)
  url.searchParams.set('token', state.token)
  url.searchParams.set('language', state.language)
  url.searchParams.set('channel', state.channelId)
  const response = await fetch(url, { headers: authHeaders() })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: response.statusText }))
    throw new Error(payload.error || 'Failed to fetch booth state.')
  }
  applyBoothState(await response.json())
}

async function fetchIngestReachability() {
  if (state.whipBase) {
    state.ingestReachable = true
    return
  }
  const response = await fetch(`/api/interpreter/status/${encodeURIComponent(state.channelId)}`)
  if (!response.ok) return
  const payload = await response.json()
  state.ingestReachable = Boolean(payload.reachable)
}

function applyBoothState(payload) {
  const previousActiveInterpreterId = state.activeInterpreterId
  state.participants = payload.participants || []
  state.activeInterpreterId = payload.active_interpreter_id || null
  state.chatMessages = payload.chat_messages || []
  const lostActivePublisher =
    state.ingestConnected &&
    state.participantId &&
    previousActiveInterpreterId === state.participantId &&
    state.activeInterpreterId !== state.participantId
  if (lostActivePublisher) {
    stopLiveIngest().catch((error) => {
      showError(`Unable to stop previous ingest session: ${error.message}`)
    })
  }
}

// ── Booth actions ─────────────────────────────────────────────────────────────

function joinBooth() {
  const displayName = elements.displayName.value.trim()
  state.language = elements.language.value.trim() || state.language
  state.channelId = elements.channel.value.trim() || state.channelId
  wsSend({
    type: 'booth:join',
    display_name: displayName || 'Interpreter',
    role: elements.role.value,
    language: state.language,
    channel_id: state.channelId,
    participant_id: state.participantId,
  })
}

function joinMonitoringFeed() {
  try {
    const rawUrl = elements.jitsiUrl.value.trim()
    if (!rawUrl) {
      showError('Jitsi meeting URL is required.')
      return
    }
    const meetingUrl = new URL(rawUrl)
    if (state.jitsiDomain && meetingUrl.hostname !== state.jitsiDomain) {
      showError(`Jitsi URL must use ${state.jitsiDomain}.`)
      return
    }
    const hash = new URLSearchParams({
      'config.startWithAudioMuted': 'true',
      'config.startWithVideoMuted': 'true',
      'config.prejoinPageEnabled': 'false',
      'config.disableInitialGUM': 'true',
      'config.startSilent': 'true',
    }).toString()
    elements.jitsiFrame.src = `${meetingUrl.origin}${meetingUrl.pathname}#${hash}`
    setBadge(elements.monitorStatus, 'Connecting', 'warning')
    showError('')
  } catch (error) {
    showError(`Invalid Jitsi URL: ${error.message}`)
  }
}

async function populateMicDevices() {
  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true })
    tempStream.getTracks().forEach((t) => t.stop())
  } catch {
    // Permission denied or no device — continue without labels
  }
  const devices = await navigator.mediaDevices.enumerateDevices()
  const audioInputs = devices.filter((d) => d.kind === 'audioinput')
  const previous = elements.micDeviceSelect.value
  elements.micDeviceSelect.innerHTML = ''

  const defaultOpt = document.createElement('option')
  defaultOpt.value = ''
  defaultOpt.textContent = 'Default microphone'
  elements.micDeviceSelect.appendChild(defaultOpt)

  for (const device of audioInputs) {
    const opt = document.createElement('option')
    opt.value = device.deviceId
    opt.textContent = device.label || `Microphone ${elements.micDeviceSelect.options.length}`
    elements.micDeviceSelect.appendChild(opt)
  }

  const saved = state.micDeviceId
  if (saved && elements.micDeviceSelect.querySelector(`option[value="${CSS.escape(saved)}"]`)) {
    elements.micDeviceSelect.value = saved
  } else if (previous && elements.micDeviceSelect.querySelector(`option[value="${CSS.escape(previous)}"]`)) {
    elements.micDeviceSelect.value = previous
  }
}

// ── Microphone management ─────────────────────────────────────────────────────

async function ensureMicStream() {
  if (state.micStream) return state.micStream
  if (micTestStream) {
    stopMicTest()
  }
  const deviceId = state.micDeviceId
  state.micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      deviceId: deviceId ? { exact: deviceId } : undefined,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  })
  startMicMeter(state.micStream)
  return state.micStream
}

async function startMicTest() {
  if (micTestStream) return
  try {
    const deviceId = state.micDeviceId
    micTestStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })
    startMicMeter(micTestStream)
    elements.micTestBtn.textContent = '⏹ Stop'
    elements.micTestBtn.classList.add('btn-primary')
    showError('')
  } catch (error) {
    showError(`Cannot access microphone: ${error.message}`)
  }
}

function stopMicTest() {
  if (!micTestStream) return
  micTestStream.getTracks().forEach((t) => t.stop())
  micTestStream = null
  elements.micTestBtn.textContent = '⚙ Test'
  elements.micTestBtn.classList.remove('btn-primary')
  if (!state.ingestConnected) {
    stopMicMeter()
  }
}

function startMicMeter(stream) {
  stopMicMeter()
  try {
    micAudioCtx = new AudioContext()
    micAnalyser = micAudioCtx.createAnalyser()
    micAnalyser.fftSize = 256
    const source = micAudioCtx.createMediaStreamSource(stream)
    source.connect(micAnalyser)
  } catch {
    return
  }

  elements.meterRow.classList.remove('hidden')
  const canvas = elements.micMeter
  const ctx = canvas.getContext('2d')
  const data = new Uint8Array(micAnalyser.frequencyBinCount)

  function draw() {
    micAnimFrame = requestAnimationFrame(draw)
    micAnalyser.getByteFrequencyData(data)
    const avg = data.reduce((a, b) => a + b, 0) / data.length
    const volume = avg / 128

    ctx.clearRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = '#e9ecef'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    if (state.micMuted) {
      ctx.fillStyle = '#9ca3af'
    } else if (volume > 0.95) {
      ctx.fillStyle = '#dc3545'
    } else if (volume > 0.75) {
      ctx.fillStyle = '#fd7e14'
    } else {
      ctx.fillStyle = '#22c55e'
    }
    ctx.fillRect(0, 0, volume * canvas.width, canvas.height)
  }
  draw()
}

function stopMicMeter() {
  if (micAnimFrame) {
    cancelAnimationFrame(micAnimFrame)
    micAnimFrame = null
  }
  if (micAudioCtx) {
    micAudioCtx.close().catch(() => {})
    micAudioCtx = null
    micAnalyser = null
  }
  const canvas = elements.micMeter
  if (canvas) {
    const ctx = canvas.getContext('2d')
    ctx.clearRect(0, 0, canvas.width, canvas.height)
  }
  elements.meterRow.classList.add('hidden')
}

async function toggleMicMute() {
  if (!state.micStream) {
    await ensureMicStream()
  }
  state.micMuted = !state.micMuted
  state.micStream.getAudioTracks().forEach((track) => {
    track.enabled = !state.micMuted
  })
  if (state.joined && state.participantId) {
    wsSend({
      type: 'booth:update-state',
      mic_active: !state.micMuted && state.ingestConnected,
    })
  }
  renderMicControls()
}

// ── Ingest helpers ────────────────────────────────────────────────────────────

async function doWhipIngest(peerConnection) {
  const whipUrl = `${state.whipBase}/${encodeURIComponent(state.channelId)}/whip`
  const response = await fetch(whipUrl, {
    method: 'POST',
    headers: { 'Content-Type': 'application/sdp' },
    body: peerConnection.localDescription.sdp,
  })
  if (!response.ok) {
    const detail = await response.text().catch(() => response.statusText)
    throw new Error(`WHIP error ${response.status}: ${detail}`)
  }
  const answerSdp = await response.text()
  await peerConnection.setRemoteDescription({ type: 'answer', sdp: answerSdp })
}

// Legacy aiortc path via FastAPI — kept for the migration period (USE_LEGACY_INGEST=true).
// Phase 1D: remove this function and the /api/interpreter/connect route.
async function doLegacyIngest(peerConnection) {
  const response = await fetch(`/api/interpreter/connect/${encodeURIComponent(state.channelId)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({
      booth_id: state.boothId,
      participant_id: state.participantId,
      language: state.language,
      token: state.token,
      type: peerConnection.localDescription.type,
      sdp: peerConnection.localDescription.sdp,
    }),
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: response.statusText }))
    throw new Error(payload.error || 'Legacy ingest negotiation failed.')
  }
  const answer = await response.json()
  await peerConnection.setRemoteDescription(answer)
}

async function startLiveIngest() {
  if (!state.joined || !state.participantId) {
    showError('Join the booth before going live.')
    return
  }
  if (!state.ingestReachable) {
    showError('Ingest backend is unavailable. Check server configuration.')
    return
  }
  const isActive = state.activeInterpreterId === state.participantId
  if (!isActive) {
    showError('Only the active interpreter can go live.')
    return
  }
  try {
    await ensureMicStream()
    if (state.peerConnection) {
      state.peerConnection.close()
      state.peerConnection = null
    }
    const peerConnection = new RTCPeerConnection()
    state.peerConnection = peerConnection
    state.micStream.getAudioTracks().forEach((track) => {
      peerConnection.addTrack(track, state.micStream)
    })
    peerConnection.addEventListener('connectionstatechange', () => {
      if (peerConnection.connectionState === 'failed' || peerConnection.connectionState === 'disconnected') {
        setBadge(elements.ingestStatus, 'Ingest reconnecting', 'warning')
      }
      if (peerConnection.connectionState === 'connected') {
        setBadge(elements.ingestStatus, 'Ingest connected', 'success')
      }
    })

    const offer = await peerConnection.createOffer({ offerToReceiveAudio: false, offerToReceiveVideo: false })
    await peerConnection.setLocalDescription(offer)
    await waitForIceGathering(peerConnection)

    if (state.whipBase) {
      try {
        await doWhipIngest(peerConnection)
        state.usedLegacyFallback = false
      } catch (whipError) {
        if (state.useLegacyIngest) {
          showError(`WHIP failed (${whipError.message}); retrying via legacy ingest…`)
          await doLegacyIngest(peerConnection)
          state.usedLegacyFallback = true
        } else {
          throw whipError
        }
      }
    } else if (state.useLegacyIngest) {
      await doLegacyIngest(peerConnection)
      state.usedLegacyFallback = true
    } else {
      throw new Error('No ingest path available. Set MEDIAMTX_WHIP_BASE or enable USE_LEGACY_INGEST.')
    }

    state.ingestConnected = true
    if (state.hlsBase) startHlsPolling()
    wsSend({
      type: 'booth:update-state',
      mic_active: !state.micMuted,
      ingest_connected: true,
    })
    showError('')
  } catch (error) {
    showError(`Unable to start ingest: ${error.message}`)
    await stopLiveIngest()
  }
  renderMicControls()
}

async function stopLiveIngest() {
  if (state.peerConnection) {
    state.peerConnection.close()
    state.peerConnection = null
  }
  if (state.micStream) {
    state.micStream.getTracks().forEach((t) => t.stop())
    state.micStream = null
  }
  if (!micTestStream) {
    stopMicMeter()
  }
  if (state.joined && state.participantId) {
    if (state.usedLegacyFallback) {
      await fetch(`/api/interpreter/disconnect/${encodeURIComponent(state.channelId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          booth_id: state.boothId,
          participant_id: state.participantId,
          language: state.language,
          token: state.token,
        }),
      }).catch(() => {})
      state.usedLegacyFallback = false
    }
    wsSend({
      type: 'booth:update-state',
      mic_active: false,
      ingest_connected: false,
    })
  }
  stopHlsPolling()
  setHlsValidationStatus('idle')
  state.ingestConnected = false
  renderMicControls()
}

function passRelayToNextInterpreter() {
  if (!state.joined || !state.participantId) return
  const interpreters = state.participants.filter((p) => p.role === 'interpreter')
  if (interpreters.length < 2) {
    showError('At least two interpreters are required for relay handoff.')
    return
  }
  const currentIndex = interpreters.findIndex((p) => p.participant_id === state.activeInterpreterId)
  const nextIndex = currentIndex >= 0 ? (currentIndex + 1) % interpreters.length : 0
  wsSend({
    type: 'booth:set-active',
    target_id: interpreters[nextIndex].participant_id,
  })
}

function sendChatMessage() {
  const body = elements.chatInput.value.trim()
  if (!body) return
  if (!state.participantId) {
    showError('Join the booth before sending messages.')
    return
  }
  wsSend({ type: 'booth:chat', body })
  elements.chatInput.value = ''
}

// ── Rendering ─────────────────────────────────────────────────────────────────

function render() {
  renderParticipants()
  renderChat()
  renderMicControls()
}

function renderParticipants() {
  const currentParticipant = state.participants.find((p) => p.participant_id === state.participantId)
  const canReassign = currentParticipant?.role === 'coordinator'
  const activeParticipant = state.participants.find((p) => p.participant_id === state.activeInterpreterId)
  setBadge(
    elements.activeIndicator,
    activeParticipant ? `${activeParticipant.display_name} is active` : 'No active interpreter',
    activeParticipant ? 'success' : 'warning',
  )
  elements.activeName.textContent = activeParticipant ? activeParticipant.display_name : 'Unassigned'
  elements.participantList.innerHTML = ''
  for (const participant of state.participants) {
    const tile = document.createElement('article')
    tile.className = 'participant-tile'
    if (participant.participant_id === state.activeInterpreterId) {
      tile.classList.add('active')
    }
    const canActivateSelf = participant.participant_id === state.participantId
    const canActivate = participant.role === 'interpreter' && (canReassign || canActivateSelf)
    const ingestLabel = participant.ingest_connected ? 'ingest connected' : 'ingest idle'
    tile.innerHTML = `
      <div class="participant-top">
        <strong>${escapeHtml(participant.display_name)}</strong>
        <span class="participant-pill ${participant.participant_id === state.activeInterpreterId ? 'live' : ''}">
          ${participant.participant_id === state.activeInterpreterId ? 'LIVE' : participant.role}
        </span>
      </div>
      <div class="participant-meta">${escapeHtml(participant.language)} · ${escapeHtml(participant.channel_id)}</div>
      <div class="participant-bottom">
        <div>
          <span class="participant-pill">${participant.mic_active ? 'mic active' : 'mic muted'}</span>
          <span class="participant-pill">${ingestLabel}</span>
        </div>
        ${canActivate ? `<button type="button" class="btn set-active-btn" data-participant-id="${participant.participant_id}">Set Active</button>` : ''}
      </div>
    `
    elements.participantList.append(tile)
  }
}

function renderChat() {
  elements.chatLog.innerHTML = ''
  if (!state.chatMessages.length) {
    elements.chatLog.textContent = 'No booth messages yet.'
    return
  }
  for (const message of state.chatMessages.slice(-100)) {
    const entry = document.createElement('article')
    entry.className = 'chat-entry'
    entry.innerHTML = `
      <header>
        <strong>${escapeHtml(message.sender_name)}</strong>
        <span>${formatTime(message.sent_at)}</span>
      </header>
      <p>${escapeHtml(message.body)}</p>
    `
    elements.chatLog.append(entry)
  }
  elements.chatLog.scrollTop = elements.chatLog.scrollHeight
}

function renderMicControls() {
  const joinedActiveInterpreter = state.joined && state.participantId === state.activeInterpreterId
  setBadge(
    elements.ingestStatus,
    state.ingestConnected ? 'Ingest connected' : 'Ingest disconnected',
    state.ingestConnected ? 'success' : 'warning',
  )
  elements.ingestReachable.textContent = state.ingestReachable ? 'Reachable' : 'Unavailable'
  elements.micState.textContent = state.micMuted ? 'Muted' : state.micStream ? 'Ready' : 'Inactive'

  const micOnIcon = elements.toggleMic.querySelector('.ctrl-icon--mic-on')
  const micOffIcon = elements.toggleMic.querySelector('.ctrl-icon--mic-off')
  if (micOnIcon) micOnIcon.classList.toggle('hidden', state.micMuted)
  if (micOffIcon) micOffIcon.classList.toggle('hidden', !state.micMuted)
  elements.toggleMic.classList.toggle('muted', state.micMuted)
  if (elements.ctrlCompound) elements.ctrlCompound.classList.toggle('muted', state.micMuted)
  if (elements.muteLabel) elements.muteLabel.textContent = state.micMuted ? 'UNMUTE' : 'MUTE'
  elements.toggleMic.setAttribute('aria-label', state.micMuted ? 'Unmute microphone' : 'Mute microphone')
  elements.toggleMic.setAttribute('title', state.micMuted ? 'Unmute (Space)' : 'Mute (Space)')

  elements.goLive.classList.toggle('live', state.ingestConnected)
  if (elements.liveLabel) elements.liveLabel.textContent = state.ingestConnected ? 'STOP' : 'GO LIVE'

  elements.toggleMic.disabled = !state.joined
  const preflightCriticalPass =
    state.preflight.micPermission === 'pass' &&
    state.preflight.serverReachable !== 'fail'
  elements.goLive.disabled = !state.ingestConnected && (!joinedActiveInterpreter || !state.ingestReachable || !preflightCriticalPass)
  elements.passRelay.disabled = !joinedActiveInterpreter
  elements.micDeviceSelect.disabled = state.ingestConnected

  if (state.ingestConnected && state.hlsBase) {
    const url = `${state.hlsBase}/${encodeURIComponent(state.channelId)}/index.m3u8`
    elements.hlsUrlDisplay.textContent = url
    elements.hlsUrlRow.classList.remove('hidden')
  } else {
    elements.hlsUrlRow.classList.add('hidden')
  }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function setBadge(element, text, tone = '') {
  element.textContent = text
  element.classList.remove('success', 'warning', 'danger')
  if (tone) element.classList.add(tone)
}

function showError(message) {
  elements.errorBanner.textContent = message
}

function waitForIceGathering(peerConnection) {
  if (peerConnection.iceGatheringState === 'complete') {
    return Promise.resolve()
  }
  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      peerConnection.removeEventListener('icegatheringstatechange', onStateChange)
      resolve()
    }, 3000)
    function onStateChange() {
      if (peerConnection.iceGatheringState !== 'complete') return
      window.clearTimeout(timeout)
      peerConnection.removeEventListener('icegatheringstatechange', onStateChange)
      resolve()
    }
    peerConnection.addEventListener('icegatheringstatechange', onStateChange)
  })
}

function formatTime(timestamp) {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;')
}
