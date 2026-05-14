function createIceServers(stunServers) {
  if (!Array.isArray(stunServers) || stunServers.length === 0) return []
  return stunServers.map((server) => ({ urls: server }))
}

function waitForIceGathering(peerConnection) {
  if (peerConnection.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve) => {
    const timeout = window.setTimeout(() => {
      peerConnection.removeEventListener('icegatheringstatechange', onStateChange)
      resolve()
    }, 3000)
    const onStateChange = () => {
      if (peerConnection.iceGatheringState !== 'complete') return
      window.clearTimeout(timeout)
      peerConnection.removeEventListener('icegatheringstatechange', onStateChange)
      resolve()
    }
    peerConnection.addEventListener('icegatheringstatechange', onStateChange)
  })
}

export class MicStreamingManager {
  constructor({ stunServers }) {
    this.stunServers = stunServers || []
    this.stream = null
    this.peerConnection = null
    this.audioContext = null
    this.levelFrame = null
    this.statsTimer = null
    this.lastOutboundSample = null
  }

  async listInputDevices() {
    const devices = await navigator.mediaDevices.enumerateDevices()
    return devices.filter((device) => device.kind === 'audioinput')
  }

  async startMicrophone(deviceId, onLevel) {
    this.stopMeter()
    this.stopTracks()
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true
      }
    })
    this.startMeter(onLevel)
    return this.stream
  }

  startMeter(onLevel) {
    if (!this.stream) return
    this.audioContext = new AudioContext()
    const analyser = this.audioContext.createAnalyser()
    analyser.fftSize = 256
    const source = this.audioContext.createMediaStreamSource(this.stream)
    source.connect(analyser)
    // Intentionally never connect analyser/source to destination to prevent loopback.
    const data = new Uint8Array(analyser.fftSize)
    const tick = () => {
      analyser.getByteTimeDomainData(data)
      let sum = 0
      for (const sample of data) {
        const normalized = (sample - 128) / 128
        sum += normalized * normalized
      }
      const level = Math.min(1, Math.sqrt(sum / data.length) * 2.6)
      onLevel(level)
      this.levelFrame = requestAnimationFrame(tick)
    }
    tick()
  }

  async createIngestConnection(onConnectionStateChange) {
    if (!this.stream) {
      throw new Error('Microphone stream has not been started.')
    }
    this.stopPeerConnection()
    this.peerConnection = new RTCPeerConnection({
      iceServers: createIceServers(this.stunServers)
    })
    for (const track of this.stream.getAudioTracks()) {
      this.peerConnection.addTrack(track, this.stream)
    }
    this.peerConnection.addEventListener('connectionstatechange', () => {
      onConnectionStateChange(this.peerConnection.connectionState)
    })
    this.peerConnection.addEventListener('iceconnectionstatechange', () => {
      onConnectionStateChange(this.peerConnection.iceConnectionState)
    })
    const offer = await this.peerConnection.createOffer({
      offerToReceiveAudio: false,
      offerToReceiveVideo: false
    })
    await this.peerConnection.setLocalDescription(offer)
    await waitForIceGathering(this.peerConnection)
    return this.peerConnection.localDescription
  }

  async applyRemoteAnswer(answer) {
    if (!this.peerConnection) {
      throw new Error('Ingest connection is not initialized.')
    }
    await this.peerConnection.setRemoteDescription({
      type: answer.type,
      sdp: answer.sdp
    })
  }

  startStats(onBitrate) {
    if (!this.peerConnection) return
    window.clearInterval(this.statsTimer)
    this.lastOutboundSample = null
    this.statsTimer = window.setInterval(async () => {
      if (!this.peerConnection) return
      const reports = await this.peerConnection.getStats()
      reports.forEach((report) => {
        if (report.type !== 'outbound-rtp' || report.kind !== 'audio') return
        if (!this.lastOutboundSample) {
          this.lastOutboundSample = {
            bytesSent: report.bytesSent,
            timestamp: report.timestamp
          }
          onBitrate(0)
          return
        }
        const bytesDiff = report.bytesSent - this.lastOutboundSample.bytesSent
        const timeDiffSeconds = (report.timestamp - this.lastOutboundSample.timestamp) / 1000
        this.lastOutboundSample = {
          bytesSent: report.bytesSent,
          timestamp: report.timestamp
        }
        if (timeDiffSeconds <= 0 || bytesDiff <= 0) {
          onBitrate(0)
          return
        }
        const kbps = Math.round((bytesDiff * 8) / 1000 / timeDiffSeconds)
        onBitrate(Number.isFinite(kbps) ? kbps : 0)
      })
    }, 3000)
  }

  stopIngest() {
    window.clearInterval(this.statsTimer)
    this.statsTimer = null
    this.lastOutboundSample = null
    this.stopPeerConnection()
  }

  stopAll() {
    this.stopIngest()
    this.stopMeter()
    this.stopTracks()
  }

  stopPeerConnection() {
    if (!this.peerConnection) return
    this.peerConnection.close()
    this.peerConnection = null
  }

  stopTracks() {
    if (!this.stream) return
    this.stream.getTracks().forEach((track) => track.stop())
    this.stream = null
  }

  stopMeter() {
    if (this.levelFrame) {
      cancelAnimationFrame(this.levelFrame)
      this.levelFrame = null
    }
    if (this.audioContext) {
      this.audioContext.close()
      this.audioContext = null
    }
  }
}
