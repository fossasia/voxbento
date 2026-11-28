export const TRANSLATION_STATES = {
  idle: ['Idle', 'is-off'],
  healthy: ['Healthy', 'is-on'],
  lagging: ['Lagging', 'is-busy'],
  failing: ['Failing', 'is-problem'],
};

export function translationSummary(data) {
  const minutes = Math.round((data.window_seconds || 300) / 60);
  const partial = data.complete === false ? ' The server is very busy, so only its latest segments are counted.' : '';
  if (data.state === 'idle') return `Nothing translated in the last ${minutes} minutes.${partial}`;
  const parts = [`${data.translated} translated`];
  if (data.stale) parts.push(`${data.stale} dropped because the engine was busy`);
  if (data.failed) parts.push(`${data.failed} failed`);
  return `${parts.join(', ')} in the last ${minutes} minutes.${partial}`;
}
