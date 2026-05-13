<template>
  <section class="ey-card">
    <header class="ey-card-header">
      <h2>Pre-flight Checklist</h2>
      <StatusPill
        :label="allChecksPassed ? 'Ready' : 'Pending'"
        :tone="allChecksPassed ? 'success' : 'warning'"
      />
    </header>
    <div class="ey-card-body">
      <label
        v-for="item in checklistItems"
        :key="item.key"
        class="check-item"
      >
        <input
          :checked="state.preflight[item.key]"
          type="checkbox"
          @change="$emit('toggle', item.key)"
        >
        <span>{{ item.label }}</span>
      </label>
      <p class="ey-muted helper-text">
        Use headphones, keep Jitsi receive-only, and never route ingest audio back to local playback.
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

defineEmits(['toggle'])

const checklistItems = [
  { key: 'headphonesConnected', label: 'Headphones connected' },
  { key: 'monitoringActive', label: 'Monitoring active' },
  { key: 'micTestComplete', label: 'Mic test complete' },
  { key: 'ingestReachable', label: 'Ingest reachable' }
]

const allChecksPassed = computed(() => {
  return checklistItems.every((item) => props.state.preflight[item.key])
})
</script>

<style scoped>
h2 {
  margin: 0;
  font-size: 1rem;
}

.check-item {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.5rem;
}

.check-item:last-of-type {
  margin-bottom: 0;
}

.helper-text {
  margin-top: 0.75rem;
  margin-bottom: 0;
  font-size: 0.85rem;
}
</style>
