<template>
  <section class="ey-card monitor-panel">
    <header class="ey-card-header">
      <h2>Jitsi Monitor Panel</h2>
      <StatusPill
        :label="connectionLabel"
        :tone="connectionTone"
      />
    </header>
    <div class="ey-card-body monitor-panel-body">
      <div class="meeting-input-row">
        <input
          v-model="meetingUrl"
          class="ey-input"
          type="url"
          placeholder="Paste Jitsi meeting URL"
          autocomplete="off"
        >
        <button
          class="ey-btn ey-btn-primary"
          type="button"
          @click="onJoinMeeting"
        >
          Join
        </button>
      </div>
      <p
        v-if="jitsi.error"
        class="error-text"
      >
        {{ jitsi.error }}
      </p>
      <dl class="ey-kv monitor-meta">
        <div>
          <dt>Current room</dt>
          <dd>{{ jitsi.roomName || 'Not joined yet' }}</dd>
        </div>
        <div>
          <dt>Join mode</dt>
          <dd>Receive-only (mic/camera muted by default)</dd>
        </div>
      </dl>
      <div class="monitor-safety-note">
        <strong>Headphones-first:</strong> monitor feed is receive-focused to prevent booth echo and bleed.
      </div>
      <div class="iframe-shell">
        <iframe
          v-if="jitsi.embedUrl"
          :src="jitsi.embedUrl"
          title="Jitsi monitoring feed"
          allow="autoplay; fullscreen"
          @load="$emit('loaded')"
        />
        <div
          v-else
          class="iframe-placeholder"
        >
          Join a Jitsi room to start monitoring stage audio/video.
        </div>
      </div>
    </div>
  </section>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import StatusPill from './common/StatusPill.vue'

const props = defineProps({
  jitsi: {
    type: Object,
    required: true
  }
})

const emit = defineEmits(['join', 'loaded'])
const meetingUrl = ref(props.jitsi.inputUrl)

watch(
  () => props.jitsi.inputUrl,
  (value) => {
    meetingUrl.value = value
  }
)

const connectionLabel = computed(() => {
  if (props.jitsi.status === 'connected') return 'Connected'
  if (props.jitsi.status === 'connecting') return 'Connecting'
  if (props.jitsi.status === 'failed') return 'Issue'
  return 'Idle'
})

const connectionTone = computed(() => {
  if (props.jitsi.status === 'connected') return 'success'
  if (props.jitsi.status === 'connecting') return 'info'
  if (props.jitsi.status === 'failed') return 'danger'
  return 'neutral'
})

function onJoinMeeting() {
  emit('join', meetingUrl.value)
}
</script>

<style scoped>
.monitor-panel {
  display: flex;
  flex-direction: column;
  min-height: 500px;
}

.monitor-panel h2 {
  margin: 0;
  font-size: 1rem;
}

.monitor-panel-body {
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
  min-height: 0;
  flex: 1;
}

.meeting-input-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.5rem;
}

.monitor-meta dd {
  margin: 0;
}

.monitor-safety-note {
  border-radius: var(--size-border-radius);
  border: 1px solid rgb(209 133 56 / 30%);
  background: rgb(209 133 56 / 10%);
  color: var(--color-warning);
  padding: 0.65rem 0.75rem;
  font-size: 0.9rem;
}

.iframe-shell {
  border: 1px solid var(--color-card-border);
  border-radius: var(--size-border-radius);
  background: #000;
  min-height: 360px;
  overflow: hidden;
  flex: 1;
}

.iframe-shell iframe,
.iframe-placeholder {
  width: 100%;
  height: 100%;
  min-height: 360px;
}

.iframe-placeholder {
  display: grid;
  place-items: center;
  color: #e9ecef;
  padding: 1rem;
  text-align: center;
}

.error-text {
  margin: 0;
  color: var(--color-danger);
  font-size: 0.9rem;
}
</style>
