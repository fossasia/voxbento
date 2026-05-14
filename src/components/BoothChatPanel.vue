<template>
  <section class="ey-card chat-panel">
    <header class="ey-card-header">
      <h2>Internal Booth Chat</h2>
      <StatusPill
        :label="connectionLabel"
        :tone="connectionTone"
      />
    </header>
    <div class="ey-card-body chat-body">
      <div class="message-list">
        <article
          v-for="message in messages"
          :key="message.id"
          class="message"
          :class="{ mine: message.senderId === localParticipantId }"
        >
          <header>
            <strong>{{ message.senderName }}</strong>
            <span>{{ formatTime(message.sentAt) }}</span>
          </header>
          <p>{{ message.body }}</p>
        </article>
        <p
          v-if="messages.length === 0"
          class="ey-muted"
        >
          No booth messages yet.
        </p>
      </div>
      <form
        class="chat-input-row"
        @submit.prevent="submitMessage"
      >
        <input
          v-model="draft"
          class="ey-input"
          type="text"
          placeholder="Type booth coordination message…"
        >
        <button
          class="ey-btn ey-btn-primary"
          type="submit"
        >
          Send
        </button>
      </form>
    </div>
  </section>
</template>

<script setup>
import { computed, ref } from 'vue'
import StatusPill from './common/StatusPill.vue'

const props = defineProps({
  messages: {
    type: Array,
    required: true
  },
  localParticipantId: {
    type: String,
    required: true
  },
  ingestStatus: {
    type: String,
    required: true
  }
})

const emit = defineEmits(['send'])
const draft = ref('')

const connectionLabel = computed(() => {
  if (props.ingestStatus === 'connected') return 'Live booth session'
  if (props.ingestStatus === 'reconnecting') return 'Session reconnecting'
  return 'Session active'
})

const connectionTone = computed(() => {
  if (props.ingestStatus === 'connected') return 'success'
  if (props.ingestStatus === 'reconnecting') return 'warning'
  return 'info'
})

function submitMessage() {
  if (!draft.value.trim()) return
  emit('send', draft.value)
  draft.value = ''
}

function formatTime(timestamp) {
  return new Date(timestamp).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit'
  })
}
</script>

<style scoped>
.chat-panel h2 {
  margin: 0;
  font-size: 1rem;
}

.chat-body {
  display: grid;
  gap: 0.75rem;
}

.message-list {
  border: 1px solid var(--color-card-border);
  border-radius: var(--size-border-radius);
  background: var(--color-grey-lightest);
  padding: 0.75rem;
  max-height: 260px;
  overflow-y: auto;
  display: grid;
  gap: 0.5rem;
}

.message {
  background: var(--color-bg);
  border: 1px solid var(--color-card-border);
  border-radius: var(--size-border-radius);
  padding: 0.6rem;
}

.message.mine {
  border-color: rgb(33 133 208 / 35%);
}

.message header {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.2rem;
  font-size: 0.8rem;
}

.message header span {
  color: var(--color-text-muted);
}

.message p {
  margin: 0;
  white-space: pre-wrap;
}

.chat-input-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 0.5rem;
}
</style>
