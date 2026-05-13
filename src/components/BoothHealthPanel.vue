<template>
  <section class="ey-card">
    <header class="ey-card-header">
      <h2>Booth Health</h2>
      <StatusPill
        :label="health.ingestHealthy ? 'Healthy' : 'Attention'"
        :tone="health.ingestHealthy ? 'success' : 'warning'"
      />
    </header>
    <div class="ey-card-body">
      <dl class="ey-kv">
        <div>
          <dt>Ingest</dt>
          <dd>{{ health.ingestHealthy ? 'Healthy' : 'Not healthy' }}</dd>
        </div>
        <div>
          <dt>Live streaming</dt>
          <dd>{{ health.liveStreamingActive ? 'Active' : 'Standby' }}</dd>
        </div>
        <div>
          <dt>Active interpreter</dt>
          <dd>{{ health.activeInterpreter }}</dd>
        </div>
        <div>
          <dt>Packet health</dt>
          <dd>{{ health.packetHealth }}</dd>
        </div>
        <div>
          <dt>Bitrate</dt>
          <dd>{{ health.bitrateKbps }} kbps</dd>
        </div>
      </dl>
    </div>
  </section>
</template>

<script setup>
import StatusPill from './common/StatusPill.vue'

defineProps({
  health: {
    type: Object,
    required: true
  }
})
</script>

<style scoped>
h2 {
  margin: 0;
  font-size: 1rem;
}

.ey-kv dd {
  margin: 0;
  text-transform: capitalize;
}
</style>
