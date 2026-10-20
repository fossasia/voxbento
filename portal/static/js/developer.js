/**
 * Developer dashboard client-side utilities.
 * Loaded as an ES module — no jQuery, no inline scripts.
 */

/**
 * Copies text to clipboard and updates the UI to show feedback.
 * Handles tooltip text change and icon state with a 2-second reset.
 */
async function copyToClipboard(element, text) {
  try {
    await navigator.clipboard.writeText(text);
    element.classList.add('copied');

    const tooltip = element.querySelector('.copy-tooltip');

    if (tooltip) {
      tooltip.textContent = 'Copied!';
      tooltip.classList.add('copied-tooltip');
    }

    setTimeout(() => {
      element.classList.remove('copied');

      if (tooltip) {
        tooltip.textContent = 'Copy to clipboard';
        tooltip.classList.remove('copied-tooltip');
      }
    }, 2000);
  } catch (err) {
    console.error('Failed to copy text: ', err);
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
