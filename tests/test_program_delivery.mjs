import assert from 'node:assert/strict';
import { createDeliveryQueue, presentationDelay } from '../portal/static/js/program-delivery.js';

// 1. presentationDelay avoids double delay by subtracting pipeline latency
{
  const delay = presentationDelay(
    {
      source_received_at_ms: 10_000,
      server_sent_at_ms: 13_000,
      sync_offset_ms: 0,
    },
    5_000,
  );
  assert.equal(delay, 2_000, '3s pipeline latency is subtracted from 5s roomAudioDelayMs');
}

// 2. Effective offset uses max(sync_offset_ms, roomAudioDelayMs) when program_sync_offset_ms > 0
{
  const delay = presentationDelay(
    {
      source_received_at_ms: 10_000,
      server_sent_at_ms: 14_000,
      sync_offset_ms: 12_000,
    },
    10_000,
  );
  assert.equal(delay, 8_000, 'effectiveOffset=12000 minus 4000 latency = 8000ms (no over-delay)');
}

// 3. Fallback when timestamps are absent
{
  assert.equal(presentationDelay({}, 4_000), 4_000);
  assert.equal(presentationDelay({}, 0), 0);
}

// 4. createDeliveryQueue preserves monotonic order when roomAudioDelayMs shrinks, and supports cancelByKey
async function runQueueTests() {
  const queue = createDeliveryQueue();
  const order = [];

  queue.schedule({ source_received_at_ms: 1000, server_sent_at_ms: 1000 }, () => order.push('seg1'), 120, 'p1');
  queue.schedule({ source_received_at_ms: 1000, server_sent_at_ms: 1000 }, () => order.push('seg2'), 20, 'p2');
  queue.cancelByKey('p1');

  await new Promise((r) => setTimeout(r, 180));
  assert.deepEqual(order, ['seg2'], 'cancelByKey cancels pending partial and keeps remaining item');

  const order2 = [];
  const now = Date.now();
  queue.schedule({ source_received_at_ms: now, server_sent_at_ms: now }, () => order2.push('first'), 100);
  queue.schedule({ source_received_at_ms: now, server_sent_at_ms: now }, () => order2.push('second'), 10);
  await new Promise((r) => setTimeout(r, 160));
  assert.deepEqual(order2, ['first', 'second'], 'shrinking delay never lets second overtake first');
  queue.reset();
}

await runQueueTests();
console.log('test_program_delivery.mjs passed');
