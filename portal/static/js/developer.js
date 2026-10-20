/**
 * Developer dashboard client-side utilities.
 * Loaded as an ES module — no jQuery, no inline scripts.
 */

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (error) {
      console.error('Clipboard API failed; trying execCommand fallback:', error);
    }
  }

  if (typeof document.execCommand !== 'function') return false;

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
    console.error('Clipboard fallback failed:', error);
    return false;
  } finally {
    textarea?.remove();
  }
}

/**
 * Copies text to clipboard and updates the UI to show success or failure.
 * Feedback resets after 2 seconds.
 */
async function copyToClipboard(element, text) {
  const copied = await copyText(text);
  const tooltip = element.querySelector('.copy-tooltip');

  element.classList.remove('copied', 'copy-failed');
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

  setTimeout(() => {
    element.classList.remove('copied', 'copy-failed');

    if (tooltip) {
      tooltip.textContent = 'Copy to clipboard';
      tooltip.classList.remove('copied-tooltip');
    }
  }, 2000);
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
      copyToClipboard(wrapper, text);
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
