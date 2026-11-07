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
