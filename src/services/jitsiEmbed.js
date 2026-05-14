const JITSI_EMBED_CONFIG = {
  'config.startWithAudioMuted': 'true',
  'config.startWithVideoMuted': 'true',
  'config.prejoinPageEnabled': 'false',
  'config.disableInitialGUM': 'true',
  'config.startSilent': 'true'
}

function ensureValidMeetingPath(url) {
  const roomPath = url.pathname.replace(/\/+$/, '')
  if (!roomPath || roomPath === '/') {
    throw new Error('Missing room path in Jitsi URL.')
  }
  return roomPath
}

function buildHashParams() {
  const hashParams = new URLSearchParams()
  for (const [key, value] of Object.entries(JITSI_EMBED_CONFIG)) {
    hashParams.set(key, value)
  }
  return hashParams.toString()
}

export function parseJitsiMeetingUrl(rawUrl) {
  const trimmed = rawUrl.trim()
  if (!trimmed) {
    throw new Error('Meeting URL is required.')
  }
  const url = new URL(trimmed)
  const roomPath = ensureValidMeetingPath(url)
  const roomName = roomPath.split('/').pop() || ''
  return {
    origin: url.origin,
    roomPath,
    roomName
  }
}

export function buildJitsiEmbedUrl(rawUrl, options = {}) {
  const parsed = parseJitsiMeetingUrl(rawUrl)
  const expectedDomain = options.expectedDomain?.trim()
  if (expectedDomain && parsed.origin !== `https://${expectedDomain}`) {
    throw new Error(`Jitsi meeting must use configured domain: ${expectedDomain}`)
  }
  const base = `${parsed.origin}${parsed.roomPath}`
  return {
    roomName: parsed.roomName,
    embedUrl: `${base}#${buildHashParams()}`
  }
}
