<template>
  <section class="ey-card">
    <header class="ey-card-header">
      <h2>Mic Capture &amp; Ingest</h2>
      <StatusPill
        :label="ingestLabel"
        :tone="ingestTone"
      />
    </header>
    <div class="ey-card-body panel-body">
      <div class="field">
        <label for="mic-device">Input device</label>
        <select
          id="mic-device"
          class="ey-select"
          :value="state.mic.selectedDeviceId"
          @change="$emit('set-device', $event.target.value)"
        >
          <option
            v-for="device in state.mic.devices"
            :key="device.deviceId"
            :value="device.deviceId"
          >
            {{ device.label || 'Microphone device' }}
          </option>
        </select>
      </div>

      <div class="field">
        <div class="meter-row">
          <span>Mic level</span>
          <span class="ey-muted">{{ Math.round(state.mic.level * 100) }}%</span>
        </div>
        <div
          class="meter-track"
          role="presentation"
        >
          <div
            class="meter-fill"
            :style="{ width: `${Math.max(4, Math.round(state.mic.level * 100))}%` }"
          />
        </div>
      </div>

      <dl class="ey-kv">
        <div>
          <dt>Language</dt>
          <dd>{{ state.session.language }}</dd>
        </div>
        <div>
          <dt>Channel</dt>
          <dd>{{ state.session.channelId }}</dd>
        </div>
      </dl>

      <div class="status-row">
        <StatusPill
          :label="state.ingest.status"
          :tone="ingestTone"
        />
        <StatusPill
          :label="state.ingest.streamingLive ? 'Streaming live' : 'Standby'"
          :tone="state.ingest.streamingLive ? 'success' : 'neutral'"
        />
        <StatusPill
          :label="state.ingest.reconnecting ? 'Reconnecting' : 'Stable'"
          :tone="state.ingest.reconnecting ? 'warning' : 'info'"
        />
      </div>

      <div class="actions-row">
        <button
          class="ey-btn ey-btn-outline"
          type="button"
          @click="$emit('mic-test')"
        >
          Run mic test
        </button>
        <button
          v-if="!state.ingest.streamingLive"
          class="ey-btn ey-btn-primary"
          type="button"
          @click="$emit('start')"
        >
          Start interpretation
        </button>
        <button
          v-else
          class="ey-btn ey-btn-outline danger"
          type="button"
          @click="$emit('stop')"
        >
          Stop interpretation
        </button>
      </div>
      <p
        v-if="state.ingest.error"
        class="error-text"
      >
        {{ state.ingest.error }}
      </p>
    </div>
  </section>
</template>

<script setup>
import { computed } from 'vue'
import StatusPill from './common/StatusPill.vue'

const props = defineProps({
  state: {
    type: Object,
    required: true
  }
})

defineEmits(['set-device', 'mic-test', 'start', 'stop'])

const ingestLabel = computed(() => {
  if (props.state.ingest.status === 'connected') return 'Ingest connected'
  if (props.state.ingest.status === 'connecting') return 'Connecting ingest'
  if (props.state.ingest.status === 'reconnecting') return 'Reconnecting'
  if (props.state.ingest.status === 'failed') return 'Ingest failed'
  return 'Ingest idle'
})

const ingestTone = computed(() => {
  if (props.state.ingest.status === 'connected') return 'success'
  if (props.state.ingest.status === 'failed') return 'danger'
  if (props.state.ingest.status === 'reconnecting') return 'warning'
  if (props.state.ingest.status === 'connecting') return 'info'
  return 'neutral'
})
</script>

<style scoped>
h2 {
  margin: 0;
  font-size: 1rem;
}

.panel-body {
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.field {
  display: grid;
  gap: 0.4rem;
}

.field label {
  color: var(--color-text-muted);
  font-size: 0.88rem;
}

.meter-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.9rem;
}

.meter-track {
  height: 8px;
  border-radius: 999px;
  background: var(--color-grey-lighter);
  overflow: hidden;
}

.meter-fill {
  height: 100%;
  background: linear-gradient(90deg, #2bba6a, #f2b63d, #d63f5f);
  transition: width 0.08s linear;
}

.status-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.actions-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.danger {
  border-color: rgb(178 62 101 / 40%);
  color: var(--color-danger);
}

.error-text {
  margin: 0;
  font-size: 0.88rem;
  color: var(--color-danger);
}
</style>
