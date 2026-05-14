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
}

const socket = io()

boot().catch((error) => {
  showError(`Failed to boot interpreter portal: ${error.message}`)
})

async function boot() {
  elements.jitsiUrl.value = state.defaultJitsiRoom
  await fetchBoothState()
  await fetchIngestReachability()
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

async function ensureMicStream() {
  if (state.micStream) return state.micStream
  state.micStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  })
  return state.micStream
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
    showError('Ingest backend is unavailable. Check aiortc/FFmpeg setup.')
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
  if (state.joined && state.participantId) {
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
