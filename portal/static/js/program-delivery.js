// Use server-relative time so a listener's wall-clock skew cannot change delay.
export function presentationDelay(metadata) {
  if (!Number.isFinite(metadata.source_received_at_ms) || !Number.isFinite(metadata.server_sent_at_ms)) return 0;
  const offset = Math.min(120000, Math.max(0, Number(metadata.sync_offset_ms) || 0));
  return Math.min(120000, Math.max(0, metadata.source_received_at_ms + offset - metadata.server_sent_at_ms));
}

export function createDeliveryQueue() {
  const pending = new Set();
  return {
    schedule(metadata, deliver) {
      const delay = presentationDelay(metadata);
      if (!delay) { deliver(); return; }
      if (pending.size >= 512) {
        const oldest = pending.values().next().value;
        clearTimeout(oldest);
        pending.delete(oldest);
        console.warn('Program presentation buffer full; dropped oldest pending update');
      }
      const timer = setTimeout(() => { pending.delete(timer); deliver(); }, delay);
      pending.add(timer);
    },
    reset() {
      pending.forEach(clearTimeout);
      pending.clear();
    },
  };
}
