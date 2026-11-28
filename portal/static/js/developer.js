// Developer dashboard: the create app and new secret dialogs, and the copy buttons.

import { closeDialog, initDialog, openDialog } from './modal-dialog.js';

const COPIED_MS = 2000;
// Per-button feedback state: maps each button to the state object owned by its latest copy attempt.
// An older attempt that resolves late checks whether it still owns the button before mutating the DOM.
const copyFeedbackByButton = new WeakMap();

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
  // Cancel any pending feedback reset for this button so the new attempt starts clean.
  const previous = copyFeedbackByButton.get(button);
  if (previous?.timerId) clearTimeout(previous.timerId);

  const state = {};
  copyFeedbackByButton.set(button, state);

  // Clear both states before setting the new one so a failed copy followed by a
  // successful one never leaves the button in the wrong colour.
  button.classList.remove('copied');
  button.classList.remove('copy-failed');

  const copied = await writeText(button, button.dataset.copyText);

  // A second click can replace our state entry while writeText was running.
  // If that happened, let the newer attempt own the DOM.
  if (copyFeedbackByButton.get(button) !== state) return;

  const outcome = copied ? 'copied' : 'copy-failed';
  button.classList.add(outcome);
  const tooltip = button.querySelector('.copy-tooltip');
  if (tooltip) {
    tooltip.textContent = copied ? 'Copied!' : 'Copy failed';
    if (copied) tooltip.classList.add('copied-tooltip');
  }
  state.timerId = setTimeout(() => {
    button.classList.remove(outcome);
    if (tooltip) {
      tooltip.textContent = 'Copy to clipboard';
      tooltip.classList.remove('copied-tooltip');
    }
    copyFeedbackByButton.delete(button);
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
