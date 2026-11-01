const root = document.getElementById('room-analysis');
const POLL_MS = 5000;
const MAX_POLLS = 420;

function pendingIds() {
  return [...document.querySelectorAll('[data-report-status="pending"], [data-report-status="running"]')]
    .map((el) => el.dataset.reportId);
}

async function finished(id) {
  const url = `/api/admin/events/${root.dataset.eventId}/rooms/${root.dataset.roomId}/analysis/${id}`;
  try {
    const res = await fetch(url, { headers: { Accept: 'application/json' } });
    if (!res.ok) return false;
    const report = await res.json();
    return report.status !== 'pending' && report.status !== 'running';
  } catch (err) {
    console.error('Could not check meeting report', id, err);
    return false;
  }
}

async function poll(count) {
  const ids = pendingIds();
  if (!ids.length || count >= MAX_POLLS) return;
  const done = await Promise.all(ids.map(finished));
  if (done.some(Boolean)) {
    window.location.reload();
    return;
  }
  setTimeout(() => poll(count + 1), POLL_MS);
}

if (root) setTimeout(() => poll(0), POLL_MS);
