function normalizeAnswerPayload(payload) {
  if (payload?.sdp && payload?.type) return payload
  if (payload?.jsep?.sdp && payload?.jsep?.type) return payload.jsep
  throw new Error('Ingest server returned invalid SDP answer payload.')
}

export class IngestClient {
  constructor({ baseUrl, authToken }) {
    this.baseUrl = (baseUrl || '').replace(/\/+$/, '')
    this.authToken = authToken || ''
  }

  ingestEndpoint(channelId) {
    if (!this.baseUrl) {
      throw new Error('VITE_INGEST_BASE_URL is not configured.')
    }
    return `${this.baseUrl}/api/interpreter/connect/${encodeURIComponent(channelId)}`
  }

  async checkReachable(channelId) {
    const endpoint = this.ingestEndpoint(channelId)
    const response = await fetch(endpoint, {
      method: 'OPTIONS',
      headers: this.headers()
    })
    return response.ok || [401, 403, 405].includes(response.status)
  }

  async negotiate(channelId, localDescription) {
    const endpoint = this.ingestEndpoint(channelId)
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: this.headers(),
      body: JSON.stringify({
        type: localDescription.type,
        sdp: localDescription.sdp
      })
    })
    if (!response.ok) {
      const reason = await response.text()
      throw new Error(`Ingest negotiation failed (${response.status}): ${reason}`)
    }
    const payload = await response.json()
    return normalizeAnswerPayload(payload)
  }

  headers() {
    const headers = {
      'Content-Type': 'application/json'
    }
    if (this.authToken) {
      headers.Authorization = `Bearer ${this.authToken}`
    }
    return headers
  }
}
