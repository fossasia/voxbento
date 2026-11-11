// Developer dashboard: the create app and new secret dialogs, and the copy buttons.

import { closeDialog, initDialog, openDialog } from './modal-dialog.js';

const COPIED_MS = 2000;

export async function copyToClipboard(button) {
  try {
    await navigator.clipboard.writeText(button.dataset.copyText);
  } catch (err) {
    console.error('Failed to copy text: ', err);
    return;
  }
  button.classList.add('copied');
  const tooltip = button.querySelector('.copy-tooltip');
  if (tooltip) {
    tooltip.textContent = 'Copied!';
    tooltip.classList.add('copied-tooltip');
  }
  setTimeout(() => {
    button.classList.remove('copied');
    if (tooltip) {
      tooltip.textContent = 'Copy to clipboard';
      tooltip.classList.remove('copied-tooltip');
    }
  }, COPIED_MS);
}

function wireDialog(dialog) {
  initDialog(dialog);
  dialog.querySelector('[data-dialog-close]').addEventListener('click', () => closeDialog(dialog));
}

export function initDeveloperDashboard() {
  const createDialog = document.getElementById('modal-create-app');
  const createButton = document.getElementById('btn-create-app');
  if (createDialog && createButton) {
    wireDialog(createDialog);
    createButton.addEventListener('click', () => openDialog(createDialog, document.getElementById('app_name')));

    // A second click while the answer is slow would create a second app whose secret nobody sees.
    const form = createDialog.querySelector('form');
    let submitting = false;
    form.addEventListener('submit', (event) => {
      if (submitting) event.preventDefault();
      submitting = true;
    });
    window.addEventListener('pageshow', (event) => {
      if (event.persisted) submitting = false;
    });
  }

  // Only rendered right after an app is created; the secret is shown this once.
  // Back/forward can bring the page back from the cache and rerun this script, so it stays closed then.
  const secretDialog = document.getElementById('modal-new-secret');
  if (secretDialog) {
    wireDialog(secretDialog);
    const [navigation] = performance.getEntriesByType('navigation');
    if (navigation?.type !== 'back_forward') {
      openDialog(secretDialog, secretDialog.querySelector('.secret-box-danger'));
    }
  }

  document.querySelectorAll('[data-copy-text]').forEach((button) => {
    button.addEventListener('click', () => copyToClipboard(button));
  });
}

if (typeof document !== 'undefined') initDeveloperDashboard();
