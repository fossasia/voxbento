// Team page (#757): the role picker limits "Where" to places the role can be
// given (portal/roles.py), explains each role, and filters the people list.

import { confirmBeforeSubmit } from './confirm-dialog.js';

const roleSelect = document.querySelector('[data-team-role]');
const whereSelect = document.querySelector('[data-team-where]');
const help = document.querySelector('[data-team-help]');

function syncWhere() {
  if (!roleSelect || !whereSelect) return;
  const option = roleSelect.selectedOptions[0];
  const levels = (option?.dataset.levels || '').split(' ');
  if (help) help.textContent = option?.dataset.help || '';
  for (const place of whereSelect.querySelectorAll('option')) {
    const allowed = levels.includes(place.dataset.level);
    place.disabled = !allowed;
    place.hidden = !allowed;
  }
  for (const group of whereSelect.querySelectorAll('optgroup')) {
    group.hidden = [...group.querySelectorAll('option')].every((o) => o.hidden);
  }
  if (whereSelect.selectedOptions[0]?.disabled) {
    whereSelect.value = whereSelect.querySelector('option:not([disabled])')?.value || '';
  }
}

roleSelect?.addEventListener('change', syncWhere);
syncWhere();

// People list: search and role filter, applied together.
const people = [...document.querySelectorAll('[data-team-person]')];
const search = document.querySelector('[data-team-search]');
const empty = document.querySelector('[data-team-empty]');
let roleFilter = '';

function applyFilters() {
  const query = (search?.value || '').trim().toLowerCase();
  let shown = 0;
  for (const person of people) {
    const matchesRole = !roleFilter || person.dataset.roles.split(' ').includes(roleFilter);
    const matchesQuery = !query || person.dataset.search.includes(query);
    person.hidden = !(matchesRole && matchesQuery);
    if (!person.hidden) shown += 1;
  }
  if (empty) empty.hidden = shown > 0 || people.length === 0;
}

search?.addEventListener('input', applyFilters);
document.querySelectorAll('[data-team-filter]').forEach((tab) => {
  tab.addEventListener('click', () => {
    roleFilter = tab.dataset.teamFilter;
    document.querySelectorAll('[data-team-filter]').forEach((t) => {
      t.classList.toggle('is-active', t === tab);
      t.setAttribute('aria-pressed', String(t === tab));
    });
    applyFilters();
  });
});

confirmBeforeSubmit();
