/**
 * Developer dashboard client-side utilities.
 * Loaded as an ES module — no jQuery, no inline scripts.
 */

const COPY_FEEDBACK_MS = 2000;
const copyFeedbackByElement = new WeakMap();
let clipboardWriteQueue = null;

async function copyTextNow(text, allowSynchronousFallback) {
  if (allowSynchronousFallback && typeof document.execCommand === 'function') {
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
      if (document.execCommand('copy')) {
        return true;
      }
    } catch (error) {
      console.error('Clipboard fallback failed:', error);
    } finally {
      textarea?.remove();
    }
  }

  // Fallback failed or unavailable, try async Clipboard API
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (error) {
      console.error('Clipboard API failed:', error);
      return false;
    }
  }

  return false;
}

function copyText(text) {
  // Only an operation started immediately can use execCommand in the click gesture.
  const allowSynchronousFallback = clipboardWriteQueue === null;
  const operation = clipboardWriteQueue
    ? clipboardWriteQueue.then(
        () => copyTextNow(text, allowSynchronousFallback),
        () => copyTextNow(text, allowSynchronousFallback),
      )
    : copyTextNow(text, allowSynchronousFallback);
  clipboardWriteQueue = operation;

  const clearQueue = () => {
    if (clipboardWriteQueue === operation) {
      clipboardWriteQueue = null;
    }
  };
  operation.then(clearQueue, clearQueue);

  return operation;
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

    const copied = await copyText(text);
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
      console.error('Failed to copy text using available clipboard methods');
    }

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
