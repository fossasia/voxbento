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

    transProviderSelect.addEventListener('change', loadTranslationModels);
    if (transProviderSelect.value) {
      loadTranslationModels();
    }

    // The TTS provider's voices and help text are handled by tts-voice-picker.js (loaded by admin.js).

    // Language tag picker
    function initLangPicker(cfg) {
      const tagsEl = document.getElementById(cfg.tagsId);
      const searchEl = document.getElementById(cfg.searchId);
      const dropdownEl = document.getElementById(cfg.dropdownId);
      const hiddenEl = document.getElementById(cfg.hiddenId);
      const selected = new Set(cfg.selectedCodes);
      let filtered = [];
      let focusedIdx = -1;

      function renderTags() {
        tagsEl.innerHTML = '';
        selected.forEach(code => {
          const lang = cfg.allLanguages.find(l => l.code === code);
          if (!lang) return;
          const tag = document.createElement('span');
          tag.className = 'lang-tag';
          const nameSpan = document.createElement('span');
          nameSpan.textContent = lang.name + ' (' + lang.code + ')';
          const btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'lang-tag-remove';
          btn.setAttribute('aria-label', 'Remove ' + lang.name);
          btn.textContent = '×';
          btn.addEventListener('click', () => { selected.delete(code); renderTags(); syncHidden(); });
          tag.appendChild(nameSpan);
          tag.appendChild(btn);
          tagsEl.appendChild(tag);
        });
      }

      function syncHidden() {
        hiddenEl.innerHTML = '';
        selected.forEach(code => {
          const inp = document.createElement('input');
          inp.type = 'hidden';
          inp.name = cfg.inputName;
          inp.value = code;
          hiddenEl.appendChild(inp);
        });
      }

      function renderDropdown(q) {
        const ql = q.toLowerCase();
        filtered = cfg.allLanguages.filter(l =>
          !selected.has(l.code) &&
          (l.name.toLowerCase().includes(ql) || l.code.toLowerCase().includes(ql))
        ).slice(0, 60);
        focusedIdx = -1;
        dropdownEl.innerHTML = '';
        if (filtered.length === 0) { dropdownEl.hidden = true; return; }
        filtered.forEach(lang => {
          const li = document.createElement('li');
          li.textContent = lang.name + ' (' + lang.code + ')';
          li.addEventListener('mousedown', e => { e.preventDefault(); addLang(lang.code); });
          dropdownEl.appendChild(li);
        });
        dropdownEl.hidden = false;
      }

      function addLang(code) {
        selected.add(code);
        searchEl.value = '';
        renderTags();
        syncHidden();
        dropdownEl.hidden = true;
        searchEl.focus();
      }

      searchEl.addEventListener('input', () => renderDropdown(searchEl.value));
      searchEl.addEventListener('focus', () => renderDropdown(searchEl.value));
      searchEl.addEventListener('blur', () => { setTimeout(() => { dropdownEl.hidden = true; }, 150); });
      searchEl.addEventListener('keydown', e => {
        if (dropdownEl.hidden) return;
        const items = dropdownEl.querySelectorAll('li');
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          focusedIdx = Math.min(focusedIdx + 1, filtered.length - 1);
          items.forEach((li, i) => li.classList.toggle('lp-focused', i === focusedIdx));
          if (items[focusedIdx]) items[focusedIdx].scrollIntoView({ block: 'nearest' });
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          focusedIdx = Math.max(focusedIdx - 1, 0);
          items.forEach((li, i) => li.classList.toggle('lp-focused', i === focusedIdx));
        } else if (e.key === 'Enter') {
          e.preventDefault();
          if (focusedIdx >= 0 && filtered[focusedIdx]) addLang(filtered[focusedIdx].code);
        } else if (e.key === 'Escape') {
          dropdownEl.hidden = true;
          focusedIdx = -1;
        }
      });

      renderTags();
      syncHidden();
    }

    initLangPicker({
      allLanguages: JSON.parse(root.dataset.translationLanguages || '[]'),
      selectedCodes: JSON.parse(root.dataset.selectedTranslationLanguages || '[]'),
      inputName: 'floor_translation_languages',
      tagsId: 'lp_floor_translation_tags',
      searchId: 'lp_floor_translation_search',
      dropdownId: 'lp_floor_translation_dropdown',
      hiddenId: 'lp_floor_translation_hidden',
    });

    if (document.getElementById('lp_floor_tts_tags')) {
      initLangPicker({
        allLanguages: JSON.parse(root.dataset.ttsLanguages || '[]'),
        selectedCodes: JSON.parse(root.dataset.selectedTtsLanguages || '[]'),
        inputName: 'floor_tts_languages',
        tagsId: 'lp_floor_tts_tags',
        searchId: 'lp_floor_tts_search',
        dropdownId: 'lp_floor_tts_dropdown',
        hiddenId: 'lp_floor_tts_hidden',
      });
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
