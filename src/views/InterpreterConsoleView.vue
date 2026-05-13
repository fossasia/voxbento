<template>
  <div class="console-view">
    <header class="console-topbar">
      <div class="brand">
        <span class="dot" />
        <strong>Eventyay Interpreter Console</strong>
      </div>
      <div class="session-meta">
        <span>{{ state.session.eventSlug }}</span>
        <span>·</span>
        <span>{{ state.session.boothId }}</span>
        <span>·</span>
        <span>{{ state.session.language }}</span>
      </div>
    </header>

    <main class="console-layout">
      <section class="monitor-column">
        <JitsiMonitorPanel
          :jitsi="state.jitsi"
          @join="joinJitsi"
          @loaded="onJitsiLoaded"
        />
      </section>

      <aside class="sidebar-column">
        <MicIngestPanel
          :state="state"
          @set-device="setAudioDevice"
          @mic-test="runMicTest"
          @start="startInterpretation"
          @stop="stopInterpretation"
        />
        <PreflightChecklist
          :state="state"
          @toggle="toggleChecklistItem"
        />
        <BoothHealthPanel :health="health" />
      </aside>
    </main>

    <ParticipantGrid
      :participants="state.participants"
      :active-participant-id="state.activeParticipantId"
      :local-participant-id="state.localParticipantId"
      :local-role="state.localRole"
      @set-live="setActiveInterpreter"
    />

    <BoothChatPanel
      :messages="state.chatMessages"
      :local-participant-id="state.localParticipantId"
      :ingest-status="state.ingest.status"
      @send="sendBoothChatMessage"
    />
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import JitsiMonitorPanel from '../components/JitsiMonitorPanel.vue'
import MicIngestPanel from '../components/MicIngestPanel.vue'
import PreflightChecklist from '../components/PreflightChecklist.vue'
import ParticipantGrid from '../components/ParticipantGrid.vue'
import BoothChatPanel from '../components/BoothChatPanel.vue'
import BoothHealthPanel from '../components/BoothHealthPanel.vue'
import { useInterpreterBooth } from '../composables/useInterpreterBooth'

const route = useRoute()
const {
  state,
  health,
  initialize,
  teardown,
  joinJitsi,
  onJitsiLoaded,
  setAudioDevice,
  runMicTest,
  startInterpretation,
  stopInterpretation,
  setActiveInterpreter,
  toggleChecklistItem,
  sendBoothChatMessage
} = useInterpreterBooth()

onMounted(async () => {
  await initialize({
    eventSlug: route.params.eventSlug,
    boothId: route.params.boothId
  })
})

onBeforeUnmount(() => {
  teardown()
})
</script>

<style scoped>
.console-view {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr auto auto;
  gap: 0.75rem;
  padding: 0.75rem;
}

.console-topbar {
  min-height: 48px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.75rem;
  border-radius: var(--size-border-radius);
  background: var(--clr-sidebar);
  color: var(--clr-sidebar-text-primary);
  padding: 0.5rem 0.75rem;
}

.brand {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  background: #f8f9fa;
  opacity: 0.9;
}

.session-meta {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  color: var(--clr-sidebar-text-secondary);
  font-size: 0.88rem;
  white-space: nowrap;
}

.console-layout {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(300px, 1fr);
  gap: 0.75rem;
  min-height: 0;
}

.sidebar-column {
  display: grid;
  align-content: start;
  gap: 0.75rem;
}

@media (max-width: 1080px) {
  .console-layout {
    grid-template-columns: 1fr;
  }

  .session-meta {
    display: none;
  }
}
</style>
