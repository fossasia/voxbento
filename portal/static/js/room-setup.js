// Room setup (add a room / change a room's languages). Values come from data attributes
// on #room-setup; there is no inline script. Without JavaScript the form shows every
// step at once and still submits; this script turns it into one step at a time when
// adding a room, puts the review on its own screen when changing one, keeps the
// per-language choices in sync and shows only the questions that apply. Each language
// reaches listeners one way: an interpreter, an AI voice or AI captions. Captions are the
// speaker's own words and stay on whenever a language uses AI.

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
  const voiceSupport = readJson(root.dataset.voiceSupport || '{}', {});

  const steps = [...form.querySelectorAll('[data-rs-step]')];
  const progress = root.querySelector('[data-rs-progress]');
  const spokenSelect = form.querySelector('[data-rs-spoken]');
  const chipsBox = form.querySelector('[data-rs-chips]');
  const moreSelect = form.querySelector('[data-rs-more]');
  const deliveryGroup = form.querySelector('[data-rs-delivery]');
  const deliveryList = form.querySelector('[data-rs-delivery-list]');
  const rowTemplate = document.getElementById('rs-delivery-row-template');
  const captionsBox = form.querySelector('[data-rs-captions]');
  const captionsToggle = form.querySelector('[data-rs-caption-toggle]');
  const captionsForced = form.querySelector('[data-rs-captions-forced]');
  const captionsReason = form.querySelector('[data-rs-captions-reason]');
  const captionContext = form.querySelector('[data-rs-caption-context]');
  const captionTitle = form.querySelector('[data-rs-caption-title]');
  const roomContext = form.querySelector('[data-rs-room-context]');
  const enginesGroup = form.querySelector('[data-rs-engines]');
  const advanced = form.querySelector('[data-rs-advanced]');
  const engineSummary = form.querySelector('[data-rs-engine-summary]');
  const voiceSelect = form.querySelector('#rs-voice');
  const noLanguages = form.querySelector('[data-rs-no-languages]');
  const review = form.querySelector('[data-rs-review]');
  const warnings = form.querySelector('[data-rs-warnings]');
  const reviewFallback = form.querySelector('[data-rs-review-fallback]');

  let captionsWanted = captionsBox.checked;
  let initialReview = null;
  let current = 0;
  const reviewStep = steps.length - 1;

  // ── Reading the answers ────────────────────────────────────────────────

  const chipInputs = () => [...chipsBox.querySelectorAll('input[name="languages"]')];
  const chosenCodes = () => chipInputs().filter((input) => input.checked && !input.disabled).map((input) => input.value);
  const nameOf = (code) => chipsBox.querySelector(`[data-rs-chip="${code}"] span`)?.textContent.trim() || code;
  const selectedText = (select) => select?.selectedOptions[0]?.textContent.split(' — ')[0].trim() || '';

  const rowOf = (code) => deliveryList.querySelector(`[data-rs-delivery-row="${code}"]`);
  const deliveryOf = (code) => rowOf(code)?.querySelector('input[type="radio"]:checked')?.value || '';
  const isAi = (delivery) => delivery === 'ai_voice' || delivery === 'ai_text';

  const languages = () => chosenCodes().map((code) => ({ code, name: nameOf(code), delivery: deliveryOf(code) }));
  const aiLanguages = () => languages().filter((l) => isAi(l.delivery));
  const usesAi = () => aiLanguages().length > 0;
  const voiceLanguages = () => languages().filter((l) => l.delivery === 'ai_voice');
  const spokenName = () => selectedText(spokenSelect) || 'the room\'s language';

  function listNames(names) {
    if (names.length < 2) return names.join('');
    return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
  }

  // ── AI voice support ───────────────────────────────────────────────────

  const voiceEngines = () => [...(voiceSelect?.options || [])].map((opt) => ({
    provider: opt.value,
    label: opt.textContent.split(' — ')[0].trim(),
  }));
  const speaks = (provider, code) => (voiceSupport[provider] || []).includes(code);

  // Why an AI voice can't be offered for this language with the services chosen, or ''.
  function voiceProblem(code) {
    const engines = voiceEngines();
    if (!engines.length) {
      return `AI voice isn't available for this event yet: no voice service is set up. Choose AI captions, or add a Deepgram key on the event's API keys page.`;
    }
    const chosen = engines.find((e) => e.provider === voiceSelect.value) || engines[0];
    if (speaks(chosen.provider, code)) return '';
    const name = nameOf(code);
    const others = engines.filter((e) => e !== chosen && speaks(e.provider, code));
    if (others.length) {
      return `${chosen.label} can't speak ${name}. Choose AI captions, or switch the voice to ${others[0].label} under AI services in the review step.`;
    }
    return `AI voice isn't supported for ${name} yet. None of this event's voice services can speak it. Choose AI captions so listeners can read ${name}, or Interpreter.`;
  }

  // A language with no voice service that can speak it can't use AI voice at all.
  const voiceImpossible = (code) => !voiceEngines().some((e) => speaks(e.provider, code));

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
      if (rowOf(code)) return;
      const row = rowTemplate.content.firstElementChild.cloneNode(true);
      row.dataset.rsDeliveryRow = code;
      const name = row.querySelector('.rs-delivery-name');
      name.textContent = nameOf(code);
      name.id = `rs-language-${code}`;
      row.querySelector('[role="radiogroup"]').setAttribute('aria-labelledby', name.id);
      row.querySelectorAll('input[type="radio"]').forEach((radio) => {
        radio.name = `delivery_${code}`;
        radio.checked = radio.value === 'interpreter';
      });
      deliveryList.append(row);
    });
    deliveryGroup.hidden = codes.length === 0;
    noLanguages.hidden = codes.length > 0;
    syncDeliveryMessages();
  }

  function syncDeliveryMessages() {
    chosenCodes().forEach((code) => {
      const row = rowOf(code);
      const message = row?.querySelector('[data-rs-delivery-error]');
      if (!message) return;
      const delivery = deliveryOf(code);
      let text = '';
      if (!delivery) {
        text = 'rsWasBoth' in row.dataset
          ? `${nameOf(code)} used to offer both an interpreter and AI. Choose one way for listeners to get it before saving.`
          : `Choose how listeners get ${nameOf(code)}.`;
      } else if (delivery === 'ai_voice') {
        text = voiceProblem(code);
      }
      message.textContent = text;
      message.hidden = !text;
      row.classList.toggle('has-problem', Boolean(text));
    });
  }

  // ── Captions & AI services ─────────────────────────────────────────────

  const shortName = (select) => select?.selectedOptions[0]?.dataset.short || selectedText(select);

  function syncAi() {
    captionsBox.setCustomValidity('');
    const ai = aiLanguages();
    const spoken = spokenName();
    roomContext.replaceChildren(
      'Speakers in ',
      Object.assign(document.createElement('strong'), { textContent: form.querySelector('#rs-name').value.trim() || 'this room' }),
      ` use ${spoken}. Choose the other languages listeners can pick.`,
    );
    captionTitle.textContent = `Captions in ${spoken}`;
    captionContext.textContent = `Listeners can read what is said, in ${spoken}, as it is spoken.`;

    // When a language uses AI, captions are on and there is nothing to switch.
    captionsBox.checked = ai.length ? true : captionsWanted;
    captionsToggle.hidden = ai.length > 0;
    captionsForced.hidden = ai.length === 0;
    if (ai.length) {
      const verb = ai.length === 1 ? 'uses' : 'use';
      captionsReason.textContent = `${listNames(ai.map((l) => l.name))} ${verb} AI. The AI translates from these captions, so they stay on while any language uses AI. Listeners can read them in ${spoken} too.`;
    }

    const shows = { speech: captionsBox.checked, translation: ai.length > 0, voice: voiceLanguages().length > 0 };
    Object.entries(shows).forEach(([kind, show]) => {
      enginesGroup.querySelector(`[data-rs-engine="${kind}"]`).hidden = !show;
    });
    enginesGroup.hidden = !captionsBox.checked;
    const parts = [];
    if (shows.speech) parts.push(shortName(form.querySelector('#rs-speech')));
    if (shows.translation) parts.push(shortName(form.querySelector('#rs-translation')));
    if (shows.voice && voiceSelect?.options.length) parts.push(shortName(voiceSelect));
    engineSummary.textContent = parts.join(' · ') || 'Change AI services';
  }

  // The switch is hidden while AI forces captions on; make sure the form still sends it.
  function captionsValueForSubmit() {
    let hidden = form.querySelector('input[type="hidden"][name="captions"]');
    if (captionsToggle.hidden && captionsBox.checked) {
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

  function reviewValues() {
    const values = new Map();
    const terms = [...review.querySelectorAll('dt')];
    const details = [...review.querySelectorAll('dd')];
    terms.forEach((term, i) => {
      if (term.textContent !== 'Listener languages') values.set(term.textContent, details[i].textContent);
    });
    languages().forEach((lang) => values.set(lang.name, deliveryLabels[lang.delivery] || 'Interpreter and AI (choose one)'));
    return values;
  }

  function renderChanges() {
    if (isCreate || !initialReview) return;
    const container = form.querySelector('[data-rs-changes]');
    const list = form.querySelector('[data-rs-change-list]');
    list.replaceChildren();
    const currentValues = reviewValues();
    new Set([...initialReview.keys(), ...currentValues.keys()]).forEach((key) => {
      const before = initialReview.get(key);
      const after = currentValues.get(key);
      if (before === after) return;
      const item = document.createElement('li');
      item.textContent = `${key}: ${before || 'Not offered'} → ${after || 'Removed'}`;
      list.append(item);
    });
    if (!list.childElementCount) {
      const item = document.createElement('li');
      item.textContent = 'No changes to your current answers.';
      list.append(item);
    }
    container.hidden = false;
  }

  function renderReview() {
    review.replaceChildren();
    const langs = languages();
    const ai = usesAi();
    addReviewItem('Room', form.querySelector('#rs-name').value.trim() || 'No name yet');
    addReviewItem('Speakers use', selectedText(spokenSelect));
    addReviewItem(
      'Listener languages',
      langs.length ? langs.map((l) => `${l.name}: ${deliveryLabels[l.delivery] || 'not chosen yet'}`) : 'None',
    );
    const captions = ai || captionsBox.checked;
    addReviewItem('Captions', captions ? `On, in ${spokenName()}` : 'Off');
    renderChanges();
    review.hidden = false;
    reviewFallback.hidden = true;

    warnings.replaceChildren();
    const wantedBooths = new Set(langs.filter((l) => l.delivery === 'interpreter').map((l) => l.code));
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
      const missing = languages().find((l) => !l.delivery);
      if (missing) return rowOf(missing.code).querySelector('input[type="radio"]');
      const noVoice = voiceLanguages().find((l) => voiceImpossible(l.code));
      if (noVoice) return problemRadio(noVoice.code, voiceProblem(noVoice.code));
      if (!chosenCodes().length && !captionsBox.checked) {
        captionsBox.setCustomValidity('Choose at least one language, or turn on captions.');
        return captionsBox;
      }
      captionsBox.setCustomValidity('');
    }
    if (index === 2) {
      voiceSelect?.setCustomValidity('');
      const unspoken = voiceLanguages().filter((l) => voiceProblem(l.code));
      if (unspoken.length && voiceSelect?.options.length) {
        advanced.open = true;
        voiceSelect.setCustomValidity(
          `${shortName(voiceSelect)} can't speak ${listNames(unspoken.map((l) => l.name))}. Choose another voice, or go back and choose AI captions for ${unspoken.length === 1 ? 'it' : 'them'}.`,
        );
        return voiceSelect;
      }
    }
    return [...step.querySelectorAll('input, select')].find((el) => {
      const hiddenAncestor = el.closest('[hidden]');
      const shown = !hiddenAncestor || hiddenAncestor === step;
      return shown && !el.disabled && !el.checkValidity();
    }) || null;
  }

  // Point at a language's AI voice choice with the reason it can't be used.
  function problemRadio(code, message) {
    const radio = rowOf(code).querySelector('input[value="ai_voice"]');
    radio.setCustomValidity(message);
    return radio;
  }

  function report(field) {
    if (field.closest('details') && !field.closest('details').open) field.closest('details').open = true;
    if (field.validity.valueMissing && field.type === 'radio') {
      field.setCustomValidity('Choose one of these options.');
      field.reportValidity();
      field.setCustomValidity('');
      return;
    }
    field.reportValidity();
    if (field.type === 'radio') field.setCustomValidity('');
  }

  // Adding a room shows one step at a time. Changing a room shows every step on one screen
  // and the review on the next.
  function isShown(i) {
    if (isCreate) return i === current;
    return current === reviewStep ? i === reviewStep : i !== reviewStep;
  }

  function show(index, { focus = true } = {}) {
    current = Math.max(0, Math.min(index, steps.length - 1));
    steps.forEach((step, i) => { step.hidden = !isShown(i); });
    [...(progress?.querySelectorAll('[data-rs-progress-step]') || [])].forEach((item, i) => {
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
    if (isCreate) {
      const problem = stepProblem(current);
      if (problem) {
        report(problem);
        return;
      }
      syncAll();
      show(current + 1);
      return;
    }
    for (let i = 0; i < reviewStep; i += 1) {
      const problem = stepProblem(i);
      if (problem) {
        report(problem);
        return;
      }
    }
    syncAll();
    show(reviewStep);
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
  deliveryList.addEventListener('change', (event) => {
    // A new choice replaces any earlier "AI voice can't be used" error on this language.
    event.target.closest('[data-rs-delivery-row]')?.querySelectorAll('input[type="radio"]')
      .forEach((radio) => radio.setCustomValidity(''));
    syncAll();
  });
  captionsBox.addEventListener('change', () => {
    captionsWanted = captionsBox.checked;
    captionsBox.setCustomValidity('');
    syncAll();
  });
  form.querySelectorAll('[data-rs-engines] select, #rs-name').forEach((el) => {
    el.addEventListener('input', renderReview);
    el.addEventListener('change', () => {
      voiceSelect?.setCustomValidity('');
      syncAll();
    });
  });

  form.addEventListener('submit', (event) => {
    // Pressing Enter before the review moves on instead of saving unseen changes.
    if (current !== reviewStep) {
      event.preventDefault();
      next();
      return;
    }
    for (let i = 0; i < steps.length; i += 1) {
      const problem = stepProblem(i);
      if (problem) {
        event.preventDefault();
        show(i, { focus: false });
        report(problem);
        return;
      }
    }
    captionsValueForSubmit();
  });

  form.querySelectorAll(isCreate ? '[data-rs-nav]' : '[data-rs-edit-nav]').forEach((el) => { el.hidden = false; });
  form.querySelectorAll('[data-rs-next]').forEach((btn) => btn.addEventListener('click', next));
  form.querySelectorAll('[data-rs-back]').forEach((btn) => btn.addEventListener('click', () => show(isCreate ? current - 1 : 0)));
  if (progress) progress.hidden = false;
  // After a rejected submit, open on the first step that needs attention.
  const firstProblem = root.querySelector('.alert-danger') ? steps.findIndex((_, i) => stepProblem(i)) : -1;
  syncAll();
  // A rejected POST contains submitted answers, not the saved room baseline.
  initialReview = root.querySelector('.alert-danger') ? null : reviewValues();
  renderChanges();
  show(firstProblem >= 0 ? firstProblem : 0, { focus: false });
}
