// Booth page (owner view): the model pickers and the translation language picker.
// The language list and the saved choices come from the #booth-detail-config JSON block
// (no inline script).

import { initLangPicker } from './lang-picker.js';
import { bindModelFilter } from './model-filter.js';

bindModelFilter(
  document.getElementById('transcription_provider'),
  document.getElementById('transcription_model'),
  'provider',
);
bindModelFilter(
  document.getElementById('translation_provider'),
  document.getElementById('translation_model'),
  'transProvider',
);

const configEl = document.getElementById('booth-detail-config');
if (configEl) {
  const config = JSON.parse(configEl.textContent);
  initLangPicker({
    tagsEl: document.getElementById('lp_translation_tags'),
    searchEl: document.getElementById('lp_translation_search'),
    dropdownEl: document.getElementById('lp_translation_dropdown'),
    hiddenEl: document.getElementById('lp_translation_hidden'),
    languages: config.languages,
    selectedCodes: config.selected_codes,
    inputName: 'translation_languages',
  });
}

function initTabs() {
  const root = document.getElementById('booth-detail');
  const tabList = document.querySelector('[data-booth-tabs]');
  if (!root || !tabList) return;
  const tabs = [...tabList.querySelectorAll('[data-booth-tab]')];
  const panels = [...document.querySelectorAll('[data-booth-panel]')];
  const storageKey = `voxbento:booth-tab:${root.dataset.boothId}`;

  function show(name, { focus = false } = {}) {
    const target = tabs.find((t) => t.dataset.boothTab === name) || tabs[0];
    tabs.forEach((tab) => {
      const selected = tab === target;
      tab.setAttribute('aria-selected', String(selected));
      tab.tabIndex = selected ? 0 : -1;
      tab.classList.toggle('is-active', selected);
    });
    panels.forEach((panel) => { panel.hidden = panel.dataset.boothPanel !== target.dataset.boothTab; });
    if (focus) target.focus();
    try { sessionStorage.setItem(storageKey, target.dataset.boothTab); } catch (err) { console.debug('Booth tab not remembered', err); }
    if (window.location.hash !== `#${target.dataset.boothTab}`) {
      history.replaceState(null, '', `${window.location.pathname}${window.location.search}#${target.dataset.boothTab}`);
    }
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => show(tab.dataset.boothTab));
    tab.addEventListener('keydown', (e) => {
      const step = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
      if (e.key === 'Home' || e.key === 'End' || step) {
        e.preventDefault();
        const next = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (index + step + tabs.length) % tabs.length;
        show(tabs[next].dataset.boothTab, { focus: true });
      }
    });
  });

  document.querySelectorAll('[data-booth-tab-link]').forEach((link) => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      show(link.dataset.boothTabLink, { focus: true });
    });
  });

  let initial = window.location.hash.slice(1) || new URLSearchParams(window.location.search).get('saved');
  if (!initial) {
    try { initial = sessionStorage.getItem(storageKey) || ''; } catch (err) { console.debug('No remembered booth tab', err); }
  }
  tabList.hidden = false;
  show(initial || 'overview');
}

initTabs();
