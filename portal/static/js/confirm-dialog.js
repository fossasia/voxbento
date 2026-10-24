// Plain confirmation dialog shared by admin pages (Users, Team).
// Needs a <dialog data-users-confirm> with [data-confirm-text] and [data-confirm-ok].

const dialog = document.querySelector('[data-users-confirm]');

export function confirmChange(message) {
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

// Forms with data-confirm-message ask first, then submit normally.
export function confirmBeforeSubmit(root = document) {
  root.querySelectorAll('form[data-confirm-message]').forEach((form) => {
    form.addEventListener('submit', async (event) => {
      if (form.dataset.confirmed) return;
      event.preventDefault();
      if (await confirmChange(form.dataset.confirmMessage)) {
        form.dataset.confirmed = '1';
        form.requestSubmit();
      }
    });
  });
}
