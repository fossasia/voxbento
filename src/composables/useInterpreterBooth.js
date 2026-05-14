import { computed, reactive } from 'vue'
import { env } from '../config/env'
import { IngestClient } from '../services/ingestClient'
import { buildJitsiEmbedUrl } from '../services/jitsiEmbed'
import { MicStreamingManager } from '../services/micStreamingManager'
import { BoothRealtimeClient, boothRealtimeEvents } from '../services/boothRealtime'

const MAX_RECONNECT_ATTEMPTS = 5

function createInitialParticipants(language, channelId) {
  return [
    {
      id: 'local-interpreter',
      displayName: 'You',
      role: 'Active Interpreter',
      language,
      channelId,
      micState: 'ready',
      speaking: false,
      connectionState: 'connected',
      ingestState: 'disconnected',
      reconnecting: false,
      isLive: true
    },
    {
      id: 'backup-interpreter',
      displayName: 'Amira R.',
      role: 'Backup Interpreter',
      language,
      channelId,
      micState: 'ready',
      speaking: false,
      connectionState: 'connected',
      ingestState: 'standby',
      reconnecting: false,
      isLive: false
    },
    {
      id: 'booth-coordinator',
      displayName: 'Kai L.',
      role: 'Coordinator',
      language,
      channelId,
      micState: 'muted',
      speaking: false,
      connectionState: 'connected',
      ingestState: 'monitoring',
      reconnecting: false,
      isLive: false
    },
    {
      id: 'listener',
      displayName: 'Relay Standby',
      role: 'Listener',
      language,
      channelId,
      micState: 'muted',
      speaking: false,
      connectionState: 'connected',
      ingestState: 'standby',
      reconnecting: false,
      isLive: false
    }
  ]
}

function mergeParticipant(list, update) {
  const index = list.findIndex((item) => item.id === update.id)
  if (index === -1) {
    list.push(update)
    return
  }
  list[index] = {
    ...list[index],
    ...update
  }
}

function buildSessionKey(eventSlug, boothId) {
  return `${eventSlug}:${boothId}`
}

function toLocalChatMessage(rawMessage) {
  return {
    id: rawMessage.id || `chat-${Date.now()}`,
    senderId: rawMessage.senderId,
    senderName: rawMessage.senderName,
    body: rawMessage.body,
    sentAt: rawMessage.sentAt || Date.now()
  }
}

export function useInterpreterBooth() {
  const ingestClient = new IngestClient({
    baseUrl: env.ingestBaseUrl,
    authToken: env.ingestAuthToken
  })
  const micManager = new MicStreamingManager({
    stunServers: env.stunServers
  })

  const state = reactive({
    initialized: false,
    localParticipantId: 'local-interpreter',
    localRole: 'Coordinator',
    session: {
      eventSlug: env.defaultEventSlug,
      boothId: env.defaultBoothId,
      language: env.defaultLanguage,
      channelId: env.defaultChannelId
    },
    jitsi: {
      inputUrl: env.defaultJitsiUrl,
      embedUrl: '',
      roomName: '',
      status: 'idle',
      error: ''
    },
    mic: {
      status: 'idle',
      level: 0,
      devices: [],
      selectedDeviceId: ''
    },
    ingest: {
      status: 'disconnected',
      connectionState: 'new',
      reconnecting: false,
      retries: 0,
      streamingLive: false,
      bitrateKbps: 0,
      error: ''
    },
    preflight: {
      headphonesConnected: false,
      monitoringActive: false,
      micTestComplete: false,
      ingestReachable: false
    },
    participants: createInitialParticipants(env.defaultLanguage, env.defaultChannelId),
    activeParticipantId: 'local-interpreter',
    chatMessages: []
  })

  let realtimeClient = null
  let reconnectTimer = null
  let manualStop = false
  let lastSpeakingSent = false

  const activeParticipant = computed(() => {
    return state.participants.find((participant) => participant.id === state.activeParticipantId) || null
  })

  const health = computed(() => ({
    ingestHealthy: state.ingest.status === 'connected',
    liveStreamingActive: state.ingest.streamingLive,
    activeInterpreter: activeParticipant.value?.displayName || 'Unassigned',
    packetHealth: state.ingest.bitrateKbps > 20 ? 'healthy' : state.ingest.streamingLive ? 'degraded' : 'idle',
    bitrateKbps: state.ingest.bitrateKbps
  }))

  async function initialize({ eventSlug, boothId }) {
    state.session.eventSlug = eventSlug || env.defaultEventSlug
    state.session.boothId = boothId || env.defaultBoothId
    state.participants = createInitialParticipants(state.session.language, state.session.channelId)
    state.activeParticipantId = state.localParticipantId
    await refreshInputDevices()
    setupRealtime()
    state.chatMessages = realtimeClient.readPersistedChat()
    await probeIngest()
    if (state.jitsi.inputUrl) {
      joinJitsi(state.jitsi.inputUrl)
    }
    state.initialized = true
  }

  function setupRealtime() {
    if (realtimeClient) {
      realtimeClient.close()
    }
    const key = buildSessionKey(state.session.eventSlug, state.session.boothId)
    realtimeClient = new BoothRealtimeClient(key, {
      websocketUrl: env.boothWsUrl,
      authToken: env.ingestAuthToken
    })
    realtimeClient.connect((envelope) => {
      if (!envelope || !envelope.eventType) return
      if (envelope.eventType === boothRealtimeEvents.EVENT_CHAT) {
        const message = toLocalChatMessage(envelope.payload)
        if (state.chatMessages.some((existing) => existing.id === message.id)) return
        state.chatMessages.push(message)
        realtimeClient.persistChat(state.chatMessages)
        return
      }
      if (envelope.eventType === boothRealtimeEvents.EVENT_ACTIVE_INTERPRETER) {
        applyActiveInterpreter(envelope.payload.participantId, true)
        return
      }
      if (envelope.eventType === boothRealtimeEvents.EVENT_PARTICIPANT_STATE) {
        mergeParticipant(state.participants, envelope.payload)
      }
    })
  }

  async function refreshInputDevices() {
    state.mic.devices = await micManager.listInputDevices()
    if (!state.mic.devices.length) {
      state.mic.selectedDeviceId = ''
      return
    }
    if (!state.mic.selectedDeviceId) {
      state.mic.selectedDeviceId = state.mic.devices[0].deviceId
    }
  }

  async function probeIngest() {
    try {
      state.preflight.ingestReachable = await ingestClient.checkReachable(state.session.channelId)
      state.ingest.error = ''
    } catch (error) {
      console.error('Ingest probe failed', error)
      state.preflight.ingestReachable = false
      state.ingest.error = error.message
    }
  }

  function joinJitsi(inputUrl) {
    try {
      const joined = buildJitsiEmbedUrl(inputUrl, {
        expectedDomain: env.jitsiDomain
      })
      state.jitsi.inputUrl = inputUrl
      state.jitsi.embedUrl = joined.embedUrl
      state.jitsi.roomName = joined.roomName
      state.jitsi.status = 'connecting'
      state.jitsi.error = ''
      state.preflight.monitoringActive = true
    } catch (error) {
      console.error('Invalid Jitsi URL', error)
      state.jitsi.error = error.message
      state.jitsi.status = 'failed'
      state.preflight.monitoringActive = false
    }
  }

  function onJitsiLoaded() {
    state.jitsi.status = 'connected'
  }

  function setAudioDevice(deviceId) {
    state.mic.selectedDeviceId = deviceId
  }

  async function runMicTest() {
    try {
      state.mic.status = 'testing'
      await micManager.startMicrophone(state.mic.selectedDeviceId, onMicLevel)
      state.preflight.micTestComplete = true
      updateLocalParticipant({
        micState: 'open'
      })
      state.mic.status = 'ready'
    } catch (error) {
      console.error('Mic test failed', error)
      state.mic.status = 'error'
      state.ingest.error = error.message
    }
  }

  function onMicLevel(level) {
    state.mic.level = level
    const speaking = level >= 0.12
    updateLocalParticipant({
      speaking
    })
    if (speaking === lastSpeakingSent) return
    lastSpeakingSent = speaking
    realtimeClient?.send(boothRealtimeEvents.EVENT_PARTICIPANT_STATE, {
      id: state.localParticipantId,
      speaking
    })
  }

  async function startInterpretation({ reconnecting = false } = {}) {
    if (state.ingest.status === 'connecting') return
    if (state.activeParticipantId !== state.localParticipantId) {
      state.ingest.error = 'You are not the currently active interpreter for this booth channel.'
      return
    }
    manualStop = false
    window.clearTimeout(reconnectTimer)
    state.ingest.status = reconnecting ? 'reconnecting' : 'connecting'
    state.ingest.reconnecting = reconnecting
    state.ingest.error = ''
    try {
      if (state.mic.status === 'idle' || state.mic.status === 'error') {
        await runMicTest()
      }
      const localDescription = await micManager.createIngestConnection(onConnectionStateChange)
      const answer = await ingestClient.negotiate(state.session.channelId, localDescription)
      await micManager.applyRemoteAnswer(answer)
      micManager.startStats((bitrateKbps) => {
        state.ingest.bitrateKbps = bitrateKbps
      })
      state.ingest.status = 'connected'
      state.ingest.streamingLive = true
      state.ingest.reconnecting = false
      state.ingest.retries = 0
      state.preflight.ingestReachable = true
      updateLocalParticipant({
        micState: 'live',
        ingestState: 'connected',
        reconnecting: false
      })
    } catch (error) {
      console.error('Failed to start interpretation', error)
      state.ingest.error = error.message
      state.ingest.status = 'failed'
      state.ingest.streamingLive = false
      updateLocalParticipant({
        micState: 'error',
        ingestState: 'failed',
        reconnecting: true
      })
      scheduleReconnect()
    }
  }

  function onConnectionStateChange(connectionState) {
    state.ingest.connectionState = connectionState
    updateLocalParticipant({
      connectionState
    })
    if (['connected', 'completed'].includes(connectionState)) {
      state.ingest.status = 'connected'
      state.ingest.reconnecting = false
      return
    }
    if (!['failed', 'disconnected', 'closed'].includes(connectionState)) return
    if (manualStop) return
    state.ingest.status = 'reconnecting'
    state.ingest.reconnecting = true
    scheduleReconnect()
  }

  function scheduleReconnect() {
    window.clearTimeout(reconnectTimer)
    if (state.ingest.retries >= MAX_RECONNECT_ATTEMPTS) {
      state.ingest.status = 'failed'
      state.ingest.reconnecting = false
      state.ingest.streamingLive = false
      updateLocalParticipant({
        ingestState: 'failed',
        reconnecting: false
      })
      return
    }
    const backoff = Math.min(1500 * 2 ** state.ingest.retries, 12000)
    state.ingest.retries += 1
    reconnectTimer = window.setTimeout(() => {
      startInterpretation({ reconnecting: true })
    }, backoff)
  }

  function stopInterpretation() {
    manualStop = true
    window.clearTimeout(reconnectTimer)
    reconnectTimer = null
    micManager.stopIngest()
    state.ingest.status = 'disconnected'
    state.ingest.connectionState = 'closed'
    state.ingest.reconnecting = false
    state.ingest.streamingLive = false
    state.ingest.bitrateKbps = 0
    updateLocalParticipant({
      micState: 'ready',
      ingestState: 'disconnected',
      reconnecting: false
    })
  }

  function applyActiveInterpreter(participantId, fromRemote = false) {
    const localLosingLiveOwnership =
      participantId !== state.localParticipantId &&
      state.activeParticipantId === state.localParticipantId &&
      state.ingest.streamingLive
    if (localLosingLiveOwnership) {
      stopInterpretation()
    }
    state.activeParticipantId = participantId
    state.participants = state.participants.map((participant) => ({
      ...participant,
      isLive: participant.id === participantId
    }))
    if (!fromRemote) {
      realtimeClient?.send(boothRealtimeEvents.EVENT_ACTIVE_INTERPRETER, {
        participantId
      })
    }
  }

  function setActiveInterpreter(participantId) {
    const participant = state.participants.find((candidate) => candidate.id === participantId)
    if (!participant) return
    const canOverride = state.localRole === 'Coordinator'
    const canSelfActivate = participant.id === state.localParticipantId
    if (!canOverride && !canSelfActivate) return
    if (participantId !== state.localParticipantId && state.ingest.streamingLive) {
      stopInterpretation()
    }
    applyActiveInterpreter(participantId, false)
  }

  function updateLocalParticipant(patch) {
    const participant = state.participants.find((item) => item.id === state.localParticipantId)
    if (!participant) return
    mergeParticipant(state.participants, {
      ...participant,
      ...patch
    })
    realtimeClient?.send(boothRealtimeEvents.EVENT_PARTICIPANT_STATE, {
      id: state.localParticipantId,
      ...patch
    })
  }

  function toggleChecklistItem(key) {
    if (!(key in state.preflight)) return
    state.preflight[key] = !state.preflight[key]
  }

  function sendBoothChatMessage(rawText) {
    const body = rawText.trim()
    if (!body) return
    const localParticipant = state.participants.find((participant) => participant.id === state.localParticipantId)
    const message = toLocalChatMessage({
      id: `${state.localParticipantId}-${Date.now()}`,
      senderId: state.localParticipantId,
      senderName: localParticipant?.displayName || 'Unknown',
      body,
      sentAt: Date.now()
    })
    state.chatMessages.push(message)
    realtimeClient?.persistChat(state.chatMessages)
    realtimeClient?.send(boothRealtimeEvents.EVENT_CHAT, message)
  }

  function teardown() {
    stopInterpretation()
    micManager.stopAll()
    realtimeClient?.close()
    realtimeClient = null
    state.initialized = false
  }

  return {
    state,
    health,
    activeParticipant,
    initialize,
    teardown,
    refreshInputDevices,
    joinJitsi,
    onJitsiLoaded,
    setAudioDevice,
    runMicTest,
    startInterpretation,
    stopInterpretation,
    setActiveInterpreter,
    toggleChecklistItem,
    sendBoothChatMessage
  }
}
