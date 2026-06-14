/**
 * Admin panel client-side utilities.
 * Loaded as an ES module — no jQuery, no inline scripts.
 */

function copyToClipboard(targetId) {
  const el = document.getElementById(targetId);
  if (!el) return;
  const text = el.textContent.trim();
  const fullUrl = text.startsWith('/') ? window.location.origin + text : text;
  navigator.clipboard.writeText(fullUrl).then(() => {
    const btn = el.nextElementSibling;
    if (btn) {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => { btn.textContent = orig; }, 1500);
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.btn-copy[data-copy-target]').forEach((btn) => {
    btn.addEventListener('click', () => {
      copyToClipboard(btn.dataset.copyTarget);
    });
  });
  initCustomModal();
});

const FUNNY_WARNINGS = [
  "Are you sure? We can't undo this, but we can judge you.",
  "Warning: The intern will probably cry if you do this.",
  "We're deleting this forever. And forever is a very long time.",
  "Think of the bytes! Oh the humanity...",
  "Are you absolutely sure? The databases are getting nervous.",
  "There is no 'Ctrl+Z' for this. Proceed with caution.",
  "Deleting this is like dropping your ice cream. Tragic.",
  "Just double checking. My anxiety acts up around delete buttons."
];

function initCustomModal() {
  const modalOverlay = document.getElementById('custom-confirm-modal');
  const messageEl = document.getElementById('custom-confirm-message');
  const funnyEl = document.getElementById('custom-confirm-funny');
  const btnCancel = document.getElementById('custom-confirm-cancel');
  const btnOk = document.getElementById('custom-confirm-ok');

  if (!modalOverlay) return;

  let pendingForm = null;

  function closeModal() {
    modalOverlay.classList.remove('active');
    pendingForm = null;
    setTimeout(() => {
      modalOverlay.style.display = 'none';
    }, 200);
  }

  function openModal(message, formElement) {
    messageEl.textContent = message;
    const randomFunny = FUNNY_WARNINGS[Math.floor(Math.random() * FUNNY_WARNINGS.length)];
    funnyEl.textContent = randomFunny;
    pendingForm = formElement;
    
    modalOverlay.style.display = 'flex';
    // Force reflow
    void modalOverlay.offsetWidth;
    modalOverlay.classList.add('active');
  }

  btnCancel.addEventListener('click', closeModal);
  modalOverlay.addEventListener('click', (e) => {
    if (e.target === modalOverlay) closeModal();
  });

  btnOk.addEventListener('click', () => {
    if (pendingForm) {
      pendingForm.submit();
    }
  });

  // Intercept all forms with data-confirm
  document.querySelectorAll('form[data-confirm]').forEach(form => {
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      openModal(form.dataset.confirm, form);
    });
  });
}
