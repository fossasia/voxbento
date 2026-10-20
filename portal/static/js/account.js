// Account page (#767): reveal the password forms and submit them to the auth API.

const ENDPOINTS = {
  change: { url: '/api/auth/set-password', success: 'password_changed' },
  set: { url: '/api/auth/set-password', success: 'password_set' },
  remove: { url: '/api/auth/remove-password', success: 'password_removed' },
};

function setPanel(panel, open) {
  const opener = document.querySelector(`[data-reveal="${panel.id}"]`);
  panel.hidden = !open;
  opener?.setAttribute('aria-expanded', String(open));
  if (open) {
    panel.querySelector('input')?.focus();
  } else {
    panel.reset();
    const error = panel.querySelector('[data-form-error]');
    if (error) error.hidden = true;
    opener?.focus();
  }
}

document.querySelectorAll('[data-reveal]').forEach((btn) => {
  const panel = document.getElementById(btn.dataset.reveal);
  if (panel) btn.addEventListener('click', () => setPanel(panel, panel.hidden));
});

document.querySelectorAll('[data-reveal-close]').forEach((btn) => {
  const panel = document.getElementById(btn.dataset.revealClose);
  if (panel) btn.addEventListener('click', () => setPanel(panel, false));
});

document.querySelectorAll('form[data-password-form]').forEach((form) => {
  const endpoint = ENDPOINTS[form.dataset.passwordForm];
  const error = form.querySelector('[data-form-error]');
  const submit = form.querySelector('button[type="submit"]');

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!endpoint || !form.reportValidity()) return;
    error.hidden = true;
    submit.disabled = true;
    try {
      const res = await fetch(endpoint.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.fromEntries(new FormData(form))),
      });
      if (res.ok) {
        window.location.assign(`/account?success=${endpoint.success}`);
        return;
      }
      const data = await res.json().catch(() => ({}));
      error.textContent = typeof data.detail === 'string' ? data.detail : 'Something went wrong. Please try again.';
    } catch {
      error.textContent = 'Could not reach VoxBento. Check your connection and try again.';
    }
    error.hidden = false;
    submit.disabled = false;
  });
});
