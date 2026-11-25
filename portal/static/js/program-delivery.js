// Use server-relative time so a listener's wall-clock skew cannot change delay.
export function presentationDelay(metadata, currentAudioDelayMs = 0) {
  if (!metadata || !Number.isFinite(metadata.source_received_at_ms) || !Number.isFinite(metadata.server_sent_at_ms)) return 0;
  const syncOffset = Number(metadata.sync_offset_ms) || 0;
  const audioDelay = Number(currentAudioDelayMs) || 0;
  const effectiveOffsetMs = syncOffset > 0 ? Math.max(syncOffset, audioDelay) : audioDelay;
  const boundedOffset = Math.min(120000, Math.max(0, effectiveOffsetMs));
  const delay = metadata.source_received_at_ms + boundedOffset - metadata.server_sent_at_ms;
  return Math.min(120000, Math.max(0, delay));
}

export function createDeliveryQueue() {
  const pending = [];
  let timerId = null;

  function scheduleNext() {
    if (timerId !== null) {
      clearTimeout(timerId);
      timerId = null;
    }
    if (pending.length === 0) return;
    const now = Date.now();
    const next = pending[0];
    const delay = Math.max(0, next.deadline - now);
    timerId = setTimeout(() => {
      timerId = null;
      const item = pending.shift();
      if (item && item.deliver) {
        try {
          item.deliver();
        } catch (err) {
          console.error('Error delivering queued update:', err);
        }
      }
      scheduleNext();
    }, delay);
  }

  return {
    schedule(metadata, deliver, audioDelayMs = 0) {
      const delay = presentationDelay(metadata, audioDelayMs);
      if (pending.length === 0 && delay === 0) {
        deliver();
        return;
      }
      const now = Date.now();
      let deadline = now + delay;
      if (pending.length > 0) {
        const lastDeadline = pending[pending.length - 1].deadline;
        if (deadline < lastDeadline) {
          deadline = lastDeadline;
        }
      }
      if (pending.length >= 512) {
        pending.shift();
        console.warn('Program presentation buffer full; dropped oldest pending update');
      }
      pending.push({ deadline, deliver });
      if (pending.length === 1) {
        scheduleNext();
      }
    },
    reset() {
      if (timerId !== null) {
        clearTimeout(timerId);
        timerId = null;
      }
      pending.length = 0;
    },
  };
}
