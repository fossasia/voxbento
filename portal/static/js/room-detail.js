// Room page (owner view): tabs, the room audio chooser and the AI settings pickers.
// Values the page needs come from data attributes on #room-detail (no inline script).

const root = document.getElementById('room-detail');

// ── Tabs ────────────────────────────────────────────────────────────────
// Without JavaScript every section shows in order. The open tab is remembered per
// room, so saving a setting (which reloads the page) returns to the same tab.

function initTabs() {
  const tabList = document.querySelector('[data-room-tabs]');
  if (!tabList || !root) return;
  const tabs = [...tabList.querySelectorAll('[data-room-tab]')];
  const panels = [...document.querySelectorAll('[data-room-panel]')];
  const storageKey = `voxbento:room-tab:${root.dataset.roomId}`;

  function show(name, { focus = false } = {}) {
    const target = tabs.find((t) => t.dataset.roomTab === name) || tabs[0];
    tabs.forEach((tab) => {
      const selected = tab === target;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      tab.classList.toggle('is-active', selected);
    });
    panels.forEach((panel) => { panel.hidden = panel.dataset.roomPanel !== target.dataset.roomTab; });
    if (focus) target.focus();
    try { sessionStorage.setItem(storageKey, target.dataset.roomTab); } catch (err) { console.debug('Tab not remembered', err); }
    if (window.location.hash !== `#${target.dataset.roomTab}`) {
      history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${target.dataset.roomTab}`);
    }
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => show(tab.dataset.roomTab));
    tab.addEventListener('keydown', (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (e.key === 'Home' || e.key === 'End' || step) {
        e.preventDefault();
        const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (index + step + tabs.length) % tabs.length;
        show(tabs[next].dataset.roomTab, { focus: true });
      }
    });
  });

  const params = new URLSearchParams(window.location.search);
  // Links elsewhere on the page (e.g. the next-steps list) can open a tab.
  document.querySelectorAll('[data-room-tab-link]').forEach((link) => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      show(link.dataset.roomTabLink, { focus: true });
    });
  });
  let initial = window.location.hash.slice(1);
  // Back from a vocabulary upload: show the result where it was uploaded.
  if (!initial && [...params.keys()].some((key) => key.startsWith('vocab'))) initial = 'ai';
  // Older links used other tab names.
  initial = { source: 'audio', captions: 'ai' }[initial] || initial;
  // Back from room setup: show the saved languages and the next steps.
  if (params.has('created') || params.has('updated')) initial = 'overview';
  if (!initial) {
    try { initial = sessionStorage.getItem(storageKey) || ''; } catch (err) { console.debug('No remembered tab', err); }
  }
  tabList.hidden = false;
  show(initial || 'overview');
}

// ── Room audio ──────────────────────────────────────────────────────────
// One way of hearing the room is shown at a time. Choosing one only changes what is
// shown; each source's own buttons connect or disconnect it.

function initAudioChooser() {
  const chooser = document.querySelector('[data-audio-chooser]');
  if (!chooser) return;
  const radios = [...chooser.querySelectorAll('input[name="audio_choice"]')];
  const panels = [...document.querySelectorAll('[data-audio-panel]')];

  function sync() {
    const choice = radios.find((r) => r.checked)?.value || '';
    panels.forEach((panel) => { panel.hidden = panel.dataset.audioPanel !== choice; });
  }

  radios.forEach((radio) => radio.addEventListener('change', sync));
  sync();
}

// ── AI settings ─────────────────────────────────────────────────────────

function initAiSettings() {
  const speechProvider = document.getElementById('floor_transcription_provider');
  const speechModel = document.getElementById('floor_transcription_model');
  if (speechProvider && speechModel) {
    const options = [...speechModel.options];
    const filterModels = () => {
      const visible = options.filter((opt) => opt.dataset.provider === speechProvider.value);
      options.forEach((opt) => {
        opt.hidden = !visible.includes(opt);
        opt.disabled = !visible.includes(opt);
      });
      if (!visible.some((opt) => opt.selected) && visible.length) visible[0].selected = true;
      const modelDetails = speechModel.closest('details');
      if (modelDetails) modelDetails.hidden = visible.length < 2;
    };
    speechProvider.addEventListener('change', filterModels);
    filterModels();
  }

  const transProvider = document.getElementById('floor_translation_provider');
  const transModel = document.getElementById('floor_translation_model');
  if (transProvider && transModel) {
    const savedProvider = transProvider.value;
    const savedModel = root.dataset.initialTranslationModel || '';
    const localNote = document.querySelector('[data-local-translation-note]');
    let catalog = null;

    async function loadModels() {
      const provider = transProvider.value;
      if (localNote) localNote.hidden = provider !== 'local';
      transModel.replaceChildren(new Option('Recommended for this service', ''));
      if (!provider) return;
      try {
        if (!catalog) {
          const res = await fetch('/api/admin/providers/translation/models');
          catalog = res.ok ? await res.json() : {};
        }
      } catch (err) {
        console.error('Could not load translation models', err);
        catalog = {};
      }
      const models = [...(catalog[provider] || [])];
      const keep = provider === savedProvider ? savedModel : '';
      if (keep && !models.includes(keep)) models.unshift(keep);
      models.forEach((model) => transModel.append(new Option(model, model, false, model === keep)));
    }

    transProvider.addEventListener('change', loadModels);
    loadModels();
  }

  const ttsProvider = document.getElementById('floor_tts_provider');
  if (ttsProvider) {
    const voiceGroup = document.getElementById('tts_voice_group');
    const deepgramHelp = document.getElementById('tts_provider_help_deepgram');
    const syncVoice = () => {
      const supertonic = ttsProvider.value === 'supertonic';
      if (voiceGroup) voiceGroup.hidden = !supertonic;
      if (deepgramHelp) deepgramHelp.hidden = ttsProvider.value !== 'deepgram';
    };
    ttsProvider.addEventListener('change', syncVoice);
    syncVoice();
  }
}

initTabs();
initAudioChooser();
initAiSettings();
