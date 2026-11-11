/**
 * Developer dashboard client-side utilities.
 * Loaded as an ES module — no jQuery, no inline scripts.
 */

const COPY_FEEDBACK_MS = 2000;
const copyFeedbackByElement = new WeakMap();

/**
 * Copies text synchronously within the click gesture to keep credential writes ordered.
 */
function copyText(text) {
  if (typeof document.execCommand !== 'function') {
    return false;
  }

  let textarea;
  try {
    textarea = document.createElement('textarea');
    textarea.value = text;
    textarea.readOnly = true;
    textarea.style.position = 'fixed';
    textarea.style.opacity = '0';
    document.body.appendChild(textarea);
    textarea.focus();
    textarea.select();
    return document.execCommand('copy');
  } catch (error) {
    console.error('Clipboard copy failed:', error);
    return false;
  } finally {
    textarea?.remove();
  }
}

/**
 * Resets copy feedback only while the same attempt still owns the element.
 */
function scheduleCopyFeedbackReset(element, tooltip, state) {
  state.timerId = setTimeout(() => {
    if (copyFeedbackByElement.get(element) !== state) {
      return;
    }

    element.classList.remove('copied', 'copy-failed');

    if (tooltip) {
      tooltip.textContent = 'Copy to clipboard';
      tooltip.classList.remove('copied-tooltip');
    }
    copyFeedbackByElement.delete(element);
  }, COPY_FEEDBACK_MS);
}

/**
 * Copies text to clipboard and updates the UI to show success or failure.
 * Feedback resets after 2 seconds.
 */
async function copyToClipboard(element, text) {
  let state;
  let tooltip;
  try {
    const previousState = copyFeedbackByElement.get(element);
    if (previousState?.timerId) {
      clearTimeout(previousState.timerId);
    }

    state = { timerId: null };
    copyFeedbackByElement.set(element, state);
    element.classList.remove('copied', 'copy-failed');
    tooltip = element.querySelector('.copy-tooltip');
    if (tooltip) {
      tooltip.textContent = 'Copying…';
      tooltip.classList.remove('copied-tooltip');
    }

    const copied = copyText(text);
    if (copyFeedbackByElement.get(element) !== state) {
      return;
    }

    element.classList.add(copied ? 'copied' : 'copy-failed');

    if (tooltip) {
      tooltip.textContent = copied ? 'Copied!' : 'Copy failed';
      if (copied) {
        tooltip.classList.add('copied-tooltip');
      } else {
        tooltip.classList.remove('copied-tooltip');
      }
    }

    if (!copied) {
      console.error('Failed to copy credential with the synchronous clipboard API');
    }

    scheduleCopyFeedbackReset(element, tooltip, state);
  } catch (error) {
    console.error('Unexpected error while copying developer credential:', error);
    if (state && copyFeedbackByElement.get(element) !== state) {
      return;
    }

    try {
      element.classList.remove('copied');
      element.classList.add('copy-failed');
      if (tooltip) {
        tooltip.textContent = 'Copy failed';
        tooltip.classList.remove('copied-tooltip');
      }
      if (state) {
        scheduleCopyFeedbackReset(element, tooltip, state);
      }
    } catch (feedbackError) {
      console.error('Could not display clipboard failure feedback:', feedbackError);
    }
  }
}

/**
 * Initializes copy-to-clipboard functionality using event delegation.
 * Elements with data-copy-text attribute will copy the specified text when clicked.
 */
function initCopyToClipboard() {
  document.addEventListener('click', (e) => {
    const wrapper = e.target.closest('.copy-wrapper[data-copy-text]');
    if (wrapper) {
      const text = wrapper.dataset.copyText;
      return copyToClipboard(wrapper, text);
    }
  });
}

/**
 * Initializes the "Create App" modal toggle.
 */
function initCreateAppModal() {
  const btnCreate = document.getElementById('btn-create-app');
  const modalCreate = document.getElementById('modal-create-app');

  if (btnCreate && modalCreate) {
    btnCreate.addEventListener('click', () => {
      modalCreate.classList.add('active');
    });
  }

  // Close modal when clicking outside or on cancel button
  const cancelBtn = modalCreate?.querySelector('button[type="button"]');
  if (modalCreate && cancelBtn) {
    cancelBtn.addEventListener('click', () => {
      modalCreate.classList.remove('active');
    });
  }

  if (modalCreate) {
    modalCreate.addEventListener('click', (e) => {
      if (e.target === modalCreate) {
        modalCreate.classList.remove('active');
      }
    });
  }

  // Initialize new secret modal close button if present
  const modalNewSecret = document.getElementById('modal-new-secret');
  const secretCloseBtn = modalNewSecret?.querySelector('button[type="button"]');
  if (modalNewSecret && secretCloseBtn) {
    secretCloseBtn.addEventListener('click', () => {
      modalNewSecret.classList.remove('active');
    });
  }
}

document.addEventListener('DOMContentLoaded', () => {
  initCopyToClipboard();
  initCreateAppModal();
});
