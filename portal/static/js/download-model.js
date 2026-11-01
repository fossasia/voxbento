// Built-in models (the NLLB translator and the Supertonic voice) download on first use.
// When the room's AI settings choose one that isn't on the server yet, saving asks first,
// then saves, starts the download and shows its progress.

const DOWNLOADS = {
  nllb: {
    wanted: () => document.getElementById('floor_translation_provider')?.value === 'local',
    model: () => document.getElementById('floor_translation_model')?.value || 'nllb-200-distilled-600M',
    progressUrl: (prefix, model) => `${prefix}/models/download_progress?model=${encodeURIComponent(model)}`,
    trigger: (prefix, model) => fetch(`${prefix}/models/trigger_download`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model }),
    }),
  },
  supertonic: {
    wanted: () => document.getElementById('floor_tts_provider')?.value === 'supertonic',
    model: () => '',
    progressUrl: (prefix) => `${prefix}/models/supertonic/download_progress`,
    trigger: (prefix) => fetch(`${prefix}/models/supertonic/trigger_download`, { method: 'POST' }),
  },
};

const byId = (id) => document.getElementById(id);

async function isDownloaded(prefix, name) {
  const download = DOWNLOADS[name];
  try {
    const res = await fetch(download.progressUrl(prefix, download.model()));
    return res.ok && (await res.json()).status === 'completed';
  } catch (err) {
    console.debug('Download status unavailable', err);
    return false;
  }
}

function watch(prefix, name) {
  const download = DOWNLOADS[name];
  const model = download.model();
  byId(`${name}_download_progress_container`).hidden = false;
  return new Promise((resolve) => {
    const interval = setInterval(async () => {
      const res = await fetch(download.progressUrl(prefix, model));
      if (!res.ok) return;
      const data = await res.json();
      if (data.status === 'downloading' || data.n > 0) {
        const pct = data.total > 0 ? Math.round((data.n / data.total) * 100) : 0;
        byId(`${name}_download_bar`).style.width = `${pct}%`;
        byId(`${name}_download_percent`).textContent = `${pct}%`;
        byId(`${name}_download_speed`).textContent = `${data.rate ? (data.rate / (1024 * 1024)).toFixed(1) : '0.0'} MB/s`;
      }
      if (data.status === 'completed' || data.status === 'error') {
        clearInterval(interval);
        byId(`${name}_download_progress_container`).hidden = true;
        if (data.status === 'completed') byId(`${name}_success_modal`).hidden = false;
        else alert('The download failed. Try saving again, or choose another service.');
        resolve();
      }
    }, 1000);
  });
}

export function initLocalModelDownloader() {
  const form = byId('room_ai_form');
  if (!form) return;
  const prefix = document.body.dataset.managementPrefix || '/admin';
  let pending = [];

  // Ask about each missing model in turn; once all are answered, save and download.
  function askNext() {
    const name = pending[0];
    if (name) {
      byId(`${name}_download_modal`).hidden = false;
      return;
    }
    form.submit();
  }

  async function saveAndDownload(names) {
    try {
      const response = await fetch(form.getAttribute('action'), { method: 'POST', body: new FormData(form), redirect: 'follow' });
      if (!response.ok) throw new Error(`Could not save AI settings (${response.status})`);
      for (const name of names) {
        const downloadResponse = await DOWNLOADS[name].trigger(prefix, DOWNLOADS[name].model());
        if (!downloadResponse.ok) throw new Error(`Could not start ${name} download (${downloadResponse.status})`);
        await watch(prefix, name);
      }
    } catch (err) {
      console.error('Could not save or start the download', err);
      alert('Could not save the settings or start the download. Please try again.');
    }
  }

  const accepted = [];
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    accepted.length = 0;
    pending = [];
    for (const name of Object.keys(DOWNLOADS)) {
      if (DOWNLOADS[name].wanted() && !(await isDownloaded(prefix, name))) pending.push(name);
    }
    if (pending.length) askNext();
    else form.submit();
  });

  Object.keys(DOWNLOADS).forEach((name) => {
    byId(`${name}_proceed_btn`)?.addEventListener('click', () => {
      byId(`${name}_download_modal`).hidden = true;
      accepted.push(pending.shift());
      if (pending.length) askNext();
      else saveAndDownload(accepted);
    });
    byId(`${name}_cancel_btn`)?.addEventListener('click', () => {
      byId(`${name}_download_modal`).hidden = true;
      pending = [];
    });
    byId(`${name}_success_got_it_btn`)?.addEventListener('click', () => {
      byId(`${name}_success_modal`).hidden = true;
      if (!pending.length) window.location.reload();
    });
  });
}
