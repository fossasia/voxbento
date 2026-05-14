const EVENT_CHAT = 'booth-chat'
const EVENT_ACTIVE_INTERPRETER = 'active-interpreter'
const EVENT_PARTICIPANT_STATE = 'participant-state'
const EVENT_CHAT_STORAGE_PREFIX = 'eventyay-interpreter-chat:'
const MAX_WS_RETRIES = 6

function safeJsonParse(rawValue, fallbackValue) {
  if (!rawValue) return fallbackValue
  try {
    return JSON.parse(rawValue)
  } catch (error) {
    console.error('Failed to parse persisted booth payload', error)
    return fallbackValue
  }
}

export class BoothRealtimeClient {
  constructor(sessionKey, options = {}) {
    this.sessionKey = sessionKey
    this.channelName = `eventyay-booth-${sessionKey}`
    this.websocketUrl = options.websocketUrl || ''
    this.authToken = options.authToken || ''
    this.channel = null
    this.socket = null
    this.socketReconnectTimer = null
    this.socketReconnectAttempts = 0
    this.closedManually = false
    this.onMessage = null
  }

  connect(onMessage) {
    this.closedManually = false
    this.onMessage = onMessage
    this.connectBroadcastChannel()
    this.connectWebSocket()
  }

  connectBroadcastChannel() {
    if (!window.BroadcastChannel) return
    if (this.channel) {
      this.channel.close()
    }
    this.channel = new BroadcastChannel(this.channelName)
    this.channel.addEventListener('message', (event) => {
      this.handleEnvelope(event.data)
    })
  }

  connectWebSocket() {
    if (!this.websocketUrl) return
    const wsUrl = this.buildWebSocketUrl()
    if (!wsUrl) return
    if (this.socket) {
      this.socket.close()
      this.socket = null
    }
    this.socket = new WebSocket(wsUrl)
    this.socket.addEventListener('open', () => {
      this.socketReconnectAttempts = 0
    })
    this.socket.addEventListener('message', (event) => {
      const envelope = typeof event.data === 'string'
        ? safeJsonParse(event.data, null)
        : event.data
      this.handleEnvelope(envelope)
    })
    this.socket.addEventListener('close', () => {
      this.scheduleSocketReconnect()
    })
    this.socket.addEventListener('error', (error) => {
      console.error('Booth websocket transport error', error)
    })
  }

  send(eventType, payload) {
    const envelope = {
      eventType,
      payload,
      sentAt: Date.now()
    }
    this.channel?.postMessage(envelope)
    this.sendWebSocket(envelope)
    return envelope
  }

  readPersistedChat() {
    const storageKey = EVENT_CHAT_STORAGE_PREFIX + this.sessionKey
    return safeJsonParse(localStorage.getItem(storageKey), [])
  }

  persistChat(messages) {
    const storageKey = EVENT_CHAT_STORAGE_PREFIX + this.sessionKey
    localStorage.setItem(storageKey, JSON.stringify(messages))
  }

  close() {
    this.closedManually = true
    window.clearTimeout(this.socketReconnectTimer)
    this.socketReconnectTimer = null
    if (this.channel) {
      this.channel.close()
      this.channel = null
    }
    if (this.socket) {
      this.socket.close()
      this.socket = null
    }
  }

  sendWebSocket(envelope) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return
    this.socket.send(JSON.stringify(envelope))
  }

  buildWebSocketUrl() {
    if (!this.websocketUrl) return ''
    const url = new URL(this.websocketUrl)
    url.searchParams.set('session', this.sessionKey)
    if (this.authToken) {
      url.searchParams.set('token', this.authToken)
    }
    return url.toString()
  }

  handleEnvelope(envelope) {
    if (!envelope || !envelope.eventType) return
    this.onMessage?.(envelope)
  }

  scheduleSocketReconnect() {
    if (this.closedManually || !this.websocketUrl) return
    if (this.socketReconnectAttempts >= MAX_WS_RETRIES) {
      console.error('Booth websocket retries exhausted; continuing with local transport only.')
      return
    }
    const delay = Math.min(1000 * 2 ** this.socketReconnectAttempts, 8000)
    this.socketReconnectAttempts += 1
    window.clearTimeout(this.socketReconnectTimer)
    this.socketReconnectTimer = window.setTimeout(() => {
      this.connectWebSocket()
    }, delay)
  }
}

export const boothRealtimeEvents = {
  EVENT_CHAT,
  EVENT_ACTIVE_INTERPRETER,
  EVENT_PARTICIPANT_STATE
}
