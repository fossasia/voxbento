// Searchable language multi-select. Chosen languages show as removable tags and are
// submitted as repeated hidden inputs, so the surrounding form needs no extra glue.

const MAX_SUGGESTIONS = 60;

export function matchLanguages(languages, selected, query, limit = MAX_SUGGESTIONS) {
  const needle = query.toLowerCase();
  return languages
    .filter((lang) => !selected.has(lang.code)
      && (lang.name.toLowerCase().includes(needle) || lang.code.toLowerCase().includes(needle)))
    .slice(0, limit);
}

const label = (lang) => `${lang.name} (${lang.code})`;

export function initLangPicker({ tagsEl, searchEl, dropdownEl, hiddenEl, languages, selectedCodes, inputName }) {
  const byCode = new Map(languages.map((lang) => [lang.code, lang]));
  const selected = new Set(selectedCodes);
  let matches = [];
  let focusedIndex = -1;

  function renderTags() {
    // A saved code the language list does not know stays selected, just without a tag.
    const known = [...selected].filter((code) => byCode.has(code));
    tagsEl.replaceChildren(...known.map((code) => {
      const lang = byCode.get(code);
      const tag = document.createElement('span');
      tag.className = 'lang-tag';
      const name = document.createElement('span');
      name.textContent = label(lang);
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'lang-tag-remove';
      remove.setAttribute('aria-label', `Remove ${lang.name}`);
      remove.textContent = '×';
      remove.addEventListener('click', () => {
        selected.delete(code);
        renderSelection();
      });
      tag.append(name, remove);
      return tag;
    }));
  }

  function renderHiddenInputs() {
    hiddenEl.replaceChildren(...[...selected].map((code) => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = inputName;
      input.value = code;
      return input;
    }));
  }

  function renderSelection() {
    renderTags();
    renderHiddenInputs();
  }

  function renderDropdown(query) {
    matches = matchLanguages(languages, selected, query);
    focusedIndex = -1;
    dropdownEl.replaceChildren(...matches.map((lang) => {
      const item = document.createElement('li');
      item.textContent = label(lang);
      // mousedown, not click: the search box would blur and hide the list first.
      item.addEventListener('mousedown', (e) => {
        e.preventDefault();
        addLanguage(lang.code);
      });
      return item;
    }));
    dropdownEl.hidden = matches.length === 0;
  }

  function addLanguage(code) {
    selected.add(code);
    searchEl.value = '';
    renderSelection();
    dropdownEl.hidden = true;
    searchEl.focus();
  }

  function moveFocus(next) {
    focusedIndex = next;
    const items = [...dropdownEl.children];
    items.forEach((item, i) => item.classList.toggle('lp-focused', i === focusedIndex));
    items[focusedIndex]?.scrollIntoView({ block: 'nearest' });
  }

  searchEl.addEventListener('input', () => renderDropdown(searchEl.value));
  searchEl.addEventListener('focus', () => renderDropdown(searchEl.value));
  // Wait a moment so a click on a suggestion still lands before the list hides.
  searchEl.addEventListener('blur', () => { setTimeout(() => { dropdownEl.hidden = true; }, 150); });
  searchEl.addEventListener('keydown', (e) => {
    // The Enter that confirms an IME composition must not pick a suggestion.
    if (dropdownEl.hidden || e.isComposing) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveFocus(Math.min(focusedIndex + 1, matches.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveFocus(Math.max(focusedIndex - 1, 0));
    } else if (e.key === 'Enter') {
      // Enter picks the highlighted suggestion instead of submitting the form.
      e.preventDefault();
      if (matches[focusedIndex]) addLanguage(matches[focusedIndex].code);
    } else if (e.key === 'Escape') {
      dropdownEl.hidden = true;
      focusedIndex = -1;
    }
  });

  renderSelection();
}
