// Use server-relative time so a listener's wall-clock skew cannot change delay.
export function presentationDelay(metadata, roomAudioDelayMs = 0) {
  const syncOffset = Math.min(120000, Math.max(0, Number(metadata.sync_offset_ms) || 0));
  const extraDelay = Math.min(10000, Math.max(0, Number(roomAudioDelayMs) || 0));
  const effectiveOffset = syncOffset > 0 ? Math.max(syncOffset, extraDelay) : extraDelay;
  if (!Number.isFinite(metadata.source_received_at_ms) || !Number.isFinite(metadata.server_sent_at_ms)) {
    return effectiveOffset;
  }
  return Math.min(120000, Math.max(0, metadata.source_received_at_ms + effectiveOffset - metadata.server_sent_at_ms));
}

export function createDeliveryQueue() {
  const pending = new Set();
  let lastFireAt = 0;

  function dropOldestIfFull() {
    if (pending.size < 512) return;
    const oldest = pending.values().next().value;
    clearTimeout(oldest.timer);
    pending.delete(oldest);
    console.warn('Program presentation buffer full; dropped oldest pending update');
  }

  function scheduleEntry(entry, fireAt) {
    entry.fireAt = fireAt;
    const waitMs = Math.max(0, fireAt - Date.now());
    entry.timer = setTimeout(() => {
      pending.delete(entry);
      entry.deliver();
    }, waitMs);
  }

  return {
    schedule(metadata, deliver, roomAudioDelayMs = 0, key = null) {
      const delay = presentationDelay(metadata, roomAudioDelayMs);
      const now = Date.now();
      if (!delay && lastFireAt <= now) {
        deliver();
        return;
      }
      dropOldestIfFull();
      const fireAt = Math.max(lastFireAt + 1, now + delay);
      lastFireAt = fireAt;
      const entry = { timer: null, fireAt, key, metadata, deliver };
      pending.add(entry);
      scheduleEntry(entry, fireAt);
    },
    cancelByKey(key) {
      if (!key) return;
      for (const entry of [...pending]) {
        if (entry.key === key) {
          clearTimeout(entry.timer);
          pending.delete(entry);
        }
      }
    },
    updateDelay(roomAudioDelayMs = 0) {
      const now = Date.now();
      let prevFireAt = 0;
      for (const entry of pending) {
        clearTimeout(entry.timer);
        const delay = presentationDelay(entry.metadata, roomAudioDelayMs);
        const target = Math.max(prevFireAt, now + delay);
        prevFireAt = target;
        scheduleEntry(entry, target);
      }
      lastFireAt = prevFireAt;
    },
    reset() {
      for (const entry of pending) {
        clearTimeout(entry.timer);
      }
      pending.clear();
      lastFireAt = 0;
    },
  };
}
