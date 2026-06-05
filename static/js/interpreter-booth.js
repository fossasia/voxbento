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
  whipBase: portal.dataset.whipBase || '',
  micDeviceId: localStorage.getItem('mic-device-id') || '',
  /** Role granted by the server (from JWT). Empty string = unknown / legacy. */
  grantedRole: portal.dataset.grantedRole || '',
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
  micState: document.getElementById('mic-state'),
  errorBanner: document.getElementById('error-banner'),
  participantList: document.getElementById('participant-list'),
  chatLog: document.getElementById('chat-log'),
  jitsiFrame: document.getElementById('jitsi-frame'),
  jitsiUrl: document.getElementById('jitsi-url'),
  joinJitsi: document.getElementById('join-jitsi'),
  joinLeaveBtn: document.getElementById('join-leave-btn'),
  joinLeaveLabel: document.getElementById('join-leave-label'),
  chatForm: document.getElementById('chat-form'),
  chatInput: document.getElementById('chat-input'),
  toggleMic: document.getElementById('toggle-mic'),
  goLive: document.getElementById('go-live'),
  passRelay: document.getElementById('pass-relay'),
  micDeviceSelect: document.getElementById('mic-device-select'),
  showVirtualDevices: document.getElementById('show-virtual-devices'),
  micMeter: document.getElementById('mic-meter'),
  meterRow: document.getElementById('meter-row'),
  micTestBtn: document.getElementById('mic-test-btn'),
  listenerUrlRow: document.getElementById('listener-url-row'),
  listenerUrlDisplay: document.getElementById('listener-url-display'),
  copyListenerUrl: document.getElementById('copy-listener-url'),
  muteLabel: document.getElementById('mute-label'),
  liveLabel: document.getElementById('live-label'),
  ctrlCompound: document.querySelector('.ctrl-compound'),
  // leaveBooth removed
  preflightRetry: document.getElementById('preflight-retry'),
  checkMicPermission: document.getElementById('check-mic-permission'),
}

// ── Audio context state ───────────────────────────────────────────────────────
let micTestStream = null
let micAnimFrame = null
let micAudioCtx = null
let micAnalyser = null


boot().catch((error) => {
  const msg = error instanceof Error ? error.message : String(error)
  showError(`Failed to boot interpreter portal: ${msg}`)
})

async function boot() {
  // Jitsi URL is set by the template — don't overwrite it
  await fetchBoothState()
  await fetchIngestReachability()
  await populateMicDevices()

  // Run preflights asynchronously before blocking on JWT/WS connection
  runPreflightChecks().catch((error) => {
    const msg = error instanceof Error ? error.message : String(error)
    showError(`Preflight checks failed: ${msg}`)
  })
  await acquireJwt()
  await connectWebSocket()
  bindEventHandlers()
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
    // skipAutoStart: the server auto-sets the first interpreter as active on
    // join, but the interpreter must press Go Live themselves.
    applyBoothState(data.state, { skipAutoStart: true })
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
  elements.joinLeaveBtn.addEventListener('click', async () => {
    if (!state.joined) {
      joinBooth()
    } else {
      if (state.ingestConnected) {
        await stopLiveIngest()
      }
      if (state.micStream) {
        state.micStream.getTracks().forEach((t) => t.stop())
        state.micStream = null
        stopMicMeter()
      }
      if (state.joined && state.participantId) {
        wsSend({ type: 'booth:leave' })
        state.joined = false
        state.participantId = null
        state.participants = []
        state.activeInterpreterId = null
        state.chatMessages = []
        leaveMonitoringFeed()
        render()
      }
    }
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
  // Always query the backend status endpoint so the UI reflects actual
  // MediaMTX availability rather than assuming reachable whenever whipBase
  // is configured. The early-return that forced ingestReachable=true when
  // whipBase was set has been removed.
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
  state.participants = payload.participants || []
  state.activeInterpreterId = payload.active_interpreter_id || null
  state.chatMessages = payload.chat_messages || []

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

  // This client just became the active interpreter (e.g. coordinator switched)
  const becameActive =
    state.joined &&
    state.participantId &&
    state.activeInterpreterId === state.participantId &&
    previousActiveInterpreterId !== state.participantId

  if (lostActivePublisher) {
    // With overridePublisher enabled on MediaMTX, the incoming interpreter
    // will take over the WHIP path immediately.  MediaMTX kicks our
    // peer-connection and seamlessly continues the WHEP stream, so viewers
    // never see a gap.  We just clean up our side.
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

  if (!skipAutoStart && becameActive && !state.ingestConnected && state.ingestReachable) {
    // Unmute immediately so audio is ready the moment WHIP connects.
    if (state.micMuted) {
      state.micMuted = false
      if (state.micStream) {
        state.micStream.getAudioTracks().forEach((t) => { t.enabled = true })
      }
      renderMicControls()
    }
    // With overridePublisher enabled on MediaMTX, the first attempt succeeds
    // immediately (no 409 Conflict). The retry logic is kept as a safety net
    // for edge cases (network hiccups, slow ICE gathering).
    attemptRelayStart(0)
  }
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
    })
    
    // Add user info to Jitsi URL config
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
  if (!navigator.mediaDevices) {
    // Non-secure context or unsupported browser — skip device enumeration gracefully
    return
  }
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
    // Cannot enumerate — proceed without device list
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
  // Save the WHIP resource URL so we can DELETE it for clean teardown.
  // Explicit DELETE lets MediaMTX release the path immediately instead of
  // waiting for ICE timeout (~5-10s), enabling fast relay handoff.
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
    // Ensure track.enabled is consistent with micMuted before adding to the
    // peer connection (guards against tracks being left disabled by a
    // previous silence-mode handoff).
    if (state.micStream) {
      state.micStream.getAudioTracks().forEach((t) => { t.enabled = !state.micMuted })
    }
    if (state.peerConnection) {
      state.peerConnection.close()
      state.peerConnection = null
    }
    // Empty iceServers: skip STUN for local dev (browser defaults add
    // Google STUN which adds 500ms+ to ICE gathering).
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
  // DELETE the WHIP session to release the MediaMTX path immediately.
  // Use the resource URL from the Location header if captured; otherwise
  // fall back to the standard WHIP endpoint for this channel.
  // Only send DELETE if WHIP was actually connected (avoids spurious 404s).
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
  // Keep the mic stream alive so a relay handoff can reuse it without
  // requesting mic permission again. The stream is stopped only when the
  // participant explicitly leaves the booth.
  if (!micTestStream && !state.micStream) {
    stopMicMeter()
  }
  if (state.joined && state.participantId) {
    wsSend({
      type: 'booth:update-state',
      mic_active: false,
      ingest_connected: false,
    })
  }
  state.ingestConnected = false
  renderMicControls()
}

// Retry intervals for WHIP on relay handoff (ms from previous attempt).
// Outgoing interpreter stops at ~700ms, so the 800ms mark (2 × 400ms) wins.
const _RELAY_RETRY_INTERVAL_MS = 200
const _RELAY_MAX_ATTEMPTS = 8

function attemptRelayStart(attempt) {
  if (attempt >= _RELAY_MAX_ATTEMPTS) return
  window.setTimeout(async () => {
    // Bail if conditions changed while waiting
    if (!state.joined || !state.participantId) return
    if (state.activeInterpreterId !== state.participantId) return
    if (state.ingestConnected) return
    try {
      await ensureMicStream()
      // Restore track.enabled (may have been muted during silence-mode handoff)
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
      // 409 = path busy (outgoing interpreter still in silence mode); retry
      if (error.message.includes('409') && attempt < _RELAY_MAX_ATTEMPTS - 1) {
        attemptRelayStart(attempt + 1)
        return
      }
      showError(`Could not start relay: ${error.message}`)
    }
    renderMicControls()
  }, attempt === 0 ? 0 : _RELAY_RETRY_INTERVAL_MS)
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
  renderRoleControls()
  renderParticipants()
  renderChat()
  renderMicControls()
}

/**
 * Show/hide controls that are not available to the current role.
 * - Listeners: hide Go Live, Relay, mic controls (they can only chat + observe).
 * - Interpreters: show Go Live + Relay, hide coordinator-only actions.
 * - Coordinators/admins: show all controls.
 */
function renderRoleControls() {
  const role = state.grantedRole
  if (!role) return  // unknown / legacy path — leave everything visible

  const isListener = role === 'listener'
  const canGo = !isListener  // interpreters + coordinators + admins can go live

  if (elements.goLive) elements.goLive.style.display = canGo ? '' : 'none'
  if (elements.passRelay) elements.passRelay.style.display = canGo ? '' : 'none'
  if (elements.ctrlCompound) elements.ctrlCompound.style.display = canGo ? '' : 'none'
}

function renderParticipants() {
  const currentParticipant = state.participants.find((p) => p.participant_id === state.participantId)
  // Coordinator or the currently-active interpreter may reassign any interpreter.
  // This aligns with the server permission model (booth_state.set_active_interpreter).
  const isActiveInterpreter = state.activeInterpreterId === state.participantId
  const canReassign = currentParticipant?.role === 'coordinator' || isActiveInterpreter
  const activeParticipant = state.participants.find((p) => p.participant_id === state.activeInterpreterId)
  setBadge(
    elements.activeIndicator,
    activeParticipant ? `${activeParticipant.display_name} is active` : 'No active interpreter',
    activeParticipant ? 'success' : 'warning',
  )
  elements.participantList.innerHTML = ''
  for (const participant of state.participants) {
    const tile = document.createElement('article')
    tile.className = 'participant-tile'
    if (participant.participant_id === state.activeInterpreterId) {
      tile.classList.add('active')
    }
    const canActivateSelf = participant.participant_id === state.participantId
    const canActivate = participant.role === 'interpreter' && (canReassign || canActivateSelf)
    const isThisActive = participant.participant_id === state.activeInterpreterId

    // Build participant tile using DOM construction (no innerHTML) to prevent XSS.
    const top = document.createElement('div')
    top.className = 'participant-top'
    const nameEl = document.createElement('strong')
    nameEl.textContent = participant.display_name
    const rolePill = document.createElement('span')
    rolePill.className = `participant-pill${isThisActive ? ' live' : ''}`
    rolePill.textContent = isThisActive ? 'LIVE' : participant.role
    top.append(nameEl, rolePill)

    const meta = document.createElement('div')
    meta.className = 'participant-meta'
    meta.textContent = `${participant.language} \u00b7 ${participant.channel_id}`

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

    // Set Active / Active button:
    //   active tile  → green "Active" badge (visible to all, no action needed)
    //   non-active   → "Set Active" button only if this user can reassign
    if (participant.role === 'interpreter') {
      const btn = document.createElement('button')
      btn.type = 'button'
      if (isThisActive) {
        btn.className = 'btn btn-active-status'
        btn.disabled = true
        btn.textContent = 'Active'
        bottom.append(btn)
      } else if (canActivate) {
        btn.className = 'btn set-active-btn'
        btn.dataset.participantId = participant.participant_id
        btn.textContent = 'Set Active'
        bottom.append(btn)
      }
    }

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
  const joinedActiveInterpreter = state.joined && state.participantId === state.activeInterpreterId
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
  if (elements.ctrlCompound) elements.ctrlCompound.classList.toggle('muted', state.micMuted)
  if (elements.muteLabel) elements.muteLabel.textContent = state.micMuted ? 'UNMUTE' : 'MUTE'
  elements.toggleMic.setAttribute('aria-label', state.micMuted ? 'Unmute microphone' : 'Mute microphone')
  elements.toggleMic.setAttribute('title', state.micMuted ? 'Unmute (Space)' : 'Mute (Space)')

  elements.goLive.classList.toggle('live', state.ingestConnected)
  if (elements.liveLabel) elements.liveLabel.textContent = state.ingestConnected ? 'STOP' : 'GO LIVE'

  const isJoined = state.joined
  elements.joinLeaveBtn.title = isJoined ? 'Leave booth' : 'Join booth'
  elements.joinLeaveBtn.setAttribute('aria-label', isJoined ? 'Leave booth' : 'Join booth')
  if (elements.joinLeaveLabel) elements.joinLeaveLabel.textContent = isJoined ? 'LEAVE' : 'JOIN'
  elements.joinLeaveBtn.classList.toggle('ctrl-btn--danger', isJoined)
  elements.joinLeaveBtn.classList.toggle('ctrl-btn--primary', !isJoined)

  elements.toggleMic.disabled = !joinedActiveInterpreter
  const preflightCriticalPass =
    state.preflight.micPermission === 'pass' &&
      state.preflight.serverReachable !== 'fail'
  elements.goLive.disabled = !state.ingestConnected && (!joinedActiveInterpreter || !state.ingestReachable || !preflightCriticalPass)
  elements.passRelay.disabled = !joinedActiveInterpreter
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
