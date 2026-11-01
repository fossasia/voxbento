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

function initReportTabs() {
  const tabList = document.querySelector('[data-report-tabs]');
  if (!tabList) return;
  const tabs = [...tabList.querySelectorAll('[data-report-tab]')];
  const panels = [...document.querySelectorAll('[data-report-panel]')];

  function show(name, { focus = false } = {}) {
    const target = tabs.find((t) => t.dataset.reportTab === name) || tabs[0];
    tabs.forEach((tab) => {
      const selected = tab === target;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      tab.classList.toggle('is-active', selected);
    });
    panels.forEach((panel) => { panel.hidden = panel.dataset.reportPanel !== target.dataset.reportTab; });
    if (focus) target.focus();
    if (window.location.hash !== `#${target.dataset.reportTab}`) {
      history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${target.dataset.reportTab}`);
    }
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => show(tab.dataset.reportTab));
    tab.addEventListener('keydown', (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (!step) return;
      e.preventDefault();
      show(tabs[(index + step + tabs.length) % tabs.length].dataset.reportTab, { focus: true });
    });
  });

  tabList.hidden = false;
  show(window.location.hash.slice(1) || 'summary');
}

initReportTabs();
