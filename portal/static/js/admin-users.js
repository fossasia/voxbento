// Admin Users list (#765): on/off switches for hosting access, Admin and Active,
// changed in place like Eventyay's admin users page. Without JavaScript each
// switch is a plain form that posts and reloads.

const live = document.querySelector('[data-users-live]');
const dialog = document.querySelector('[data-users-confirm]');

function announce(message, kind = 'success') {
  if (!live) return;
  live.textContent = '';
  const note = document.createElement('div');
  note.className = `alert alert-${kind === 'error' ? 'danger' : 'success'}`;
  note.textContent = message;
  live.append(note);
  clearTimeout(announce.timer);
  announce.timer = setTimeout(() => note.remove(), 6000);
}

function confirmChange(message) {
  if (!message) return Promise.resolve(true);
  if (!dialog || typeof dialog.showModal !== 'function') return Promise.resolve(window.confirm(message));
  dialog.querySelector('[data-confirm-text]').textContent = message;
  // Escape closes the dialog without setting returnValue, so clear the previous answer.
  dialog.returnValue = '';
  return new Promise((resolve) => {
    dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true });
    dialog.showModal();
    dialog.querySelector('[data-confirm-ok]')?.focus();
  });
}

function setSwitch(form, on) {
  form.querySelector('[role="switch"]').setAttribute('aria-checked', String(on));
  form.querySelector('input[name="value"]').value = on ? 'false' : 'true';
}

// Making someone an Admin also means they can always create events.
function syncHostingSwitch(row, isAdmin) {
  const hosting = row.querySelector('[data-user-toggle][data-field="hosting"]');
  const button = hosting?.querySelector('[role="switch"]');
  if (!button) return;
  if (isAdmin) {
    setSwitch(hosting, true);
    button.disabled = true;
    button.title = 'Admins can always create events';
  } else {
    setSwitch(hosting, row.dataset.canHost === 'true');
    button.disabled = false;
    button.title = 'Hosting access';
  }
}

function syncVerifiedTag(row, isVerified) {
  let tag = row.querySelector('[data-unverified-tag]');
  if (isVerified) {
    if (tag) tag.remove();
  } else {
    if (!tag && !row.querySelector('.ul-tag--pending')) {
      const email = row.querySelector('.ul-email');
      if (email) {
        tag = document.createElement('span');
        tag.className = 'ul-tag';
        tag.dataset.unverifiedTag = '';
        tag.textContent = 'Email not verified';
        email.after(tag);
      }
    }
  }
}

async function onToggle(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('[role="switch"]');
  if (button.disabled || form.dataset.busy) return;

  const turningOn = form.querySelector('input[name="value"]').value === 'true';
  const message = turningOn ? form.dataset.confirmOn : form.dataset.confirmOff;
  if (!(await confirmChange(message))) return;

  form.dataset.busy = '1';
  setSwitch(form, turningOn);
  try {
    const res = await fetch(form.action, {
      method: 'POST',
      headers: { Accept: 'application/json' },
      body: new URLSearchParams({ field: form.dataset.field, value: String(turningOn) }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) throw new Error(data.detail || 'The change could not be saved. Please try again.');
    const row = form.closest('[data-user-row]');
    if (form.dataset.field === 'hosting') row.dataset.canHost = String(turningOn);
    if (form.dataset.field === 'admin') syncHostingSwitch(row, turningOn);
    if (form.dataset.field === 'active') row.classList.toggle('is-deactivated', !turningOn);
    if (form.dataset.field === 'verified') syncVerifiedTag(row, turningOn);
    announce(data.message);
  } catch (err) {
    setSwitch(form, !turningOn);
    announce(err.message, 'error');
  } finally {
    delete form.dataset.busy;
  }
}

document.querySelectorAll('form[data-user-toggle]').forEach((form) => form.addEventListener('submit', onToggle));

document.querySelectorAll('[data-auto-submit]').forEach((select) => {
  select.addEventListener('change', () => select.form.requestSubmit());
});

// User page: the delete dialog only enables its button once the email is typed exactly.
document.querySelectorAll('[data-open-dialog]').forEach((btn) => {
  const target = document.getElementById(btn.dataset.openDialog);
  if (!target || typeof target.showModal !== 'function') return;
  btn.addEventListener('click', () => target.showModal());
  target.querySelector('[data-close-dialog]')?.addEventListener('click', () => target.close());
  const input = target.querySelector('[data-confirm-match]');
  const submit = target.querySelector('[data-confirm-submit]');
  input?.addEventListener('input', () => {
    submit.disabled = input.value.trim().toLowerCase() !== input.dataset.confirmMatch.toLowerCase();
  });
});

// User page buttons that need a second look (Make Admin, Deactivate, …).
document.querySelectorAll('form[data-confirm-message]').forEach((form) => {
  form.addEventListener('submit', async (event) => {
    if (form.dataset.confirmed) return;
    event.preventDefault();
    if (await confirmChange(form.dataset.confirmMessage)) {
      form.dataset.confirmed = '1';
      form.requestSubmit();
    }
  });
});
