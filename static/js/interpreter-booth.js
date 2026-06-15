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
  relayingOut: false,   // true while outgoing interp is in silence-mode handoff
  micStream: null,
  peerConnection: null,
  whipResourceUrl: null,
  micMuted: true,
  ingestConnected: false,
  ingestReachable: Boolean(portal.dataset.whipBase),
  defaultJitsiRoom: portal.dataset.defaultJitsi || '',
  jitsiDomain: portal.dataset.jitsiDomain || '',
  whipBase: portal.dataset.whipBase || '',
  whepUrl: portal.dataset.whepUrl || '',
  relayWhepUrl: portal.dataset.relayWhepUrl || '',
  micDeviceId: localStorage.getItem('mic-device-id') || '',
  /** Role granted by the server (from JWT). Empty string = unknown / legacy. */
  grantedRole: portal.dataset.grantedRole || '',
  preflight: {
    micPermission: 'pending',
    audioDevice: 'pending',
    serverReachable: 'pending',
  },
  /** Handoff protocol state from server */
  handoffState: 'idle',       // 'idle' | 'offered' | 'requested'
  handoffInitiatorId: null,
  /** Track whether booth audio WHEP has been auto-started for passive */
  boothAudioAutoStarted: false,
}

const elements = {
  connectionStatus: document.getElementById('connection-status'),
  activeIndicator: document.getElementById('active-indicator'),
  ingestStatus: document.getElementById('ingest-status'),
  micState: document.getElementById('mic-state'),
  errorBanner: document.getElementById('error-banner'),
  participantList: document.getElementById('participant-list'),
  participantCount: document.getElementById('participant-count'),
  chatLog: document.getElementById('chat-log'),
  jitsiFrame: document.getElementById('jitsi-frame'),
  chatForm: document.getElementById('chat-form'),
  chatInput: document.getElementById('chat-input'),
  toggleMic: document.getElementById('toggle-mic'),
  handoverBtn: document.getElementById('handover-btn'),
  handoverLabel: document.getElementById('handover-label'),
  liveBadge: document.getElementById('live-badge'),
  liveBadgeText: document.getElementById('live-badge-text'),
  passRelay: document.getElementById('pass-relay'),
  relayAudio: document.getElementById('relay-audio'),
  relayDeviceSelect: document.getElementById('relay-device-select'),
  showVirtualRelayDevices: document.getElementById('show-virtual-relay-devices'),
  relayVolume: document.getElementById('relay-volume'),
  relayStatus: document.getElementById('relay-status'),

  boothAudio: document.getElementById('booth-audio'),
  boothVolume: document.getElementById('booth-volume'),
  boothVolumeLabel: document.getElementById('booth-volume-label'),

  micDeviceSelect: document.getElementById('mic-device-select'),
  showVirtualDevices: document.getElementById('show-virtual-devices'),
  micMeter: document.getElementById('mic-meter'),
  meterRow: document.getElementById('meter-row'),
  micTestBtn: document.getElementById('mic-test-btn'),
  loopbackTestBtn: document.getElementById('loopback-test-btn'),
  loopbackProgressRow: document.getElementById('loopback-progress-row'),
  loopbackProgress: document.getElementById('loopback-progress'),
  loopbackStatus: document.getElementById('loopback-status'),
  listenerUrlRow: document.getElementById('listener-url-row'),
  listenerUrlDisplay: document.getElementById('listener-url-display'),
  copyListenerUrl: document.getElementById('copy-listener-url'),
  muteLabel: document.getElementById('mute-label'),
  preflightRetry: document.getElementById('preflight-retry'),
  checkMicPermission: document.getElementById('check-mic-permission'),
}

// ── Audio context state ───────────────────────────────────────────────────────
let micTestStream = null
let micAnimFrame = null
let micAudioCtx = null
let micAnalyser = null
let loopbackRecorder = null
let loopbackAudio = null


boot().catch((error) => {
  const msg = error instanceof Error ? error.message : String(error)
  showError(`Failed to boot interpreter portal: ${msg}`)
})

async function boot() {
  await fetchBoothState()
  await fetchIngestReachability()
  await populateMicDevices()
  if (state.relayWhepUrl) {
    await populateRelayDevices()
  }

  // Run preflights asynchronously before blocking on JWT/WS connection
  runPreflightChecks().catch((error) => {
    const msg = error instanceof Error ? error.message : String(error)
    showError(`Preflight checks failed: ${msg}`)
  })
  await acquireJwt()
  await connectWebSocket()
  bindEventHandlers()

  // Auto-join the booth immediately
  joinBooth()

  render()
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
    applyBoothState(data.state, { skipAutoStart: false })
    joinMonitoringFeed()
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
  // Handover button click
  elements.handoverBtn.addEventListener('click', () => {
    if (!state.joined || !state.participantId) return
    const isActive = state.activeInterpreterId === state.participantId
    const hState = state.handoffState

    if (hState === 'idle') {
      // Initiate a handoff
      wsSend({ type: 'booth:initiate-handoff' })
    } else if (hState === 'offered' && !isActive) {
      // Passive accepts the offer (TAKE OVER flashing yellow → accept)
      wsSend({ type: 'booth:accept-handoff' })
    } else if (hState === 'requested' && isActive) {
      // Active accepts the request (PASS MIC flashing yellow → yield)
      wsSend({ type: 'booth:accept-handoff' })
    } else if (hState === 'offered' && isActive && state.handoffInitiatorId === state.participantId) {
      // Active clicks their own green button → cancel
      wsSend({ type: 'booth:cancel-handoff' })
    } else if (hState === 'requested' && !isActive && state.handoffInitiatorId === state.participantId) {
      // Passive clicks their own green button → cancel
      wsSend({ type: 'booth:cancel-handoff' })
    }
  })

  elements.chatForm.addEventListener('submit', (event) => {
    event.preventDefault()
    sendChatMessage()
  })

  elements.toggleMic.addEventListener('click', async () => {
    await toggleMicMute()
  })

  if (elements.passRelay) {
    elements.passRelay.addEventListener('click', toggleRelayAudio)
  }

  if (elements.relayDeviceSelect && elements.relayAudio) {
    elements.relayDeviceSelect.addEventListener('change', async () => {
      try {
        if (typeof elements.relayAudio.setSinkId === 'function') {
          await elements.relayAudio.setSinkId(elements.relayDeviceSelect.value)
        }
      } catch (e) {
        console.warn('setSinkId failed', e)
      }
    })
  }

  // Booth Audio Volume slider
  if (elements.boothVolume && elements.boothAudio) {
    elements.boothVolume.addEventListener('input', () => {
      const val = parseInt(elements.boothVolume.value, 10)
      elements.boothAudio.volume = val / 100
      if (elements.boothVolumeLabel) elements.boothVolumeLabel.textContent = `${val}%`
      // Start WHEP on first non-zero drag; stop when back to zero.
      if (val > 0 && !boothListening) {
        startBoothAudioListening()
      } else if (val === 0 && boothListening) {
        stopBoothAudioListening()
      }
    })
  }

  if (elements.showVirtualRelayDevices) {
    elements.showVirtualRelayDevices.addEventListener('change', populateRelayDevices)
  }

  if (elements.relayVolume && elements.relayAudio) {
    elements.relayVolume.addEventListener('input', () => {
      elements.relayAudio.volume = parseFloat(elements.relayVolume.value)
    })
  }

  elements.micDeviceSelect.addEventListener('change', () => {
    state.micDeviceId = elements.micDeviceSelect.value
    localStorage.setItem('mic-device-id', state.micDeviceId)
    if (micTestStream) {
      stopMicTest().then(startMicTest).catch(() => {})
    }
  })

  elements.showVirtualDevices.addEventListener('change', () => {
    populateMicDevices()
  })

  elements.micTestBtn.addEventListener('click', async () => {
    if (micTestStream) {
      stopMicTest()
    } else {
      await startMicTest()
    }
  })

  if (elements.loopbackTestBtn) {
    elements.loopbackTestBtn.addEventListener('click', async () => {
      if (loopbackRecorder || loopbackAudio) {
        stopLoopbackTest()
      } else {
        await startLoopbackTest()
      }
    })
  }

  if (elements.copyListenerUrl) {
    elements.copyListenerUrl.addEventListener('click', () => {
      const url = elements.listenerUrlDisplay.textContent
      if (!url) return
      navigator.clipboard.writeText(url).then(() => {
        elements.copyListenerUrl.textContent = 'Copied!'
        setTimeout(() => {
          elements.copyListenerUrl.textContent = 'Copy'
        }, 2000)
      }).catch(() => {})
    })
  }

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

  if (navigator.mediaDevices) {
    navigator.mediaDevices.addEventListener('devicechange', () => {
      populateMicDevices().catch(() => {})
    })
  }

  // Set Active button removed — active interpreter assignment is handled
  // automatically on join and via the handover protocol.

  window.addEventListener('beforeunload', () => {
    if (state.joined && state.participantId) {
      wsSend({ type: 'booth:leave' })
    }
  })
}

// ── Preflight checks ──────────────────────────────────────────────────────────

async function runPreflightChecks() {
  setPreflightStatus('micPermission', 'pending', 'Checking…')

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
    const resp = await fetch(`/api/interpreter/status/${encodeURIComponent(state.channelId)}`)
    if (resp.ok) {
      const payload = await resp.json()
      state.ingestReachable = Boolean(payload.reachable)
    } else {
      state.ingestReachable = false
    }
  } catch {
    state.ingestReachable = false
  }

  if (!state.ingestReachable) {
    console.warn('MediaMTX is unreachable — start MediaMTX: docker compose up mediamtx')
  }

  renderMicControls()
}

function setPreflightStatus(check, status, message = '') {
  state.preflight[check] = status
  const idMap = {
    micPermission: 'check-mic-permission',
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
  const response = await fetch(`/api/interpreter/status/${encodeURIComponent(state.channelId)}`)
  if (!response.ok) {
    state.ingestReachable = false
    return
  }
  const payload = await response.json()
  state.ingestReachable = Boolean(payload.reachable)
}

function applyBoothState(payload, { skipAutoStart = false } = {}) {
  const previousActiveInterpreterId = state.activeInterpreterId
  const previousBroadcastUnlocked = state.broadcastUnlocked

  state.participants = payload.participants || []
  state.activeInterpreterId = payload.active_interpreter_id || null
  state.chatMessages = payload.chat_messages || []
  state.handoffState = payload.handoff_state || 'idle'
  state.handoffInitiatorId = payload.handoff_initiator_id || null
  state.broadcastUnlocked = payload.broadcast_unlocked || false

  // Ensure non-active interpreters are always muted locally
  if (state.participantId && state.activeInterpreterId !== state.participantId) {
    if (!state.micMuted) {
      state.micMuted = true
      if (state.micStream) {
        state.micStream.getAudioTracks().forEach((t) => { t.enabled = false })
      }
    }
  }

  const lostActivePublisher =
    state.ingestConnected &&
    state.participantId &&
    previousActiveInterpreterId === state.participantId &&
    state.activeInterpreterId !== state.participantId

  const becameActive =
    state.joined &&
    state.participantId &&
    state.activeInterpreterId === state.participantId &&
    previousActiveInterpreterId !== state.participantId

  const becamePassive =
    state.joined &&
    state.participantId &&
    state.activeInterpreterId !== state.participantId &&
    previousActiveInterpreterId === state.participantId

  const becameUnlocked = state.broadcastUnlocked && !previousBroadcastUnlocked
  const becameLocked = !state.broadcastUnlocked && previousBroadcastUnlocked

  const lostPublishingRights = 
    lostActivePublisher || (becameLocked && state.activeInterpreterId === state.participantId)

  if (lostPublishingRights) {
    state.relayingOut = true
    if (state.micStream) {
      state.micStream.getAudioTracks().forEach((t) => { t.enabled = false })
    }
    stopLiveIngest().catch(() => {}).then(() => {
      state.relayingOut = false
      if (state.micStream) {
        state.micStream.getAudioTracks().forEach((t) => { t.enabled = !state.micMuted })
      }
    })
  }

  // Auto-start ingest when becoming active or when broadcast unlocks
  const shouldStartIngest = (becameActive || becameUnlocked) && state.activeInterpreterId === state.participantId && state.broadcastUnlocked

  if (!skipAutoStart && shouldStartIngest && !state.ingestConnected && state.ingestReachable) {
    if (state.micMuted) {
      state.micMuted = false
      if (state.micStream) {
        state.micStream.getAudioTracks().forEach((t) => { t.enabled = true })
      }
      renderMicControls()
    }
    attemptRelayStart(0)
  }

  // Booth Audio volume defaults: Active=0%, Passive=80%
  if (becameActive) {
    setBoothVolume(0)
  } else if (becamePassive) {
    setBoothVolume(80)
    // Auto-start booth audio WHEP for passive interpreters
    if (!state.boothAudioAutoStarted) {
      state.boothAudioAutoStarted = true
      startBoothAudioListening()
    }
  }

  // If just joined as passive, also set default volume
  if (state.joined && state.participantId && state.activeInterpreterId !== state.participantId && previousActiveInterpreterId === null) {
    setBoothVolume(80)
    if (!state.boothAudioAutoStarted) {
      state.boothAudioAutoStarted = true
      startBoothAudioListening()
    }
  }
  // If just joined as active, set default volume
  if (state.joined && state.participantId && state.activeInterpreterId === state.participantId && previousActiveInterpreterId === null) {
    setBoothVolume(0)
  }
}

/** Set booth audio volume slider + audio element */
function setBoothVolume(pct) {
  if (elements.boothVolume) elements.boothVolume.value = pct
  if (elements.boothAudio) elements.boothAudio.volume = pct / 100
  if (elements.boothVolumeLabel) elements.boothVolumeLabel.textContent = `${pct}%`
}

// ── Booth actions ─────────────────────────────────────────────────────────────

function joinBooth() {
  const displayName = portal.dataset.displayName || 'Interpreter'
  const requestedRole = state.grantedRole || 'interpreter'

  wsSend({
    type: 'booth:join',
    display_name: displayName || 'Interpreter',
    role: requestedRole,
    language: state.language,
    channel_id: state.channelId,
    participant_id: state.participantId,
    event_slug: portal.dataset.eventSlug || '',
  })
}

function joinMonitoringFeed() {
  try {
    const rawUrl = portal.dataset.jitsiUrl
    if (!rawUrl) {
      showError('Jitsi meeting URL is required.')
      return
    }
    const meetingUrl = new URL(rawUrl)
    if (state.jitsiDomain && meetingUrl.host !== state.jitsiDomain) {
      showError(`Jitsi URL must use ${state.jitsiDomain}.`)
      return
    }
    const hashParams = new URLSearchParams({
      'config.startWithAudioMuted': 'true',
      'config.startWithVideoMuted': 'true',
      'config.prejoinPageEnabled': 'false',
      'config.disableInitialGUM': 'false',
      'config.disableDeepLinking': 'true',
      'config.toolbarButtons': '["microphone","camera","chat","participants-pane","tileview","fullscreen","settings"]',
      'interfaceConfig.TOOLBAR_BUTTONS': '["microphone","camera","chat","participants-pane","tileview","fullscreen","settings"]',
    })
    
    const dName = portal.dataset.displayName || 'Interpreter'
    if (dName) {
      hashParams.set('userInfo.displayName', `"${dName}"`)
    }
    
    const hash = hashParams.toString()
    elements.jitsiFrame.src = `${meetingUrl.origin}${meetingUrl.pathname}#${hash}`
    showError('')
  } catch (error) {
    showError(`Invalid Jitsi URL: ${error.message}`)
  }
}

async function populateMicDevices() {
  if (!navigator.mediaDevices) return
  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true })
    tempStream.getTracks().forEach((t) => t.stop())
  } catch {
    // Permission denied or no device — continue without labels
  }
  let devices = []
  try {
    devices = await navigator.mediaDevices.enumerateDevices()
  } catch {
    return
  }

  const virtualKeywords = ['zoom', 'teams', 'nomachine', 'blackhole', 'loopback', 'soundflower', 'obs', 'virtual', 'webex']
  const showVirtual = elements.showVirtualDevices.checked

  let audioInputs = devices.filter((d) => d.kind === 'audioinput')
  
  if (!showVirtual) {
    audioInputs = audioInputs.filter((d) => {
      const lower = d.label.toLowerCase()
      return !virtualKeywords.some((keyword) => lower.includes(keyword))
    })
  }

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

async function populateRelayDevices() {
  if (!navigator.mediaDevices || !elements.relayDeviceSelect) return
  let devices = []
  try {
    devices = await navigator.mediaDevices.enumerateDevices()
  } catch {
    return
  }
  let audioOutputs = devices.filter((d) => d.kind === 'audiooutput')

  const includeVirtual = elements.showVirtualRelayDevices && elements.showVirtualRelayDevices.checked
  if (!includeVirtual) {
    const virtualKeywords = ['zoom', 'teams', 'nomachine', 'blackhole', 'loopback', 'soundflower', 'obs', 'virtual', 'webex', 'eqmac', 'epoccam']
    audioOutputs = audioOutputs.filter((d) => {
      const lower = d.label.toLowerCase()
      return !virtualKeywords.some((keyword) => lower.includes(keyword))
    })
  }

  const previous = elements.relayDeviceSelect.value
  elements.relayDeviceSelect.innerHTML = ''

  const defaultOpt = document.createElement('option')
  defaultOpt.value = ''
  defaultOpt.textContent = 'Default output'
  elements.relayDeviceSelect.appendChild(defaultOpt)

  for (const device of audioOutputs) {
    const opt = document.createElement('option')
    opt.value = device.deviceId
    opt.textContent = device.label || `Speaker ${elements.relayDeviceSelect.options.length}`
    elements.relayDeviceSelect.appendChild(opt)
  }

  if (previous && elements.relayDeviceSelect.querySelector(`option[value="${CSS.escape(previous)}"]`)) {
    elements.relayDeviceSelect.value = previous
  }
}

// ── WHEP Clients ──────────────────────────────────────────────────────────────
const relayWhep = typeof createWhepClient === 'function' ? createWhepClient() : (typeof WhepListener !== 'undefined' ? WhepListener : null)
const boothWhep = typeof createWhepClient === 'function' ? createWhepClient() : (typeof WhepListener !== 'undefined' ? WhepListener : null)

// ── Relay Listening ───────────────────────────────────────────────────────────

let relayListening = false
let relayDefaultText = ''

function toggleRelayAudio() {
  if (!state.relayWhepUrl) return
  
  if (!relayDefaultText && elements.passRelay) {
    relayDefaultText = elements.passRelay.textContent.trim()
  }

  relayListening = !relayListening
  if (relayListening) {
    if (elements.passRelay) {
      elements.passRelay.classList.add('btn-primary')
      elements.passRelay.textContent = 'Stop Listening to Relay'
    }
    if (elements.relayStatus) elements.relayStatus.textContent = 'Connecting...'
    relayWhep.start({
      whepUrl: state.relayWhepUrl,
      audioEl: elements.relayAudio,
      onState: (st) => {
        if (!elements.relayStatus) return
        if (st.audioActive) {
          elements.relayStatus.textContent = 'Listening'
          elements.relayStatus.className = 'status-badge status-live'
        } else if (st.peerConnection === 'failed') {
          elements.relayStatus.textContent = 'Error'
          elements.relayStatus.className = 'status-badge status-disconnected'
        } else {
          elements.relayStatus.textContent = 'Connecting...'
          elements.relayStatus.className = 'status-badge status-warning'
        }
      }
    })
  } else {
    if (elements.passRelay) {
      elements.passRelay.classList.remove('btn-primary')
      elements.passRelay.textContent = relayDefaultText
    }
    if (elements.relayStatus) {
      elements.relayStatus.textContent = 'Not Listening'
      elements.relayStatus.className = 'status-badge'
    }
    relayWhep.stop()
  }
}

// ── Booth Audio Listening ─────────────────────────────────────────────────────

let boothListening = false

function startBoothAudioListening() {
  const whepUrl = state.whepUrl || (state.whipBase && state.channelId ? `${state.whipBase}/${encodeURIComponent(state.channelId)}/whep` : '')
  if (!whepUrl || !boothWhep) return
  if (boothListening) return

  boothListening = true
  boothWhep.start({
    whepUrl: whepUrl,
    audioEl: elements.boothAudio,
    onState: () => {}
  })
}

function stopBoothAudioListening() {
  if (!boothListening) return
  boothListening = false
  if (boothWhep) boothWhep.stop()
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

// ── Loopback Test ─────────────────────────────────────────────────────────────

async function startLoopbackTest() {
  if (loopbackRecorder || loopbackAudio) stopLoopbackTest()
  try {
    const deviceId = state.micDeviceId
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })
    
    elements.loopbackTestBtn.textContent = '⏹ Stop Test'
    elements.loopbackTestBtn.classList.add('btn-primary')
    elements.loopbackProgressRow.classList.remove('hidden')
    elements.loopbackStatus.textContent = 'Recording...'
    elements.loopbackProgress.value = 0
    
    const chunks = []
    loopbackRecorder = new MediaRecorder(stream)
    loopbackRecorder.ondataavailable = (e) => chunks.push(e.data)
    
    const startTime = Date.now()
    const durationMs = 5000
    
    const progressInterval = setInterval(() => {
      const elapsed = Date.now() - startTime
      const pct = Math.min(100, (elapsed / durationMs) * 100)
      elements.loopbackProgress.value = pct
    }, 50)
    
    loopbackRecorder.onstop = () => {
      clearInterval(progressInterval)
      stream.getTracks().forEach(t => t.stop())
      if (!elements.loopbackTestBtn.classList.contains('btn-primary')) return // Was stopped manually
      
      const blob = new Blob(chunks, { type: 'audio/webm' })
      const url = URL.createObjectURL(blob)
      loopbackAudio = new Audio(url)
      
      elements.loopbackStatus.textContent = 'Playing...'
      elements.loopbackProgress.value = 0
      
      loopbackAudio.onended = () => {
        stopLoopbackTest()
      }
      
      loopbackAudio.ontimeupdate = () => {
        if (!loopbackAudio) return
        const pct = (loopbackAudio.currentTime / loopbackAudio.duration) * 100
        elements.loopbackProgress.value = pct
      }
      
      loopbackAudio.play().catch(e => {
        showError(`Failed to play loopback audio: ${e.message}`)
        stopLoopbackTest()
      })
    }
    
    loopbackRecorder.start()
    setTimeout(() => {
      if (loopbackRecorder && loopbackRecorder.state === 'recording') {
        loopbackRecorder.stop()
      }
    }, durationMs)
    
  } catch (error) {
    showError(`Cannot access microphone: ${error.message}`)
    stopLoopbackTest()
  }
}

function stopLoopbackTest() {
  if (loopbackRecorder && loopbackRecorder.state !== 'inactive') {
    loopbackRecorder.stop()
  }
  loopbackRecorder = null
  
  if (loopbackAudio) {
    loopbackAudio.pause()
    loopbackAudio.src = ''
    loopbackAudio = null
  }
  
  elements.loopbackTestBtn.textContent = '🎙️ Record & Play'
  elements.loopbackTestBtn.classList.remove('btn-primary')
  elements.loopbackProgressRow.classList.add('hidden')
}

async function toggleMicMute() {
  // Flip state and update UI immediately for instant visual feedback.
  state.micMuted = !state.micMuted
  renderMicControls()
  // Then acquire the mic stream if not yet open (may take a moment).
  if (!state.micStream) {
    await ensureMicStream()
  }
  state.micStream.getAudioTracks().forEach((track) => {
    track.enabled = !state.micMuted
  })
  if (state.joined && state.participantId) {
    wsSend({
      type: 'booth:update-state',
      mic_active: !state.micMuted && state.ingestConnected,
    })
  }
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
  const location = response.headers.get('Location')
  if (location) {
    state.whipResourceUrl = new URL(location, whipUrl).href
  }
  const answerSdp = await response.text()
  await peerConnection.setRemoteDescription({ type: 'answer', sdp: answerSdp })
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
    if (state.micStream) {
      state.micStream.getAudioTracks().forEach((t) => { t.enabled = !state.micMuted })
    }
    if (state.peerConnection) {
      state.peerConnection.close()
      state.peerConnection = null
    }
    const peerConnection = new RTCPeerConnection({ iceServers: [] })
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
      await doWhipIngest(peerConnection)
    } else {
      throw new Error('MEDIAMTX_WHIP_BASE is not configured. Set it in your .env and restart the server.')
    }

    state.ingestConnected = true
    
    // Start backend transcription
    try {
      const payload = { event_slug: portal.dataset.eventSlug, language_code: portal.dataset.languageCode }
      await fetch(`/api/booth/${state.boothId}/transcription/start`, {
        method: 'POST',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      })
    } catch (err) {
      console.warn('Failed to start transcription worker:', err)
    }
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
  if (state.ingestConnected || state.whipResourceUrl) {
    const deleteUrl = state.whipResourceUrl
      || (state.whipBase && state.channelId
          ? `${state.whipBase}/${encodeURIComponent(state.channelId)}/whip`
          : null)
    state.whipResourceUrl = null
    if (deleteUrl) fetch(deleteUrl, { method: 'DELETE' }).catch(() => {})
  }
  if (state.peerConnection) {
    state.peerConnection.close()
    state.peerConnection = null
  }
  if (!micTestStream && !state.micStream) {
    stopMicMeter()
  }
  if (state.joined && state.participantId) {
    wsSend({
      type: 'booth:update-state',
      mic_active: false,
      ingest_connected: false,
    })
    
    // Stop backend transcription
    try {
      await fetch(`/api/booth/${state.boothId}/transcription/stop`, { method: 'POST', headers: authHeaders() })
    } catch (err) {
      console.warn('Failed to stop transcription worker:', err)
    }
  }
  state.ingestConnected = false
  renderMicControls()
}

// Retry intervals for WHIP on relay handoff (ms from previous attempt).
const _RELAY_RETRY_INTERVAL_MS = 200
const _RELAY_MAX_ATTEMPTS = 8

function attemptRelayStart(attempt) {
  if (attempt >= _RELAY_MAX_ATTEMPTS) return
  window.setTimeout(async () => {
    if (!state.joined || !state.participantId) return
    if (state.activeInterpreterId !== state.participantId) return
    if (state.ingestConnected) return
    try {
      await ensureMicStream()
      if (state.micStream) {
        state.micStream.getAudioTracks().forEach((t) => { t.enabled = !state.micMuted })
      }
      if (state.peerConnection) {
        state.peerConnection.close()
        state.peerConnection = null
      }
      const pc = new RTCPeerConnection({ iceServers: [] })
      state.peerConnection = pc
      state.micStream.getAudioTracks().forEach((t) => pc.addTrack(t, state.micStream))
      pc.addEventListener('connectionstatechange', () => {
        if (pc.connectionState === 'failed' || pc.connectionState === 'disconnected') {
          setBadge(elements.ingestStatus, 'Ingest reconnecting', 'warning')
        }
        if (pc.connectionState === 'connected') {
          setBadge(elements.ingestStatus, 'Ingest connected', 'success')
        }
      })
      const offer = await pc.createOffer({ offerToReceiveAudio: false, offerToReceiveVideo: false })
      await pc.setLocalDescription(offer)
      await waitForIceGathering(pc)
      if (!state.whipBase) throw new Error('MEDIAMTX_WHIP_BASE is not configured')
      await doWhipIngest(pc)
      state.ingestConnected = true
      wsSend({ type: 'booth:update-state', mic_active: !state.micMuted, ingest_connected: true })
      showError('')
    } catch (error) {
      if (state.peerConnection) {
        state.peerConnection.close()
        state.peerConnection = null
      }
      state.whipResourceUrl = null
      if (error.message.includes('409') && attempt < _RELAY_MAX_ATTEMPTS - 1) {
        attemptRelayStart(attempt + 1)
        return
      }
      showError(`Could not start relay: ${error.message}`)
    }
    renderMicControls()
  }, attempt === 0 ? 0 : _RELAY_RETRY_INTERVAL_MS)
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
  renderRoleControls()
  renderParticipants()
  renderChat()
  renderMicControls()
  renderHandoverButton()
  renderLiveBadge()
}

/**
 * Show/hide controls that are not available to the current role.
 */
function renderRoleControls() {
  const role = state.grantedRole
  if (!role) return

  const isListener = role === 'listener'
  const canGo = !isListener

  if (elements.handoverBtn) elements.handoverBtn.style.display = canGo ? '' : 'none'
  if (elements.toggleMic) elements.toggleMic.style.display = canGo ? '' : 'none'
  if (elements.passRelay) elements.passRelay.style.display = canGo ? '' : 'none'
}

function renderParticipants() {
  const currentParticipant = state.participants.find((p) => p.participant_id === state.participantId)
  const isActiveInterpreter = state.activeInterpreterId === state.participantId
  const canReassign = ['room_coordinator', 'event_owner', 'super_admin'].includes(currentParticipant?.role) || isActiveInterpreter
  const activeParticipant = state.participants.find((p) => p.participant_id === state.activeInterpreterId)
  setBadge(
    elements.activeIndicator,
    activeParticipant ? `${activeParticipant.display_name} is active` : 'No active interpreter',
    activeParticipant ? 'success' : 'warning',
  )
  if (elements.participantCount) elements.participantCount.textContent = state.participants.length
  elements.participantList.innerHTML = ''
  for (const participant of state.participants) {
    const tile = document.createElement('article')
    tile.className = 'participant-tile'
    if (participant.participant_id === state.activeInterpreterId) {
      tile.classList.add('active')
    }
    const canActivateSelf = participant.participant_id === state.participantId
    const canActivate = ['interpreter', 'room_coordinator', 'event_owner', 'super_admin'].includes(participant.role) && (canReassign || canActivateSelf)
    const isThisActive = participant.participant_id === state.activeInterpreterId

    const top = document.createElement('div')
    top.className = 'participant-top'
    const nameEl = document.createElement('strong')
    nameEl.textContent = participant.display_name
    if (participant.participant_id === state.participantId) {
      nameEl.textContent += ' (You)'
    }
    const rolePill = document.createElement('span')
    rolePill.className = `participant-pill${isThisActive ? ' live' : ''}`
    
    if (participant.role === 'interpreter') {
      rolePill.textContent = isThisActive ? 'Active' : 'Passive'
    } else {
      rolePill.textContent = participant.role.replace('_', ' ')
      rolePill.style.textTransform = 'capitalize'
    }
    
    top.append(nameEl, rolePill)

    const meta = document.createElement('div')
    meta.className = 'participant-meta'
    meta.textContent = `${participant.language} · ${participant.channel_id}`

    const bottom = document.createElement('div')
    bottom.className = 'participant-bottom'
    const pillGroup = document.createElement('div')
    const micPill = document.createElement('span')
    micPill.className = 'participant-pill'
    micPill.textContent = participant.mic_active ? 'mic active' : 'mic muted'
    const ingestPill = document.createElement('span')
    ingestPill.className = 'participant-pill'
    ingestPill.textContent = participant.ingest_connected ? 'ingest connected' : 'ingest idle'
    pillGroup.append(micPill, ingestPill)
    bottom.append(pillGroup)

    // Active/Passive status is shown via the role pill above.
    // Set Active button removed — use the handover protocol instead.

    tile.append(top, meta, bottom)
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
    const header = document.createElement('header')
    const senderEl = document.createElement('strong')
    senderEl.textContent = message.sender_name
    const timeEl = document.createElement('span')
    timeEl.textContent = formatTime(message.sent_at)
    header.append(senderEl, timeEl)
    const bodyEl = document.createElement('p')
    bodyEl.textContent = message.body
    entry.append(header, bodyEl)
    elements.chatLog.append(entry)
  }
  elements.chatLog.scrollTop = elements.chatLog.scrollHeight
}

function renderMicControls() {
  const isActive = state.joined && state.participantId === state.activeInterpreterId
  setBadge(
    elements.ingestStatus,
    state.ingestConnected ? 'Ingest connected' : 'Ingest disconnected',
    state.ingestConnected ? 'success' : 'warning',
  )
  elements.micState.textContent = state.micMuted ? 'Muted' : state.micStream ? 'Ready' : 'Inactive'

  const micOnIcon = elements.toggleMic.querySelector('.ctrl-icon--mic-on')
  const micOffIcon = elements.toggleMic.querySelector('.ctrl-icon--mic-off')
  if (micOnIcon) micOnIcon.classList.toggle('hidden', state.micMuted)
  if (micOffIcon) micOffIcon.classList.toggle('hidden', !state.micMuted)
  elements.toggleMic.classList.toggle('muted', state.micMuted)
  elements.toggleMic.classList.toggle('unmuted', !state.micMuted)
  if (elements.muteLabel) elements.muteLabel.textContent = state.micMuted ? 'UNMUTE' : 'MUTE'
  elements.toggleMic.setAttribute('aria-label', state.micMuted ? 'Unmute microphone' : 'Mute microphone')
  elements.toggleMic.setAttribute('title', state.micMuted ? 'Unmute (Space)' : 'Mute (Space)')

  elements.toggleMic.disabled = !isActive
  if (!state.broadcastUnlocked) {
    elements.toggleMic.setAttribute('title', 'Broadcast locked by Coordinator (Local test only)')
  }
  elements.micDeviceSelect.disabled = state.ingestConnected

  if (state.ingestConnected && portal.dataset.eventSlug) {
    const proto = window.location.protocol
    const host = window.location.host
    const slug = portal.dataset.eventSlug
    const url = `${proto}//${host}/listener/${slug}`
    if (elements.listenerUrlDisplay) elements.listenerUrlDisplay.textContent = url
    if (elements.listenerUrlRow) elements.listenerUrlRow.classList.remove('hidden')
  } else {
    if (elements.listenerUrlRow) elements.listenerUrlRow.classList.add('hidden')
  }
}

/**
 * Render the handover button based on the current handoff state machine.
 *
 * State matrix:
 * ┌─────────────┬───────────────────────────────────┬──────────────────────────────────────┐
 * │ State       │ Active Interpreter                │ Passive Interpreter                  │
 * ├─────────────┼───────────────────────────────────┼──────────────────────────────────────┤
 * │ idle        │ "PASS MIC" (grey, clickable)      │ "TAKE OVER" (grey, clickable)        │
 * │ offered     │ "PASS MIC" (green, frozen/cancel) │ "TAKE OVER" (flash yellow, clickable)│
 * │ requested   │ "PASS MIC" (flash yellow, click)  │ "TAKE OVER" (green, frozen/cancel)   │
 * └─────────────┴───────────────────────────────────┴──────────────────────────────────────┘
 */
function renderHandoverButton() {
  if (!elements.handoverBtn) return
  if (!state.joined || !state.participantId) {
    elements.handoverBtn.disabled = true
    elements.handoverBtn.className = 'header-btn header-btn--handover state-grey'
    elements.handoverLabel.textContent = 'PASS MIC'
    return
  }

  const isActive = state.activeInterpreterId === state.participantId
  const hState = state.handoffState
  const iAmInitiator = state.handoffInitiatorId === state.participantId
  const hasPartner = state.participants.filter(p => ['interpreter', 'room_coordinator', 'event_owner', 'super_admin'].includes(p.role)).length > 1

  // Remove all state classes
  elements.handoverBtn.classList.remove('state-grey', 'state-green', 'state-flash-yellow')

  if (isActive) {
    elements.handoverLabel.textContent = 'PASS MIC'

    if (hState === 'idle') {
      elements.handoverBtn.classList.add('state-grey')
      elements.handoverBtn.disabled = !hasPartner
    } else if (hState === 'offered' && iAmInitiator) {
      // I (active) offered → green, frozen (can cancel)
      elements.handoverBtn.classList.add('state-green')
      elements.handoverBtn.disabled = false
    } else if (hState === 'requested' && !iAmInitiator) {
      // Partner (passive) requested → flash yellow, clickable to yield
      elements.handoverBtn.classList.add('state-flash-yellow')
      elements.handoverBtn.disabled = false
    } else {
      elements.handoverBtn.classList.add('state-grey')
      elements.handoverBtn.disabled = true
    }
  } else {
    elements.handoverLabel.textContent = 'TAKE OVER'

    if (hState === 'idle') {
      elements.handoverBtn.classList.add('state-grey')
      elements.handoverBtn.disabled = !hasPartner
    } else if (hState === 'requested' && iAmInitiator) {
      // I (passive) requested → green, frozen (can cancel)
      elements.handoverBtn.classList.add('state-green')
      elements.handoverBtn.disabled = false
    } else if (hState === 'offered' && !iAmInitiator) {
      // Partner (active) offered → flash yellow, clickable to accept
      elements.handoverBtn.classList.add('state-flash-yellow')
      elements.handoverBtn.disabled = false
    } else {
      elements.handoverBtn.classList.add('state-grey')
      elements.handoverBtn.disabled = true
    }
  }
}

/** Update the LIVE / OFF LIVE / STANDBY badge in the header */
function renderLiveBadge() {
  if (!elements.liveBadge) return
  if (state.ingestConnected) {
    elements.liveBadge.classList.remove('off', 'standby')
    elements.liveBadge.classList.add('on')
    if (elements.liveBadgeText) elements.liveBadgeText.textContent = 'LIVE'
  } else if (state.broadcastUnlocked && state.joined && state.activeInterpreterId === state.participantId) {
    // Coordinator has unlocked this booth and we are the active interpreter —
    // show STANDBY so the interpreter knows they are cleared to go live.
    elements.liveBadge.classList.remove('off', 'on')
    elements.liveBadge.classList.add('standby')
    if (elements.liveBadgeText) elements.liveBadgeText.textContent = 'STANDBY'
  } else {
    elements.liveBadge.classList.remove('on', 'standby')
    elements.liveBadge.classList.add('off')
    if (elements.liveBadgeText) elements.liveBadgeText.textContent = 'OFF LIVE'
  }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function setBadge(element, text, tone = '') {
  if (!element) return
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
    }, 100)
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
