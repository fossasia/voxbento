const elements = {
  micDeviceSelect: document.getElementById('mic-device-select'),
  showVirtualDevices: document.getElementById('show-virtual-devices'),
  micMeter: document.getElementById('mic-meter'),
  meterRow: document.getElementById('meter-row'),
  micTestBtn: document.getElementById('mic-test-btn'),
  loopbackTestBtn: document.getElementById('loopback-test-btn'),
  loopbackProgressRow: document.getElementById('loopback-progress-row'),
  loopbackProgress: document.getElementById('loopback-progress'),
  loopbackStatus: document.getElementById('loopback-status'),
  checkMicPermission: document.getElementById('check-mic-permission'),
  checkNetwork: document.getElementById('check-network'),
  preflightRetry: document.getElementById('preflight-retry'),
}

const state = {
  micDeviceId: localStorage.getItem('mic-device-id') || '',
}

let micTestStream = null
let micAnimFrame = null
let micAudioCtx = null
let micAnalyser = null
let loopbackRecorder = null
let loopbackAudio = null

document.addEventListener('DOMContentLoaded', () => {
  boot()
})

async function boot() {
  await populateMicDevices()
  bindEventHandlers()
  await runPreflightChecks()
}

function bindEventHandlers() {
  if (elements.micDeviceSelect) {
    elements.micDeviceSelect.addEventListener('change', () => {
      state.micDeviceId = elements.micDeviceSelect.value
      localStorage.setItem('mic-device-id', state.micDeviceId)
      if (micTestStream) {
        stopMicTest().then(startMicTest).catch(() => {})
      }
    })
  }

  if (elements.showVirtualDevices) {
    elements.showVirtualDevices.addEventListener('change', () => {
      populateMicDevices()
    })
  }

  if (elements.micTestBtn) {
    elements.micTestBtn.addEventListener('click', async () => {
      if (micTestStream) {
        stopMicTest()
      } else {
        await startMicTest()
      }
    })
  }

  if (elements.loopbackTestBtn) {
    elements.loopbackTestBtn.addEventListener('click', async () => {
      if (loopbackRecorder || loopbackAudio) {
        stopLoopbackTest()
      } else {
        await startLoopbackTest()
      }
    })
  }

  if (elements.preflightRetry) {
    elements.preflightRetry.addEventListener('click', () => {
      runPreflightChecks()
    })
  }
}

async function populateMicDevices() {
  if (!navigator.mediaDevices) return
  try {
    const tempStream = await navigator.mediaDevices.getUserMedia({ audio: true })
    tempStream.getTracks().forEach((t) => t.stop())
  } catch {
    // Permission denied or no device
  }
  let devices = []
  try {
    devices = await navigator.mediaDevices.enumerateDevices()
  } catch {
    return
  }

  const virtualKeywords = ['zoom', 'teams', 'nomachine', 'blackhole', 'loopback', 'soundflower', 'obs', 'virtual', 'webex']
  const showVirtual = elements.showVirtualDevices && elements.showVirtualDevices.checked

  let audioInputs = devices.filter((d) => d.kind === 'audioinput')
  
  if (!showVirtual) {
    audioInputs = audioInputs.filter((d) => {
      const lower = d.label.toLowerCase()
      return !virtualKeywords.some((keyword) => lower.includes(keyword))
    })
  }

  if (!elements.micDeviceSelect) return
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

async function runPreflightChecks() {
  setPreflightStatus(elements.checkMicPermission, 'pending', 'Checking…')
  setPreflightStatus(elements.checkNetwork, 'pending', 'Checking…')

  // Check Mic
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    stream.getTracks().forEach((t) => t.stop())
    setPreflightStatus(elements.checkMicPermission, 'pass', 'Permission granted')
    await populateMicDevices() // Refresh list since we now have permission
  } catch (error) {
    const msg = error.name === 'NotAllowedError'
        ? 'Denied — allow microphone access in browser settings'
        : `Error: ${error.message}`
    setPreflightStatus(elements.checkMicPermission, 'fail', msg)
  }

  // Check Network (MediaMTX reachable?)
  // We can just check the generic healthz endpoint or a generic channel
  try {
    const resp = await fetch('/healthz')
    if (resp.ok) {
      const data = await resp.json()
      if (data.mediamtx_ok) {
        setPreflightStatus(elements.checkNetwork, 'pass', 'Media server reachable')
      } else {
        setPreflightStatus(elements.checkNetwork, 'fail', 'Media server unreachable')
      }
    } else {
      setPreflightStatus(elements.checkNetwork, 'fail', 'Server error')
    }
  } catch (err) {
    setPreflightStatus(elements.checkNetwork, 'fail', 'Network unreachable')
  }
}

function setPreflightStatus(element, status, message = '') {
  if (!element) return
  const iconMap = { pass: '✅', fail: '❌', warn: '⚠️', pending: '⏳' }
  element.className = `preflight-item preflight--${status}`
  const iconEl = element.querySelector('.preflight-icon')
  if (iconEl) iconEl.textContent = iconMap[status] ?? '⏳'
  const msgEl = element.querySelector('.preflight-msg')
  if (msgEl) msgEl.textContent = message
}


// ── Mic Testing & Level Meter ────────────────────────────────────────────────

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
    if (elements.micTestBtn) {
      elements.micTestBtn.textContent = '⏹ Stop'
      elements.micTestBtn.classList.add('btn-primary')
    }
  } catch (error) {
    alert(`Cannot access microphone: ${error.message}`)
  }
}

function stopMicTest() {
  if (!micTestStream) return
  micTestStream.getTracks().forEach((t) => t.stop())
  micTestStream = null
  if (elements.micTestBtn) {
    elements.micTestBtn.textContent = '⚙ Test'
    elements.micTestBtn.classList.remove('btn-primary')
  }
  stopMicMeter()
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

  if (elements.meterRow) elements.meterRow.classList.remove('hidden')
  const canvas = elements.micMeter
  if (!canvas) return
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
    
    if (volume > 0.95) {
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
  if (elements.meterRow) elements.meterRow.classList.add('hidden')
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
    
    if (elements.loopbackTestBtn) {
      elements.loopbackTestBtn.textContent = '⏹ Stop Test'
      elements.loopbackTestBtn.classList.add('btn-primary')
    }
    if (elements.loopbackProgressRow) elements.loopbackProgressRow.classList.remove('hidden')
    if (elements.loopbackStatus) elements.loopbackStatus.textContent = 'Recording...'
    if (elements.loopbackProgress) elements.loopbackProgress.value = 0
    
    const chunks = []
    loopbackRecorder = new MediaRecorder(stream)
    loopbackRecorder.ondataavailable = (e) => chunks.push(e.data)
    
    const startTime = Date.now()
    const durationMs = 5000
    
    const progressInterval = setInterval(() => {
      const elapsed = Date.now() - startTime
      const pct = Math.min(100, (elapsed / durationMs) * 100)
      if (elements.loopbackProgress) elements.loopbackProgress.value = pct
    }, 50)
    
    loopbackRecorder.onstop = () => {
      clearInterval(progressInterval)
      stream.getTracks().forEach(t => t.stop())
      if (elements.loopbackTestBtn && !elements.loopbackTestBtn.classList.contains('btn-primary')) return // Was stopped manually
      
      const blob = new Blob(chunks, { type: 'audio/webm' })
      const url = URL.createObjectURL(blob)
      loopbackAudio = new Audio(url)
      
      if (elements.loopbackStatus) elements.loopbackStatus.textContent = 'Playing...'
      if (elements.loopbackProgress) elements.loopbackProgress.value = 0
      
      loopbackAudio.onended = () => {
        stopLoopbackTest()
      }
      
      loopbackAudio.ontimeupdate = () => {
        if (!loopbackAudio) return
        const pct = (loopbackAudio.currentTime / loopbackAudio.duration) * 100
        if (elements.loopbackProgress) elements.loopbackProgress.value = pct
      }
      
      loopbackAudio.play().catch(e => {
        alert(`Failed to play loopback audio: ${e.message}`)
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
    alert(`Cannot access microphone: ${error.message}`)
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
  
  if (elements.loopbackTestBtn) {
    elements.loopbackTestBtn.textContent = '🎙️ Record & Play'
    elements.loopbackTestBtn.classList.remove('btn-primary')
  }
  if (elements.loopbackProgressRow) elements.loopbackProgressRow.classList.add('hidden')
}
