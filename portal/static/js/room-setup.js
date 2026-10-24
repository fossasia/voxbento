// Room setup (add a room / change a room's languages). Values come from data attributes
// on #room-setup; there is no inline script. Without JavaScript the form shows every
// step at once and still submits; this script turns it into one step at a time when
// adding a room, keeps the per-language choices in sync and shows only the questions
// that apply.

const root = document.getElementById('room-setup');
const form = document.getElementById('room-setup-form');

function readJson(value, fallback) {
  try {
    return JSON.parse(value);
  } catch (err) {
    console.error('Room setup: could not read page data', err);
    return fallback;
  }
}

if (root && form) {
  const isCreate = root.dataset.mode === 'create';
  const boothCodes = readJson(root.dataset.boothCodes || '[]', []);
  const liveCodes = new Set(readJson(root.dataset.liveCodes || '[]', []));
  const deliveryLabels = readJson(root.dataset.deliveryLabels || '{}', {});

  const steps = [...form.querySelectorAll('[data-rs-step]')];
  const progress = root.querySelector('[data-rs-progress]');
  const spokenSelect = form.querySelector('[data-rs-spoken]');
  const chipsBox = form.querySelector('[data-rs-chips]');
  const moreSelect = form.querySelector('[data-rs-more]');
  const deliveryGroup = form.querySelector('[data-rs-delivery]');
  const deliveryList = form.querySelector('[data-rs-delivery-list]');
  const rowTemplate = document.getElementById('rs-delivery-row-template');
  const captionsBox = form.querySelector('[data-rs-captions]');
  const captionsForced = form.querySelector('[data-rs-captions-forced]');
  const aiOutputGroup = form.querySelector('[data-rs-ai-output]');
  const enginesGroup = form.querySelector('[data-rs-engines]');
  const nothingAi = form.querySelector('[data-rs-nothing-ai]');
  const noLanguages = form.querySelector('[data-rs-no-languages]');
  const review = form.querySelector('[data-rs-review]');
  const warnings = form.querySelector('[data-rs-warnings]');
  const reviewFallback = form.querySelector('[data-rs-review-fallback]');

  let captionsWanted = captionsBox.checked;
  let current = 0;

  // ── Reading the answers ────────────────────────────────────────────────

  const mode = () => form.querySelector('input[name="mode"]:checked')?.value || '';
  const chipInputs = () => [...chipsBox.querySelectorAll('input[name="languages"]')];
  const chosenCodes = () => chipInputs().filter((input) => input.checked && !input.disabled).map((input) => input.value);
  const nameOf = (code) => chipsBox.querySelector(`[data-rs-chip="${code}"] span`)?.textContent.trim() || code;
  const selectedText = (select) => select?.selectedOptions[0]?.textContent.split(' — ')[0].trim() || '';

  function deliveryOf(code) {
    const m = mode();
    if (m === 'interpreters') return 'interpreter';
    if (m === 'ai') return 'ai';
    const select = deliveryList.querySelector(`[data-rs-delivery-row="${code}"] select`);
    return select ? select.value : 'both';
  }

  const languages = () => chosenCodes().map((code) => ({ code, name: nameOf(code), delivery: deliveryOf(code) }));
  const usesAi = () => languages().some((l) => l.delivery === 'ai' || l.delivery === 'both');
  const aiVoice = () => form.querySelector('input[name="ai_output"]:checked')?.value !== 'captions';

  // ── Languages ──────────────────────────────────────────────────────────

  function addChip(code, name) {
    let input = chipsBox.querySelector(`[data-rs-chip="${code}"] input`);
    if (!input) {
      const label = document.createElement('label');
      label.className = 'rs-chip';
      label.dataset.rsChip = code;
      input = document.createElement('input');
      input.type = 'checkbox';
      input.name = 'languages';
      input.value = code;
      const span = document.createElement('span');
      span.textContent = name;
      label.append(input, span);
      chipsBox.append(label);
    }
    input.checked = true;
  }

  function syncSpoken() {
    const spoken = spokenSelect.value;
    chipInputs().forEach((input) => {
      const isSpoken = input.value === spoken && !boothCodes.includes(input.value);
      input.disabled = isSpoken;
      if (isSpoken) input.checked = false;
      input.closest('.rs-chip').classList.toggle('is-spoken', isSpoken);
      input.closest('.rs-chip').title = isSpoken ? 'This is the language spoken in the room' : '';
    });
  }

  function syncDeliveryRows() {
    const codes = chosenCodes();
    [...deliveryList.querySelectorAll('[data-rs-delivery-row]')].forEach((row) => {
      if (!codes.includes(row.dataset.rsDeliveryRow)) row.remove();
    });
    codes.forEach((code) => {
      if (deliveryList.querySelector(`[data-rs-delivery-row="${code}"]`)) return;
      const row = rowTemplate.content.firstElementChild.cloneNode(true);
      row.dataset.rsDeliveryRow = code;
      const label = row.querySelector('label');
      const select = row.querySelector('select');
      label.textContent = nameOf(code);
      label.htmlFor = `rs-delivery-${code}`;
      select.id = `rs-delivery-${code}`;
      select.name = `delivery_${code}`;
      if (boothCodes.includes(code)) select.value = 'interpreter';
      deliveryList.append(row);
    });
    deliveryGroup.hidden = mode() !== 'both' || codes.length === 0;
    noLanguages.hidden = codes.length > 0 || !mode();
  }

  // ── Captions & AI ──────────────────────────────────────────────────────

  function syncAi() {
    captionsBox.setCustomValidity('');
    const ai = usesAi();
    if (ai) {
      captionsBox.checked = true;
    } else {
      captionsBox.checked = captionsWanted;
    }
    captionsBox.disabled = ai;
    captionsForced.hidden = !ai;
    aiOutputGroup.hidden = !ai;

    const showSpeech = ai || captionsBox.checked;
    const shows = { speech: showSpeech, translation: ai, voice: ai && aiVoice() };
    Object.entries(shows).forEach(([kind, show]) => {
      enginesGroup.querySelector(`[data-rs-engine="${kind}"]`).hidden = !show;
    });
    enginesGroup.hidden = !showSpeech;
    nothingAi.hidden = showSpeech;
  }

  // A disabled checkbox is not submitted, so when AI forces captions on, send it anyway.
  function captionsValueForSubmit() {
    let hidden = form.querySelector('input[type="hidden"][name="captions"]');
    if (captionsBox.disabled && captionsBox.checked) {
      if (!hidden) {
        hidden = document.createElement('input');
        hidden.type = 'hidden';
        hidden.name = 'captions';
        hidden.value = 'on';
        form.append(hidden);
      }
    } else if (hidden) {
      hidden.remove();
    }
  }

  // ── Review ─────────────────────────────────────────────────────────────

  function addReviewItem(term, details) {
    const dt = document.createElement('dt');
    dt.textContent = term;
    const dd = document.createElement('dd');
    if (Array.isArray(details)) {
      const list = document.createElement('ul');
      details.forEach((text) => {
        const li = document.createElement('li');
        li.textContent = text;
        list.append(li);
      });
      dd.append(list);
    } else {
      dd.textContent = details;
    }
    review.append(dt, dd);
  }

  function modeLabel() {
    return { interpreters: 'Human interpreters', ai: 'AI interpretation', both: 'Interpreters and AI' }[mode()] || 'Not chosen yet';
  }

  function renderReview() {
    review.replaceChildren();
    const langs = languages();
    const ai = usesAi();
    addReviewItem('Room', form.querySelector('#rs-name').value.trim() || 'No name yet');
    addReviewItem('Speakers use', selectedText(spokenSelect));
    addReviewItem('Interpretation', modeLabel());
    addReviewItem(
      'Listener languages',
      langs.length ? langs.map((l) => `${l.name}: ${deliveryLabels[l.delivery] || l.delivery}`) : 'None (captions only)',
    );
    const captions = ai || captionsBox.checked;
    addReviewItem('Live captions', captions ? `On, by ${selectedText(form.querySelector('#rs-speech'))}` : 'Off');
    if (ai) {
      const how = aiVoice()
        ? `Translated by ${selectedText(form.querySelector('#rs-translation'))}, spoken by ${selectedText(form.querySelector('#rs-voice'))}`
        : `Translated by ${selectedText(form.querySelector('#rs-translation'))}, captions only`;
      addReviewItem('AI languages', how);
    }
    review.hidden = false;
    reviewFallback.hidden = true;

    warnings.replaceChildren();
    const wantedBooths = new Set(langs.filter((l) => l.delivery !== 'ai').map((l) => l.code));
    boothCodes.filter((code) => !wantedBooths.has(code)).forEach((code) => {
      const li = document.createElement('li');
      li.textContent = liveCodes.has(code)
        ? `The ${nameOf(code)} booth is live right now, so it will be kept. Remove it after the session.`
        : `The ${nameOf(code)} booth will be removed, with its invite links and assigned interpreters.`;
      warnings.append(li);
    });
    warnings.hidden = warnings.childElementCount === 0;
  }

  function syncAll() {
    syncSpoken();
    syncDeliveryRows();
    syncAi();
    renderReview();
  }

  // ── Step-by-step (adding a room) ──────────────────────────────────────

  function stepProblem(index) {
    const step = steps[index];
    if (index === 0) {
      const name = form.querySelector('#rs-name');
      if (!name.value.trim()) return name;
    }
    if (index === 1) {
      const firstMode = form.querySelector('input[name="mode"]');
      if (!mode()) return firstMode;
    }
    if (index === 2) {
      if (!chosenCodes().length && !captionsBox.checked) {
        captionsBox.setCustomValidity('Turn on captions, or go back and choose at least one language.');
        return captionsBox;
      }
      captionsBox.setCustomValidity('');
    }
    return [...step.querySelectorAll('input, select')].find((el) => {
      const hiddenAncestor = el.closest('[hidden]');
      const shown = !hiddenAncestor || hiddenAncestor === step;
      return shown && !el.disabled && !el.checkValidity();
    }) || null;
  }

  function report(field) {
    if (field.validity.valueMissing && field.type === 'radio') {
      field.setCustomValidity('Choose one of these options.');
      field.reportValidity();
      field.setCustomValidity('');
      return;
    }
    field.reportValidity();
  }

  function show(index, { focus = true } = {}) {
    current = Math.max(0, Math.min(index, steps.length - 1));
    steps.forEach((step, i) => { step.hidden = i !== current; });
    [...progress.querySelectorAll('[data-rs-progress-step]')].forEach((item, i) => {
      item.classList.toggle('is-active', i === current);
      item.classList.toggle('is-done', i < current);
      if (i === current) item.setAttribute('aria-current', 'step');
      else item.removeAttribute('aria-current');
    });
    if (focus) {
      const legend = steps[current].querySelector('.rs-step-title');
      legend.tabIndex = -1;
      legend.focus();
    }
  }

  function next() {
    const problem = stepProblem(current);
    if (problem) {
      report(problem);
      return;
    }
    syncAll();
    show(current + 1);
  }

  // ── Wiring ─────────────────────────────────────────────────────────────

  form.noValidate = true;
  reviewFallback.hidden = true;

  spokenSelect.addEventListener('change', syncAll);
  chipsBox.addEventListener('change', syncAll);
  moreSelect.addEventListener('change', () => {
    const code = moreSelect.value;
    if (!code) return;
    if (code !== spokenSelect.value || boothCodes.includes(code)) addChip(code, selectedText(moreSelect));
    moreSelect.value = '';
    syncAll();
  });
  form.querySelectorAll('input[name="mode"]').forEach((radio) => radio.addEventListener('change', syncAll));
  deliveryList.addEventListener('change', syncAll);
  captionsBox.addEventListener('change', () => {
    captionsWanted = captionsBox.checked;
    captionsBox.setCustomValidity('');
    syncAll();
  });
  form.querySelectorAll('input[name="ai_output"], [data-rs-engines] select, #rs-name').forEach((el) => {
    el.addEventListener('input', renderReview);
    el.addEventListener('change', syncAll);
  });

  form.addEventListener('submit', (event) => {
    for (let i = 0; i < steps.length; i += 1) {
      const problem = stepProblem(i);
      if (problem) {
        event.preventDefault();
        if (isCreate) show(i, { focus: false });
        report(problem);
        return;
      }
    }
    captionsValueForSubmit();
  });

  if (isCreate) {
    form.querySelectorAll('[data-rs-nav]').forEach((el) => { el.hidden = false; });
    form.querySelectorAll('[data-rs-next]').forEach((btn) => btn.addEventListener('click', next));
    form.querySelectorAll('[data-rs-back]').forEach((btn) => btn.addEventListener('click', () => show(current - 1)));
    progress.hidden = false;
    // After a rejected submit, open on the first step that needs attention.
    const firstProblem = root.querySelector('.alert-danger') ? steps.findIndex((_, i) => stepProblem(i)) : -1;
    syncAll();
    show(firstProblem >= 0 ? firstProblem : 0, { focus: false });
  } else {
    syncAll();
  }
}
