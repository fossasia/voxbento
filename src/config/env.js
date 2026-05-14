const parseStunServers = (rawValue) => {
  if (!rawValue) return []
  return rawValue
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
}

export const env = {
  ingestBaseUrl: import.meta.env.VITE_INGEST_BASE_URL || '',
  ingestAuthToken: import.meta.env.VITE_INGEST_AUTH_TOKEN || '',
  defaultEventSlug: import.meta.env.VITE_DEFAULT_EVENT_SLUG || 'sample-event',
  defaultBoothId: import.meta.env.VITE_DEFAULT_BOOTH_ID || 'booth-a',
  defaultLanguage: import.meta.env.VITE_DEFAULT_LANGUAGE_LABEL || 'English',
  defaultChannelId: import.meta.env.VITE_DEFAULT_CHANNEL_ID || 'en-main',
  boothWsUrl: import.meta.env.VITE_BOOTH_WS_URL || '',
  jitsiDomain: import.meta.env.VITE_JITSI_DOMAIN || '',
  defaultJitsiUrl: import.meta.env.VITE_JITSI_DEFAULT_URL || '',
  stunServers: parseStunServers(import.meta.env.VITE_STUN_SERVERS)
}
