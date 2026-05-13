<template>
  <span
    class="status-pill"
    :class="`tone-${tone}`"
  >{{ label }}</span>
</template>

<script setup>
defineProps({
  label: {
    type: String,
    required: true
  },
  tone: {
    type: String,
    default: 'neutral'
  }
})
</script>

<style scoped>
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  border-radius: 999px;
  border: 1px solid transparent;
  padding: 0.15rem 0.55rem;
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.02em;
}

.tone-neutral {
  background: var(--color-grey-lightest);
  border-color: var(--color-grey-light);
  color: var(--color-text-muted);
}

.tone-success {
  background: rgb(38 132 74 / 12%);
  border-color: rgb(38 132 74 / 30%);
  color: var(--color-success);
}

.tone-warning {
  background: rgb(209 133 56 / 12%);
  border-color: rgb(209 133 56 / 30%);
  color: var(--color-warning);
}

.tone-danger {
  background: rgb(178 62 101 / 12%);
  border-color: rgb(178 62 101 / 30%);
  color: var(--color-danger);
}

.tone-info {
  background: rgb(33 133 208 / 12%);
  border-color: rgb(33 133 208 / 30%);
  color: var(--color-info);
}
</style>
