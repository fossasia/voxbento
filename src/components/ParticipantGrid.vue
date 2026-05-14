<template>
  <section class="ey-card participants-card">
    <header class="ey-card-header">
      <h2>Participant Grid</h2>
      <p class="ey-muted">
        Only one interpreter can be LIVE per language channel.
      </p>
    </header>
    <div class="ey-card-body participant-grid">
      <article
        v-for="participant in participants"
        :key="participant.id"
        class="participant-tile"
        :class="{ live: participant.id === activeParticipantId }"
      >
        <div class="tile-top">
          <strong>{{ participant.displayName }}</strong>
          <StatusPill
            :label="participant.id === activeParticipantId ? 'LIVE' : participant.role"
            :tone="participant.id === activeParticipantId ? 'danger' : 'neutral'"
          />
        </div>
        <div class="tile-mid">
          <div>{{ participant.language }} · {{ participant.channelId }}</div>
          <div class="ey-muted">
            {{ participant.connectionState }}
          </div>
        </div>
        <div class="tile-bottom">
          <StatusPill
            :label="`Mic: ${participant.micState}`"
            :tone="participant.micState === 'live' ? 'success' : 'neutral'"
          />
          <StatusPill
            :label="participant.speaking ? 'Speaking' : 'Idle'"
            :tone="participant.speaking ? 'info' : 'neutral'"
          />
          <StatusPill
            :label="participant.ingestState"
            :tone="participant.ingestState === 'connected' ? 'success' : 'neutral'"
          />
        </div>
        <button
          v-if="canSetLive(participant)"
          class="ey-btn ey-btn-outline"
          type="button"
          @click="$emit('set-live', participant.id)"
        >
          Set live
        </button>
      </article>
    </div>
  </section>
</template>

<script setup>
import StatusPill from './common/StatusPill.vue'

const props = defineProps({
  participants: {
    type: Array,
    required: true
  },
  activeParticipantId: {
    type: String,
    required: true
  },
  localParticipantId: {
    type: String,
    required: true
  },
  localRole: {
    type: String,
    required: true
  }
})

defineEmits(['set-live'])

function canSetLive(participant) {
  const isInterpreterRole = participant.role.includes('Interpreter')
  if (!isInterpreterRole) return false
  return props.localRole === 'Coordinator' || participant.id === props.localParticipantId
}
</script>

<style scoped>
.participants-card h2,
.participants-card p {
  margin: 0;
}

.participant-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 0.75rem;
}

.participant-tile {
  border: 1px solid var(--color-card-border);
  border-radius: var(--size-border-radius);
  padding: 0.75rem;
  display: grid;
  gap: 0.6rem;
}

.participant-tile.live {
  border-color: rgb(178 62 101 / 45%);
  box-shadow: 0 0 0 1px rgb(178 62 101 / 25%);
}

.tile-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.tile-mid {
  display: grid;
  gap: 0.15rem;
  font-size: 0.88rem;
}

.tile-bottom {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
</style>
