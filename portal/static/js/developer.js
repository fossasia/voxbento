// Developer dashboard: the create app and new secret dialogs, and the copy buttons.

import { closeDialog, initDialog, openDialog } from './modal-dialog.js';

const COPIED_MS = 2000;

// navigator.clipboard is missing on plain http outside localhost, so copy through a hidden textarea.
// It goes next to the button because the page behind an open modal dialog cannot take focus.
function copyWithTextarea(button, text) {
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  button.after(textarea);
  textarea.focus();
  textarea.select();
  try {
    return document.execCommand('copy');
  } finally {
    textarea.remove();
    button.focus();
  }
}

async function writeText(button, text) {
  if (navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (err) {
      console.error('navigator.clipboard.writeText rejected, trying the textarea fallback', err);
    }
  }
  try {
    if (copyWithTextarea(button, text)) return true;
    console.error('Could not copy to the clipboard');
  } catch (err) {
    console.error('Could not copy to the clipboard', err);
  }
  return false;
}

export async function copyToClipboard(button) {
  const copied = await writeText(button, button.dataset.copyText);
  const state = copied ? 'copied' : 'copy-failed';
  button.classList.add(state);
  const tooltip = button.querySelector('.copy-tooltip');
  if (tooltip) {
    tooltip.textContent = copied ? 'Copied!' : 'Copy failed';
    if (copied) tooltip.classList.add('copied-tooltip');
  }
  setTimeout(() => {
    button.classList.remove(state);
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
