// Room page (owner view): tabs, provider/model pickers and the translation language picker.
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
  let initial = window.location.hash.slice(1);
  // Back from a vocabulary upload: show the result where it was uploaded.
  if (!initial && [...params.keys()].some((key) => key.startsWith('vocab'))) initial = 'captions';
  if (!initial) {
    try { initial = sessionStorage.getItem(storageKey) || ''; } catch (err) { console.debug('No remembered tab', err); }
  }
  tabList.hidden = false;
  show(initial || 'overview');
}

// ── Captions & translation pickers (moved unchanged from the page) ───────

function initPickers() {
    const providerSelect = document.getElementById('floor_transcription_provider');
    if (!providerSelect) return;
    const modelSelect = document.getElementById('floor_transcription_model');
    const allOptions = Array.from(modelSelect.options);

    function filterModels() {
      const selectedProvider = providerSelect.value;
      let firstVisible = null;
      let currentSelectedStillVisible = false;

      allOptions.forEach(opt => {
        if (opt.getAttribute('data-provider') === selectedProvider) {
          opt.style.display = '';
          opt.disabled = false;
          if (!firstVisible) firstVisible = opt;
          if (opt.selected) currentSelectedStillVisible = true;
        } else {
          opt.style.display = 'none';
          opt.disabled = true;
        }
      });

      if (!currentSelectedStillVisible && firstVisible) {
        firstVisible.selected = true;
      }
    }

    providerSelect.addEventListener('change', filterModels);
    filterModels();

    const transProviderSelect = document.getElementById('floor_translation_provider');
    const transModelSelect = document.getElementById('floor_translation_model');
    const initialTransModel = root.dataset.initialTranslationModel || '';

    async function loadTranslationModels() {
      const provider = transProviderSelect.value;
      if (provider === 'none') {
        transModelSelect.innerHTML = '<option value="none" data-provider="none">-- None --</option>';
      } else {
        transModelSelect.innerHTML = '<option value="">Select a Model...</option>';
      }
      if (!provider) return;

      try {
        const res = await fetch('/api/admin/providers/translation/models');
        if (res.ok) {
          const data = await res.json();
          const models = data[provider] || [];
          models.forEach(model => {
            const opt = document.createElement('option');
            opt.value = model;
            opt.textContent = model;
            if (model === initialTransModel) {
              opt.selected = true;
            }
            transModelSelect.appendChild(opt);
          });
        }
      } catch (err) {
        console.error("Failed to load models", err);
      }
    }

    transProviderSelect.addEventListener('change', loadTranslationModels);
    if (transProviderSelect.value) {
      loadTranslationModels();
    }

    // TTS provider → toggle Supertonic voice picker + provider help text
    const ttsProviderSelect = document.getElementById('floor_tts_provider');
    if (ttsProviderSelect) {
      const ttsVoiceGroup = document.getElementById('tts_voice_group');
      const helpDeepgram = document.getElementById('tts_provider_help_deepgram');
      const helpSupertonic = document.getElementById('tts_provider_help_supertonic');

      function syncTtsProvider() {
        const isSupertonic = ttsProviderSelect.value === 'supertonic';
        if (ttsVoiceGroup) ttsVoiceGroup.hidden = !isSupertonic;
        if (helpDeepgram) helpDeepgram.hidden = isSupertonic;
        if (helpSupertonic) helpSupertonic.hidden = !isSupertonic;
      }

      ttsProviderSelect.addEventListener('change', syncTtsProvider);
      syncTtsProvider();
    }

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
  }

initTabs();
initPickers();
