const portal = document.getElementById('interpreter-portal')

if (!portal) {
  throw new Error('Interpreter portal root element is missing.')
}

const state = {
  socketConnected: false,
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
  micDeviceId: localStorage.getItem('mic-device-id') || '',
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
}

// ── Audio context state (not reflected directly in UI) ────────────────────
let micTestStream = null
let micAnimFrame = null
let micAudioCtx = null
let micAnalyser = null

const socket = io()

boot().catch((error) => {
  showError(`Failed to boot interpreter portal: ${error.message}`)
})

async function boot() {
  elements.jitsiUrl.value = state.defaultJitsiRoom
  await fetchBoothState()
  await fetchIngestReachability()
  await populateMicDevices()
  bindEventHandlers()
  render()
}

function bindEventHandlers() {
  socket.on('connect', () => {
    state.socketConnected = true
    setBadge(elements.connectionStatus, 'Connected', 'success')
  })

  socket.on('disconnect', () => {
    state.socketConnected = false
    setBadge(elements.connectionStatus, 'Disconnected', 'warning')
  })

  socket.on('booth:joined', (payload) => {
    state.participantId = payload.participant_id
    state.joined = true
    applyBoothState(payload.state)
    render()
    showError('')
  })

  socket.on('booth:state', (payload) => {
    applyBoothState(payload)
    render()
  })

  socket.on('booth:chat', (message) => {
    state.chatMessages.push(message)
    renderChat()
  })

  socket.on('booth:error', (payload) => {
    showError(payload.message || 'An unknown booth error occurred.')
  })

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
    // If a test or live stream is active with a different device, restart it
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

  navigator.mediaDevices.addEventListener('devicechange', () => {
    populateMicDevices().catch(() => {})
  })

  elements.participantList.addEventListener('click', (event) => {
    const target = event.target
    if (!(target instanceof HTMLElement)) return
    if (!target.classList.contains('set-active-btn')) return
    const participantId = target.dataset.participantId
    if (!participantId) return
    socket.emit('booth:set-active', {
      booth_id: state.boothId,
      requester_id: state.participantId,
      target_id: participantId,
      language: state.language,
      channel_id: state.channelId,
    })
  })

  window.addEventListener('beforeunload', () => {
    if (!state.joined || !state.participantId) return
    socket.emit('booth:leave', {
      booth_id: state.boothId,
      participant_id: state.participantId,
      language: state.language,
      channel_id: state.channelId,
    })
  })
}

async function fetchBoothState() {
  const url = new URL(`/api/booth/${encodeURIComponent(state.boothId)}/state`, window.location.origin)
  url.searchParams.set('token', state.token)
  url.searchParams.set('language', state.language)
  url.searchParams.set('channel', state.channelId)
  const response = await fetch(url)
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ error: response.statusText }))
    throw new Error(payload.error || 'Failed to fetch booth state.')
  }
  const payload = await response.json()
  applyBoothState(payload)
}

async function fetchIngestReachability() {
  if (state.whipBase) {
    // WHIP path: MediaMTX is configured; treat as reachable. Actual connectivity
    // is validated at publish time (startLiveIngest will surface errors on failure).
    state.ingestReachable = true
    return
  }
  // Legacy: check aiortc/FFmpeg reachability via Flask
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

function joinBooth() {
  const displayName = elements.displayName.value.trim()
  state.language = elements.language.value.trim() || state.language
  state.channelId = elements.channel.value.trim() || state.channelId
  socket.emit('booth:join', {
    booth_id: state.boothId,
    token: state.token,
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
  // A brief getUserMedia call is required before enumerateDevices returns device labels.
  // The stream is immediately stopped — this only grants the label permission.
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

  // Restore saved selection
  const saved = state.micDeviceId
  if (saved && elements.micDeviceSelect.querySelector(`option[value="${CSS.escape(saved)}"]`)) {
    elements.micDeviceSelect.value = saved
  } else if (previous && elements.micDeviceSelect.querySelector(`option[value="${CSS.escape(previous)}"]`)) {
    elements.micDeviceSelect.value = previous
  }
}

async function ensureMicStream() {
  if (state.micStream) return state.micStream
  // If a test stream is active on the same device, stop it — live stream replaces it
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
  // Only stop the meter if we're not currently live
  if (!state.ingestConnected) {
    stopMicMeter()
  }
}

function startMicMeter(stream) {
  stopMicMeter() // clean up any previous context first
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
    const volume = avg / 128 // normalise 0..1

    ctx.clearRect(0, 0, canvas.width, canvas.height)
    // Track background
    ctx.fillStyle = '#e9ecef'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    // Active bar — colour reflects level
    if (volume > 0.95) {
      ctx.fillStyle = '#dc3545' // red — clipping
    } else if (volume > 0.75) {
      ctx.fillStyle = '#fd7e14' // amber — loud
    } else {
      ctx.fillStyle = '#22c55e' // green — normal
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
    socket.emit('booth:update-state', {
      booth_id: state.boothId,
      participant_id: state.participantId,
      language: state.language,
      channel_id: state.channelId,
      mic_active: !state.micMuted && state.ingestConnected,
    })
  }
  renderMicControls()
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
      // WHIP path: POST raw SDP offer to MediaMTX; receive SDP answer as plain text.
      // MediaMTX WHIP URL format is /{channelId}/whip (not /whip/{channelId}).
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
    } else {
      // Legacy: aiortc path via Flask
      const response = await fetch(`/api/interpreter/connect/${encodeURIComponent(state.channelId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
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
        throw new Error(payload.error || 'Ingest negotiation failed.')
      }
      const answer = await response.json()
      await peerConnection.setRemoteDescription(answer)
    }

    state.ingestConnected = true
    socket.emit('booth:update-state', {
      booth_id: state.boothId,
      participant_id: state.participantId,
      language: state.language,
      channel_id: state.channelId,
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
  // Release live mic stream and stop the meter (unless a test is still active)
  if (state.micStream) {
    state.micStream.getTracks().forEach((t) => t.stop())
    state.micStream = null
  }
  if (!micTestStream) {
    stopMicMeter()
  }
  if (state.joined && state.participantId) {
    if (!state.whipBase) {
      // Legacy: notify Flask to release the server-side aiortc peer connection
      await fetch(`/api/interpreter/disconnect/${encodeURIComponent(state.channelId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          booth_id: state.boothId,
          participant_id: state.participantId,
          language: state.language,
          token: state.token,
        }),
      }).catch(() => {})
    }
    socket.emit('booth:update-state', {
      booth_id: state.boothId,
      participant_id: state.participantId,
      language: state.language,
      channel_id: state.channelId,
      mic_active: false,
      ingest_connected: false,
    })
  }
  state.ingestConnected = false
  renderMicControls()
}

function passRelayToNextInterpreter() {
  if (!state.joined || !state.participantId) return
  const interpreters = state.participants.filter((participant) => participant.role === 'interpreter')
  if (interpreters.length < 2) {
    showError('At least two interpreters are required for relay handoff.')
    return
  }
  const currentIndex = interpreters.findIndex((participant) => participant.participant_id === state.activeInterpreterId)
  const nextIndex = currentIndex >= 0 ? (currentIndex + 1) % interpreters.length : 0
  const nextInterpreter = interpreters[nextIndex]
  socket.emit('booth:set-active', {
    booth_id: state.boothId,
    requester_id: state.participantId,
    target_id: nextInterpreter.participant_id,
    language: state.language,
    channel_id: state.channelId,
  })
}

function sendChatMessage() {
  const body = elements.chatInput.value.trim()
  if (!body) return
  if (!state.participantId) {
    showError('Join the booth before sending messages.')
    return
  }
  socket.emit('booth:chat', {
    booth_id: state.boothId,
    sender_id: state.participantId,
    language: state.language,
    channel_id: state.channelId,
    body,
  })
  elements.chatInput.value = ''
}

function render() {
  renderParticipants()
  renderChat()
  renderMicControls()
}

function renderParticipants() {
  const currentParticipant = state.participants.find((participant) => participant.participant_id === state.participantId)
  const canReassign = currentParticipant?.role === 'coordinator'
  const activeParticipant = state.participants.find((participant) => participant.participant_id === state.activeInterpreterId)
  setBadge(
    elements.activeIndicator,
    activeParticipant ? `${activeParticipant.display_name} is active` : 'No active interpreter',
    activeParticipant ? 'success' : 'warning'
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
  setBadge(elements.ingestStatus, state.ingestConnected ? 'Ingest connected' : 'Ingest disconnected', state.ingestConnected ? 'success' : 'warning')
  elements.ingestReachable.textContent = state.ingestReachable ? 'Reachable' : 'Unavailable'
  elements.micState.textContent = state.micMuted ? 'Muted' : state.micStream ? 'Ready' : 'Inactive'
  elements.toggleMic.textContent = state.micMuted ? 'Unmute' : 'Mute'
  elements.goLive.textContent = state.ingestConnected ? 'Stop Live' : 'Go Live'
  elements.toggleMic.disabled = !state.joined
  elements.goLive.disabled = !state.ingestConnected && (!joinedActiveInterpreter || !state.ingestReachable)
  elements.passRelay.disabled = !joinedActiveInterpreter
  // Disable device selector while streaming — cannot switch device mid-stream
  elements.micDeviceSelect.disabled = state.ingestConnected
  // Show HLS stream URL once live so the interpreter can paste it into VLC
  if (state.ingestConnected && state.hlsBase) {
    const url = `${state.hlsBase}/${encodeURIComponent(state.channelId)}/index.m3u8`
    elements.hlsUrlDisplay.textContent = url
    elements.hlsUrlRow.classList.remove('hidden')
  } else {
    elements.hlsUrlRow.classList.add('hidden')
  }
}

function setBadge(element, text, tone = '') {
  element.textContent = text
  element.classList.remove('success', 'warning', 'danger')
  if (tone) {
    element.classList.add(tone)
  }
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
