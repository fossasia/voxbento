/**
 * Split datetime-local inputs into separate date + time inputs.
 * Safari's native datetime-local popup only offers a date calendar, so users
 * cannot discover how to change the time. 
 * Loaded as an ES module with jQuery and inline scripts.
 */

function splitValue(value) {
  const v = value || '';
  const i = v.indexOf('T');
  return i === -1 ? { date: v, time: '' } : { date: v.slice(0, i), time: v.slice(i + 1) };
}

function normalize(v) {
  const { date, time } = splitValue(v);
  const [hh = '00', mm = '00', ss = '00'] = time.split(':');
  return `${date}T${hh}:${mm}:${ss.split('.')[0]}`;
}

function labelTextFor(input) {
  const wrapping = input.closest('label');
  if (wrapping) {
    const clone = wrapping.cloneNode(true);
    clone.querySelectorAll('input, select, textarea, button').forEach((el) => el.remove());
    return clone.textContent.trim();
  }
  if (input.id) {
    const escaped = typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(input.id) : input.id;
    const forLabel = document.querySelector(`label[for="${escaped}"]`);
    if (forLabel) return forLabel.textContent.trim();
  }
  return input.getAttribute('aria-label') || '';
}

function enhance(input) {
  if (input.dataset.splitReady === '1') return;
  input.dataset.splitReady = '1';

  const initial = input.value;
  const { date, time } = splitValue(initial);
  const min = input.min;
  const max = input.max;
  const labelText = labelTextFor(input);

  const dateInput = document.createElement('input');
  dateInput.type = 'date';
  if (min) dateInput.min = min.split('T')[0];
  if (max) dateInput.max = max.split('T')[0];

  const timeInput = document.createElement('input');
  timeInput.type = 'time';
  const stepSeconds = input.step && input.step !== 'any' ? parseFloat(input.step) : 0;
  if (input.step) timeInput.step = 'any';
  else if (time.split(':').length > 2) timeInput.step = '1';
  const stepBase = normalize(min || input.getAttribute('value') || '1970-01-01T00:00');

  const visible = [dateInput, timeInput];
  visible.forEach((el) => {
    el.required = input.required;
    el.disabled = input.disabled;
    el.className = input.className;
    const formId = input.getAttribute('form');
    if (formId) el.setAttribute('form', formId);
  });
  dateInput.value = date;
  timeInput.value = time;
  if (labelText) {
    dateInput.setAttribute('aria-label', `${labelText} (date)`);
    timeInput.setAttribute('aria-label', `${labelText} (time, UTC)`);
  }

  
  const validate = () => {
    timeInput.setCustomValidity('');
    if (!dateInput.value || !timeInput.value) return;
    const v = normalize(`${dateInput.value}T${timeInput.value}`);
    if (min && v < normalize(min)) {
      timeInput.setCustomValidity(`Must be ${min.replace('T', ' ')} or later.`);
    } else if (max && v > normalize(max)) {
      timeInput.setCustomValidity(`Must be ${max.replace('T', ' ')} or earlier.`);
    } else if (stepSeconds > 0) {
      const diff = Date.parse(`${v}Z`) / 1000 - Date.parse(`${stepBase}Z`) / 1000;
      const rest = Math.abs(diff % stepSeconds);
      if (rest > 1e-6 && stepSeconds - rest > 1e-6) {
        timeInput.setCustomValidity('Please enter a valid time for this field.');
      }
    }
  };

  const update = () => {
    // A date with no time means midnight, shown in the field so nothing is hidden.
    if (dateInput.value && !timeInput.value) timeInput.value = '00:00';
    validate();
    input.value = dateInput.value ? `${dateInput.value}T${timeInput.value}` : '';
  };


  const emit = () => {
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  visible.forEach((el) => {
    el.addEventListener('input', (e) => { e.stopPropagation(); update(); emit(); });
    el.addEventListener('change', (e) => { e.stopPropagation(); update(); emit(); });
  });


  input.form?.addEventListener('reset', () => {
    setTimeout(() => {
      const v = splitValue(initial);
      dateInput.value = v.date;
      timeInput.value = v.time;
      input.value = initial;
      validate();
    }, 0);
  });

  input.removeAttribute('required');
  input.type = 'hidden';
  input.after(dateInput, timeInput);
}

export function initSplitDatetime(root = document) {
  root.querySelectorAll('input[type="datetime-local"][data-split-datetime]').forEach(enhance);
}

initSplitDatetime();
